"""Exercise a real loopback server and recovery in a private temporary data root."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from fusion_memory.config import load_settings


def main():
    with tempfile.TemporaryDirectory(prefix="fusion-记忆 smoke-") as folder:
        root = Path(folder)
        settings = load_settings(root)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        headers = {"Authorization": "Bearer " + settings.credentials[0].token.get_secret_value()}
        base = f"http://127.0.0.1:{port}"
        cmd = [
            sys.executable,
            "-m",
            "fusion_memory.cli",
            "serve",
            "--data-root",
            str(root),
            "--port",
            str(port),
        ]

        def start():
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Smoke server exited before readiness")
                    try:
                        if httpx.get(base + "/health/ready", timeout=0.5).status_code == 200:
                            return process
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                raise RuntimeError("Smoke server readiness timed out")
            except BaseException:
                process.terminate()
                process.wait(timeout=15)
                raise

        event = {
            "schema_version": "1.1",
            "delivery_id": "smoke-1",
            "project_id": "default",
            "connector_id": "local-http",
            "middleware_id": "http",
            "origin_namespace": "local:default",
            "source_stream_id": "smoke",
            "source_event_id": "message-1",
            "source_revision": "1",
            "event_type": "message.upsert",
            "actor": {"external_id": "local-user", "role": "user"},
            "occurred_at": None,
            "mapping_revision": "example-v1",
            "content": [{"block_id": "b1", "type": "text", "text": "选择灰色 A-123 石材"}],
        }
        process = start()
        try:
            with httpx.Client(base_url=base, headers=headers, timeout=10) as client:
                posted = client.post("/v1/events", json=event)
                posted.raise_for_status()
                receipt = posted.json()
                search = {
                    "project_id": "default",
                    "query": "灰色 A-123",
                    "log_id": receipt["log_id"],
                    "min_commit_seq": receipt["commit_seq"],
                    "consistency_mode": "at_least",
                    "wait_timeout_ms": 2000,
                }
                result = client.post("/v1/memory/search", json=search)
                assert result.status_code == 200, result.text
                assert result.json()["evidence"][0]["source_id"] == receipt["event_id"]
                checkpoint = {
                    "checkpoint_id": "smoke-task",
                    "workflow_version": "v1",
                    "expected_revision": 0,
                    "idempotency_key": "cp1",
                    "state": {"next": "verify"},
                }
                client.post("/v1/checkpoints", json=checkpoint).raise_for_status()
        finally:
            # Deliberate abrupt process termination tests committed-data recovery.
            process.terminate()
            process.wait(timeout=15)
        process = start()
        try:
            with httpx.Client(base_url=base, headers=headers, timeout=10) as client:
                repeated = client.post("/v1/events", json=event).json()
                assert repeated["status"] == "duplicate"
                assert repeated["event_id"] == receipt["event_id"]
                assert client.get("/v1/checkpoints/smoke-task?workflow_version=v1").json()["state"] == {
                    "next": "verify"
                }
                deleted = {
                    **event,
                    "delivery_id": "smoke-delete",
                    "source_event_id": "delete-1",
                    "event_type": "message.delete",
                    "content": [],
                    "deletion_of": {
                        "source_stream_id": "smoke",
                        "source_event_id": "message-1",
                        "entity_type": "message",
                    },
                }
                client.post("/v1/events", json=deleted).raise_for_status()
                assert client.post("/v1/memory/search", json=search).json()["evidence"] == []
                assert client.get(f"/v1/sources/{receipt['event_id']}").status_code == 404
        finally:
            process.terminate()
            process.wait(timeout=15)
        print(
            json.dumps(
                {
                    "status": "passed",
                    "transport": "loopback HTTP",
                    "checks": [
                        "startup",
                        "ingest",
                        "background FTS",
                        "at_least",
                        "checkpoint",
                        "process restart",
                        "durable idempotency",
                        "deletion",
                        "Unicode path",
                        "owned-process cleanup",
                    ],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
