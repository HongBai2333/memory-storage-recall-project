import pytest
from fastapi.testclient import TestClient

from fusion_memory.app import create_app
from fusion_memory.config import Credential, Settings
from fusion_memory.contracts import Scope

TOKEN = "test-only-token-project-a-0123456789"
TOKEN_B = "test-only-token-project-b-0123456789"


@pytest.fixture
def settings(tmp_path):
    scope = Scope(
        tenant_id="tenant-a",
        project_id="project-a",
        connector_id="http-a",
        middleware_id="web",
        origin_namespace="source:a",
        actor_ids=("alice",),
        permissions=("read", "write", "checkpoint"),
    )
    return Settings(
        data_root=tmp_path / "记忆 data",
        credentials=[
            Credential(token=TOKEN, scope=scope),
            Credential(
                token=TOKEN_B,
                scope=scope.model_copy(update={"tenant_id": "tenant-b", "project_id": "project-b"}),
            ),
        ],
    )


@pytest.fixture
def client(settings):
    with TestClient(
        create_app(settings, run_worker=False), headers={"Authorization": f"Bearer {TOKEN}"}
    ) as c:
        yield c


@pytest.fixture
def event():
    return {
        "schema_version": "1.1",
        "delivery_id": "delivery-1",
        "project_id": "project-a",
        "connector_id": "http-a",
        "middleware_id": "web",
        "origin_namespace": "source:a",
        "source_stream_id": "session-1",
        "source_event_id": "msg-1",
        "source_revision": "v1",
        "source_sequence": 1,
        "event_type": "message.upsert",
        "conversation_id": "session-1",
        "actor": {"external_id": "alice", "role": "user"},
        "occurred_at": None,
        "content": [{"block_id": "b1", "type": "text", "text": "最终选用灰色 A-123 石材"}],
        "mapping_revision": "http-v1",
    }


def query(**updates):
    return {"project_id": "project-a", "query": "灰色 A-123", **updates}


def deletion(event):
    return {
        **event,
        "delivery_id": "delete-1",
        "source_event_id": "delete-1",
        "source_revision": "delete-v1",
        "source_sequence": 2,
        "event_type": "message.delete",
        "content": [],
        "deletion_of": {
            "source_stream_id": "session-1",
            "source_event_id": "msg-1",
            "entity_type": "message",
        },
    }
