"""Workflow state is deliberately separate from long-term memory facts."""

import json
from datetime import UTC, datetime

from fusion_memory.contracts import CheckpointWrite, DomainError, Scope
from fusion_memory.providers.sqlite import SQLite

DDL = """
CREATE TABLE checkpoints (
 tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
 workflow_version TEXT NOT NULL, state TEXT NOT NULL, saved_at TEXT NOT NULL,
 PRIMARY KEY(tenant,project,id)
);
CREATE TABLE commands (
 tenant TEXT NOT NULL, project TEXT NOT NULL, key TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant,project,key)
);
"""


class Checkpoints:
    def __init__(self, db: SQLite):
        self._db = db
        db.migrate(DDL)

    def save(self, scope: Scope, command: CheckpointWrite):
        payload = command.model_dump_json()
        if len(payload.encode()) > 256_000:
            raise DomainError("checkpoint_too_large", "Checkpoint exceeds limit", 422)
        with self._db.connect(write=True) as db:
            existing = db.execute(
                "SELECT payload,result FROM commands WHERE tenant=? AND project=? AND key=?",
                (scope.tenant_id, scope.project_id, command.idempotency_key),
            ).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise DomainError("idempotency_conflict", "Checkpoint command changed")
                return json.loads(existing["result"])
            current = db.execute(
                "SELECT revision FROM checkpoints WHERE tenant=? AND project=? AND id=?",
                (scope.tenant_id, scope.project_id, command.checkpoint_id),
            ).fetchone()
            revision = current[0] if current else 0
            if command.expected_revision != revision:
                raise DomainError("revision_conflict", "Checkpoint revision changed")
            result = {
                "checkpoint_id": command.checkpoint_id,
                "revision": revision + 1,
                "workflow_version": command.workflow_version,
                "state": command.state,
                "saved_at": datetime.now(UTC).isoformat(),
            }
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?) ON CONFLICT(tenant,project,id) "
                "DO UPDATE SET revision=excluded.revision,workflow_version=excluded.workflow_version,"
                "state=excluded.state,saved_at=excluded.saved_at",
                (
                    scope.tenant_id,
                    scope.project_id,
                    command.checkpoint_id,
                    revision + 1,
                    command.workflow_version,
                    json.dumps(command.state),
                    result["saved_at"],
                ),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?)",
                (scope.tenant_id, scope.project_id, command.idempotency_key, payload, json.dumps(result)),
            )
            return result

    def read(self, scope: Scope, checkpoint_id: str, workflow_version: str):
        with self._db.connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE tenant=? AND project=? AND id=?",
                (scope.tenant_id, scope.project_id, checkpoint_id),
            ).fetchone()
            if row is None:
                raise DomainError("not_found", "Checkpoint not found", 404)
            if row["workflow_version"] != workflow_version:
                raise DomainError("workflow_mismatch", "Checkpoint belongs to another workflow version")
            return {
                "checkpoint_id": row["id"],
                "revision": row["revision"],
                "workflow_version": workflow_version,
                "state": json.loads(row["state"]),
                "saved_at": row["saved_at"],
            }
