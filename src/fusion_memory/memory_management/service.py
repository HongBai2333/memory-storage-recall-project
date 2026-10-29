import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from uuid import uuid4

from fusion_memory.contracts import (
    Candidate,
    Change,
    DomainError,
    MemoryEvent,
    Receipt,
    Scope,
    SourceRecord,
    Validation,
)
from fusion_memory.providers.sqlite import SQLite

DDL = """
CREATE TABLE core (id TEXT NOT NULL);
CREATE TABLE logs (
 tenant TEXT NOT NULL, project TEXT NOT NULL, log_id TEXT UNIQUE NOT NULL,
 seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tenant, project)
);
CREATE TABLE events (
 event_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, project TEXT NOT NULL,
 canonical_key TEXT UNIQUE NOT NULL, entity_key TEXT NOT NULL,
 revision TEXT NOT NULL, source_sequence INTEGER, kind TEXT NOT NULL,
 payload TEXT NOT NULL, content_hash TEXT NOT NULL, log_id TEXT NOT NULL,
 seq INTEGER NOT NULL, received_at TEXT NOT NULL, text TEXT NOT NULL,
 conversation_id TEXT, occurred_at TEXT,
 UNIQUE(log_id, seq)
);
CREATE INDEX events_scope_seq ON events(tenant, project, seq);
CREATE TABLE heads (
 entity_key TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(event_id),
 revision TEXT NOT NULL, source_sequence INTEGER
);
CREATE TABLE tombstones (
 entity_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, deleted_at TEXT NOT NULL
);
CREATE TABLE changes (
 change_id TEXT PRIMARY KEY, log_id TEXT NOT NULL, seq INTEGER NOT NULL,
 event_id TEXT NOT NULL REFERENCES events(event_id), revision TEXT NOT NULL,
 entity_key TEXT NOT NULL, operation TEXT NOT NULL, UNIQUE(log_id, seq)
);
CREATE TABLE deliveries (
 tenant TEXT NOT NULL, project TEXT NOT NULL, connector TEXT NOT NULL,
 delivery_id TEXT NOT NULL, canonical_key TEXT NOT NULL,
 attempts INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(tenant, project, connector, delivery_id)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def stable_json(value) -> str:
    def normalize(v):
        if isinstance(v, str):
            return unicodedata.normalize("NFC", v)
        if isinstance(v, list):
            return [normalize(i) for i in v]
        if isinstance(v, dict):
            result = {}
            for k, item in v.items():
                nk = normalize(k)
                if nk in result:
                    raise DomainError("invalid_metadata", "Normalized keys collide", 422)
                result[nk] = normalize(item)
            return result
        return v

    try:
        return json.dumps(
            normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError) as exc:
        raise DomainError("invalid_json", "Only finite JSON values are accepted", 422) from exc


def key(*parts) -> str:
    return hashlib.sha256(stable_json(list(parts)).encode()).hexdigest()


class MemoryService:
    """Only owner of authoritative memory tables. No retrieval imports or calls."""

    def __init__(self, database: SQLite):
        self._db = database
        database.migrate(DDL)
        with database.connect(write=True) as db:
            if not db.execute("SELECT id FROM core").fetchone():
                db.execute("INSERT INTO core VALUES (?)", (str(uuid4()),))
            self.core_id = db.execute("SELECT id FROM core").fetchone()[0]

    def register_scope(self, scope: Scope):
        with self._db.connect(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO logs(tenant,project,log_id) VALUES (?,?,?)",
                (scope.tenant_id, scope.project_id, str(uuid4())),
            )

    @staticmethod
    def _log(db, scope):
        row = db.execute(
            "SELECT log_id,seq FROM logs WHERE tenant=? AND project=?", (scope.tenant_id, scope.project_id)
        ).fetchone()
        if row is None:
            raise DomainError("unknown_project", "Project not registered", 403)
        return row

    def position(self, scope: Scope) -> tuple[str, int]:
        with self._db.connect() as db:
            row = self._log(db, scope)
            return row["log_id"], row["seq"]

    def ingest(self, scope: Scope, event: MemoryEvent, request_id: str) -> Receipt:
        if "write" not in scope.permissions:
            raise DomainError("forbidden", "Write permission required", 403)
        for field in ("project_id", "connector_id", "middleware_id", "origin_namespace"):
            if getattr(event, field) != getattr(scope, field):
                raise DomainError("scope_mismatch", "Event does not match authenticated connector", 403)
        if event.actor.external_id not in scope.actor_ids:
            raise DomainError("actor_unmapped", "Source actor has no registered identity mapping", 403)
        if len(stable_json(event.metadata).encode()) > 16_384:
            raise DomainError("metadata_too_large", "Metadata exceeds 16 KiB", 422)
        payload = event.model_dump(mode="json")
        semantic = {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "delivery_id",
                "connector_id",
                "middleware_id",
                "trace_id",
                "correlation_id",
                "causation_id",
                "backfill",
                "mapping_revision",
            }
        }
        digest = hashlib.sha256(stable_json(semantic).encode()).hexdigest()
        identity = (
            scope.tenant_id,
            scope.project_id,
            event.origin_namespace,
            event.source_stream_id,
            event.source_event_id,
        )
        canonical = key(*identity, event.source_revision, event.event_type)
        entity = key(*identity, event.event_type.split(".")[0])
        deleting = event.deletion_of is not None
        if deleting:
            target = event.deletion_of
            entity = key(
                scope.tenant_id,
                scope.project_id,
                event.origin_namespace,
                target.source_stream_id,
                target.source_event_id,
                target.entity_type,
            )
        received_at = now()
        with self._db.connect(write=True) as db:
            delivery_key = (scope.tenant_id, scope.project_id, scope.connector_id, event.delivery_id)
            delivery = db.execute(
                "SELECT canonical_key FROM deliveries WHERE tenant=? AND project=? "
                "AND connector=? AND delivery_id=?",
                delivery_key,
            ).fetchone()
            if delivery and delivery[0] != canonical:
                raise DomainError("delivery_conflict", "Delivery ID already identifies another source event")
            existing = db.execute("SELECT * FROM events WHERE canonical_key=?", (canonical,)).fetchone()
            if existing and existing["content_hash"] != digest:
                raise DomainError(
                    "idempotency_conflict", "Source revision already has different canonical content"
                )
            if existing:
                row, status = existing, "duplicate"
            else:
                head = db.execute("SELECT * FROM heads WHERE entity_key=?", (entity,)).fetchone()
                if not deleting:
                    if db.execute("SELECT 1 FROM tombstones WHERE entity_key=?", (entity,)).fetchone():
                        raise DomainError(
                            "source_deleted", "Deleted sources require an explicit restore workflow", 410
                        )
                    if head:
                        if event.expected_previous_revision is not None:
                            if event.expected_previous_revision != head["revision"]:
                                raise DomainError(
                                    "revision_conflict", "Previous source revision does not match"
                                )
                        elif event.source_sequence is None or head["source_sequence"] is None:
                            raise DomainError(
                                "unordered_revision", "Revision requires CAS or a monotonic source sequence"
                            )
                        if (
                            event.source_sequence is not None
                            and head["source_sequence"] is not None
                            and event.source_sequence <= head["source_sequence"]
                        ):
                            raise DomainError(
                                "stale_revision", "Late source revision cannot replace the current version"
                            )
                    elif event.expected_previous_revision is not None:
                        raise DomainError("revision_conflict", "Expected predecessor does not exist")
                log = self._log(db, scope)
                seq = log["seq"] + 1
                event_id = str(uuid4())
                db.execute("UPDATE logs SET seq=? WHERE log_id=?", (seq, log["log_id"]))
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        scope.tenant_id,
                        scope.project_id,
                        canonical,
                        entity,
                        event.source_revision,
                        event.source_sequence,
                        event.event_type,
                        stable_json(payload),
                        digest,
                        log["log_id"],
                        seq,
                        received_at,
                        "\n".join(b.text for b in event.content),
                        event.conversation_id,
                        event.occurred_at.isoformat() if event.occurred_at else None,
                    ),
                )
                if deleting:
                    db.execute(
                        "INSERT OR IGNORE INTO tombstones VALUES (?,?,?)", (entity, event_id, received_at)
                    )
                else:
                    db.execute(
                        "INSERT INTO heads VALUES (?,?,?,?) ON CONFLICT(entity_key) DO UPDATE SET "
                        "event_id=excluded.event_id, revision=excluded.revision, "
                        "source_sequence=excluded.source_sequence",
                        (entity, event_id, event.source_revision, event.source_sequence),
                    )
                # The durable change log is also the replayable outbox: no destructive acknowledgement.
                db.execute(
                    "INSERT INTO changes VALUES (?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        log["log_id"],
                        seq,
                        event_id,
                        event.source_revision,
                        entity,
                        "delete" if deleting else "upsert",
                    ),
                )
                row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                status = "committed"
            db.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(tenant,project,connector,delivery_id) DO UPDATE SET "
                "attempts=attempts+1,last_seen_at=excluded.last_seen_at",
                (*delivery_key, canonical, received_at),
            )
            return Receipt(
                request_id=request_id,
                delivery_id=event.delivery_id,
                status=status,
                event_id=row["event_id"],
                tenant_id=scope.tenant_id,
                core_instance_id=self.core_id,
                log_id=row["log_id"],
                commit_seq=row["seq"],
                received_at=row["received_at"],
                content_hash=row["content_hash"],
                processing_ref=f"/v1/processing-status/{row['event_id']}",
            )

    @staticmethod
    def _source(row) -> SourceRecord:
        return SourceRecord(
            event_id=row["event_id"],
            revision=row["revision"],
            log_id=row["log_id"],
            commit_seq=row["seq"],
            text=row["text"],
            occurred_at=row["occurred_at"],
            received_at=row["received_at"],
            conversation_id=row["conversation_id"],
            entity_key=row["entity_key"],
        )

    def read(self, scope: Scope, event_id: str) -> SourceRecord | None:
        with self._db.connect() as db:
            row = db.execute(
                "SELECT e.* FROM events e JOIN heads h ON h.event_id=e.event_id "
                "WHERE e.tenant=? AND e.project=? AND e.event_id=? "
                "AND NOT EXISTS(SELECT 1 FROM tombstones t WHERE t.entity_key=e.entity_key)",
                (scope.tenant_id, scope.project_id, event_id),
            ).fetchone()
            return self._source(row) if row else None

    def receipt_position(self, scope: Scope, event_id: str) -> tuple[str, int]:
        with self._db.connect() as db:
            row = db.execute(
                "SELECT log_id,seq FROM events WHERE tenant=? AND project=? AND event_id=?",
                (scope.tenant_id, scope.project_id, event_id),
            ).fetchone()
            if not row:
                raise DomainError("not_found", "Source not found", 404)
            return row["log_id"], row["seq"]

    def recent(self, scope: Scope, after: int, through: int, limit: int) -> tuple[list[SourceRecord], bool]:
        with self._db.connect() as db:
            rows = db.execute(
                "SELECT e.* FROM events e JOIN heads h ON h.event_id=e.event_id "
                "WHERE e.tenant=? AND e.project=? AND e.seq>? AND e.seq<=? "
                "AND NOT EXISTS(SELECT 1 FROM tombstones t WHERE t.entity_key=e.entity_key) "
                "ORDER BY e.seq DESC LIMIT ?",
                (scope.tenant_id, scope.project_id, after, through, limit + 1),
            ).fetchall()
            return [self._source(r) for r in rows[:limit]], len(rows) > limit

    def changes(self, scope: Scope, after: int, limit: int) -> list[Change]:
        with self._db.connect() as db:
            log = self._log(db, scope)
            rows = db.execute(
                "SELECT * FROM changes WHERE log_id=? AND seq>? ORDER BY seq LIMIT ?",
                (log["log_id"], after, limit),
            ).fetchall()
            return [
                Change(
                    change_id=r["change_id"],
                    log_id=r["log_id"],
                    commit_seq=r["seq"],
                    event_id=r["event_id"],
                    revision=r["revision"],
                    entity_key=r["entity_key"],
                    operation=r["operation"],
                )
                for r in rows
            ]

    def validate(self, scope: Scope, candidates: list[Candidate]) -> Validation:
        with self._db.connect() as db:
            log = self._log(db, scope)  # Establish one authoritative read snapshot.
            allowed = []
            for item in candidates:
                row = db.execute(
                    "SELECT 1 FROM events e JOIN heads h ON h.event_id=e.event_id "
                    "WHERE e.tenant=? AND e.project=? AND e.event_id=? AND e.revision=? "
                    "AND NOT EXISTS(SELECT 1 FROM tombstones t WHERE t.entity_key=e.entity_key)",
                    (scope.tenant_id, scope.project_id, item.event_id, item.revision),
                ).fetchone()
                if row:
                    allowed.append(item.event_id)
            return Validation(allowed=tuple(allowed), validation_revision=log["seq"], validated_at=now())
