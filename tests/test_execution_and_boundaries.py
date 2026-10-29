import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_memory.app import create_app
from fusion_memory.config import load_settings
from fusion_memory.runtime import RootLock


def test_checkpoint_cas_idempotency_and_restart(client, settings):
    command = {
        "checkpoint_id": "task-1",
        "workflow_version": "workflow-v1",
        "expected_revision": 0,
        "idempotency_key": "save-1",
        "state": {"next_step": "index", "source_refs": ["source-1"]},
    }
    first = client.post("/v1/checkpoints", json=command)
    assert first.status_code == 200
    assert first.json() == client.post("/v1/checkpoints", json=command).json()
    assert client.post("/v1/checkpoints", json={**command, "idempotency_key": "save-2"}).status_code == 409
    assert client.get("/v1/checkpoints/task-1?workflow_version=workflow-v2").status_code == 409
    assert (
        client.get("/v1/checkpoints/task-1?workflow_version=workflow-v1").json()["state"] == command["state"]
    )


def test_root_lock_prevents_double_writer(tmp_path):
    first = RootLock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="owns"):
            RootLock(tmp_path)
    finally:
        first.close()
    RootLock(tmp_path).close()


def test_default_config_persists_identity(tmp_path):
    first = load_settings(tmp_path)
    second = load_settings(tmp_path)
    assert first.credentials[0].token == second.credentials[0].token
    assert first.credentials[0].scope.tenant_id == "local"
    assert len(first.credentials[0].token.get_secret_value()) >= 24


def test_unauthorized_and_body_limit(settings, event):
    with TestClient(create_app(settings, run_worker=False)) as client:
        assert client.post("/v1/events", json=event).status_code == 401
        assert client.post("/v1/events", content=b"x" * 1_048_577).status_code == 413


def test_architecture_dependency_boundaries():
    root = Path(__file__).parents[1] / "src" / "fusion_memory"
    for module, forbidden in [
        ("memory_management", "memory_retrieval"),
        ("memory_retrieval", "memory_management"),
    ]:
        for file in (root / module).glob("*.py"):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert forbidden not in (node.module or ""), file
                elif isinstance(node, ast.Import):
                    assert all(forbidden not in alias.name for alias in node.names), file
    query = ast.parse((root / "memory_retrieval" / "service.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(query) if isinstance(n, ast.Attribute)}
    assert not ({"execute", "connect", "apply", "ingest", "writer"} & names)
