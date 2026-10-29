from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import TOKEN_B, deletion, query

from fusion_memory.contracts import DomainError, MemoryEvent
from fusion_memory.runtime import Runtime


def test_commit_without_retrieval(settings, event):
    runtime = Runtime(settings.model_copy(update={"index_enabled": False}))
    scope = settings.credentials[0].scope
    receipt = runtime.memory.ingest(scope, MemoryEvent(**event), "request")
    assert receipt.schema_version == "1.2"
    assert receipt.commit_seq == 1
    assert runtime.memory.read(scope, receipt.event_id).text == event["content"][0]["text"]
    assert runtime.worker is None
    restarted = Runtime(settings)
    assert restarted.worker.run_once(scope) == 1
    assert restarted.memory.position(scope) == (receipt.log_id, 1)


def test_duplicate_across_connectors(settings, event):
    runtime = Runtime(settings)
    scope = settings.credentials[0].scope
    first = runtime.memory.ingest(scope, MemoryEvent(**event), "one")
    second_scope = scope.model_copy(update={"connector_id": "dsh-bridge", "middleware_id": "dsh"})
    second = runtime.memory.ingest(
        second_scope,
        MemoryEvent(
            **{
                **event,
                "delivery_id": "other",
                "connector_id": "dsh-bridge",
                "middleware_id": "dsh",
                "trace_id": "new-trace",
                "backfill": True,
                "mapping_revision": "another-mapping",
            }
        ),
        "two",
    )
    assert second.status == "duplicate"
    assert (second.event_id, second.commit_seq, second.received_at) == (first.event_id, 1, first.received_at)
    assert len(runtime.memory.changes(scope, 0, 100)) == 1


def test_concurrent_idempotency(settings, event):
    runtime = Runtime(settings)
    scope = settings.credentials[0].scope
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(
            pool.map(lambda i: runtime.memory.ingest(scope, MemoryEvent(**event), str(i)), range(16))
        )
    assert len({r.event_id for r in receipts}) == 1
    assert sum(r.status == "committed" for r in receipts) == 1
    assert runtime.memory.position(scope)[1] == 1


def test_conflict_does_not_advance_log(client, event):
    first = client.post("/v1/events", json=event).json()
    changed = {**event, "content": [{"block_id": "b1", "type": "text", "text": "different"}]}
    response = client.post("/v1/events", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert (
        client.app.state.runtime.memory.position(client.app.state.runtime.scopes[0])[1] == first["commit_seq"]
    )


def test_delivery_id_cannot_identify_another_event(client, event):
    client.post("/v1/events", json=event).raise_for_status()
    response = client.post("/v1/events", json={**event, "source_event_id": "other"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "delivery_conflict"


@pytest.mark.parametrize(
    "override",
    [
        {"tenant_id": "attacker"},
        {"commit_seq": 10},
        {"schema_version": "1.2"},
        {"content": []},
        {"occurred_at": "2026-10-28T10:00:00"},
        {"content": [{"block_id": "x", "type": "image", "asset_id": "unverified"}]},
    ],
)
def test_invalid_contract_rejected(client, event, override):
    response = client.post("/v1/events", json={**event, **override})
    assert response.status_code == 422
    assert client.app.state.runtime.memory.position(client.app.state.runtime.scopes[0])[1] == 0


def test_source_identity_bound_to_credential(client, event):
    response = client.post("/v1/events", json={**event, "origin_namespace": "stolen"})
    assert response.status_code == 403
    response = client.post("/v1/events", json={**event, "actor": {"external_id": "mallory", "role": "user"}})
    assert response.status_code == 403


def test_new_revision_and_late_revision(client, event):
    original = client.post("/v1/events", json=event).json()
    revised = {
        **event,
        "delivery_id": "d2",
        "source_revision": "v10",
        "source_sequence": 10,
        "content": [{"block_id": "b1", "type": "text", "text": "改用蓝色 B-456"}],
    }
    current = client.post("/v1/events", json=revised)
    assert current.status_code == 200
    late = client.post(
        "/v1/events", json={**revised, "delivery_id": "d3", "source_revision": "v9", "source_sequence": 9}
    )
    assert late.status_code == 409
    assert client.get(f"/v1/sources/{original['event_id']}").status_code == 404
    result = client.post("/v1/memory/search", json=query(query="蓝色 B-456")).json()
    assert result["evidence"][0]["revision"] == "v10"


def test_cross_tenant_read_isolation(client, event):
    receipt = client.post("/v1/events", json=event).json()
    headers = {"Authorization": f"Bearer {TOKEN_B}"}
    assert client.get(f"/v1/sources/{receipt['event_id']}", headers=headers).status_code == 404
    assert client.get(f"/v1/processing-status/{receipt['event_id']}", headers=headers).status_code == 404
    result = client.post("/v1/memory/search", json=query(project_id="project-b"), headers=headers)
    assert result.json()["evidence"] == []
    assert client.post("/v1/events", json=event, headers=headers).status_code == 403


def test_tombstone_blocks_resurrection(client, event):
    client.post("/v1/events", json=event).raise_for_status()
    client.post("/v1/events", json=deletion(event)).raise_for_status()
    result = client.post(
        "/v1/events", json={**event, "delivery_id": "d3", "source_revision": "new", "source_sequence": 20}
    )
    assert result.status_code == 410
    assert client.post("/v1/events", json=event).json()["status"] == "duplicate"
    assert client.post("/v1/memory/search", json=query()).json()["evidence"] == []


def test_transaction_failure_rolls_back_event_and_log(settings, event):
    runtime = Runtime(settings)
    scope = settings.credentials[0].scope
    with runtime.memory._db.connect(write=True) as db:
        db.execute(
            "CREATE TRIGGER fail_change BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        runtime.memory.ingest(scope, MemoryEvent(**event), "request")
    assert runtime.memory.position(scope)[1] == 0
    assert runtime.memory.changes(scope, 0, 100) == []
    with runtime.memory._db.connect(write=True) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_change")
    assert runtime.memory.ingest(scope, MemoryEvent(**event), "retry").status == "committed"


def test_cas_revision_without_sequence(settings, event):
    runtime = Runtime(settings)
    scope = settings.credentials[0].scope
    runtime.memory.ingest(scope, MemoryEvent(**{**event, "source_sequence": None}), "a")
    updated = {**event, "delivery_id": "d2", "source_revision": "next", "source_sequence": None}
    with pytest.raises(DomainError, match="CAS"):
        runtime.memory.ingest(scope, MemoryEvent(**updated), "b")
    assert (
        runtime.memory.ingest(
            scope, MemoryEvent(**{**updated, "expected_previous_revision": "v1"}), "c"
        ).status
        == "committed"
    )
