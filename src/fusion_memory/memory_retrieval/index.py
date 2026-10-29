"""FTS storage belongs to retrieval, separate from the authoritative database."""

from datetime import UTC, datetime

from fusion_memory.contracts import (
    Candidate,
    Capability,
    Change,
    DomainError,
    IndexManifest,
    Scope,
    SourceRecord,
)
from fusion_memory.memory_retrieval.text import TOKENIZER_VERSION, terms
from fusion_memory.providers.sqlite import SQLite

GENERATION = "lexical-1"
MANIFEST = IndexManifest(generation=GENERATION, tokenizer_version=TOKENIZER_VERSION)
DDL = """
CREATE TABLE manifest (generation TEXT PRIMARY KEY, tokenizer TEXT NOT NULL, specification TEXT NOT NULL);
CREATE TABLE watermarks (tenant TEXT, project TEXT, log_id TEXT, seq INTEGER NOT NULL,
 PRIMARY KEY(tenant,project));
CREATE VIRTUAL TABLE lexical USING fts5(
 tokens, tenant UNINDEXED, project UNINDEXED, entity_key UNINDEXED,
 event_id UNINDEXED, revision UNINDEXED, conversation_id UNINDEXED,
 tokenize='unicode61'
);
"""


class LexicalIndex:
    def __init__(self, db: SQLite):
        self._db = db
        db.migrate(DDL)
        with db.connect(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO manifest VALUES (?,?,?)",
                (GENERATION, TOKENIZER_VERSION, MANIFEST.model_dump_json()),
            )
            rows = conn.execute("SELECT * FROM manifest").fetchall()
            if (
                len(rows) != 1
                or rows[0]["generation"] != GENERATION
                or rows[0]["tokenizer"] != TOKENIZER_VERSION
                or rows[0]["specification"] != MANIFEST.model_dump_json()
            ):
                raise DomainError(
                    "index_incompatible", "Index manifest changed; build a new index generation"
                )

    def reader(self):
        return LexicalReader(self._db)

    def writer(self):
        return LexicalWriter(self._db)


class LexicalReader:
    """No mutation methods are given to the query service."""

    def __init__(self, db: SQLite):
        self._db = db

    def status(self, scope: Scope, log_id: str, target: int) -> Capability:
        with self._db.connect() as db:
            row = db.execute(
                "SELECT log_id,seq FROM watermarks WHERE tenant=? AND project=?",
                (scope.tenant_id, scope.project_id),
            ).fetchone()
            if row and row["log_id"] != log_id:
                raise DomainError("scope_mismatch", "Index belongs to a different authoritative log")
            covered = row["seq"] if row else 0
            return Capability(
                capability="lexical",
                generation=GENERATION,
                log_id=log_id,
                covered_commit_seq=covered,
                status="ready" if covered >= target else "catching_up",
                observed_at=datetime.now(UTC),
            )

    def search(self, scope: Scope, query: str, limit: int, conversation_id: str | None) -> list[Candidate]:
        tokens = list(dict.fromkeys(terms(query)))[:128]
        if not tokens:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        with self._db.connect() as db:
            rows = db.execute(
                "SELECT event_id,revision FROM lexical WHERE lexical MATCH ? AND tenant=? AND project=? "
                "AND (? IS NULL OR conversation_id=?) ORDER BY bm25(lexical),event_id LIMIT ?",
                (expression, scope.tenant_id, scope.project_id, conversation_id, conversation_id, limit),
            ).fetchall()
        return [
            Candidate(event_id=r["event_id"], revision=r["revision"], rank=i + 1, route="lexical")
            for i, r in enumerate(rows)
        ]


class LexicalWriter:
    def __init__(self, db: SQLite):
        self._db = db

    def watermark(self, scope: Scope, log_id: str) -> int:
        with self._db.connect() as db:
            row = db.execute(
                "SELECT * FROM watermarks WHERE tenant=? AND project=?", (scope.tenant_id, scope.project_id)
            ).fetchone()
            if row and row["log_id"] != log_id:
                raise DomainError("scope_mismatch", "Index belongs to another log")
            return row["seq"] if row else 0

    def apply(self, scope: Scope, change: Change, source: SourceRecord | None) -> None:
        with self._db.connect(write=True) as db:
            row = db.execute(
                "SELECT log_id,seq FROM watermarks WHERE tenant=? AND project=?",
                (scope.tenant_id, scope.project_id),
            ).fetchone()
            if row and row["log_id"] != change.log_id:
                raise DomainError("scope_mismatch", "Change belongs to another log")
            covered = row["seq"] if row else 0
            if change.commit_seq <= covered:
                return
            if change.commit_seq != covered + 1:
                raise DomainError("change_gap", "Cannot advance coverage across a missing change")
            db.execute(
                "DELETE FROM lexical WHERE tenant=? AND project=? AND entity_key=?",
                (scope.tenant_id, scope.project_id, change.entity_key),
            )
            if source and change.operation == "upsert":
                if source.event_id != change.event_id or source.revision != change.revision:
                    raise DomainError("revision_conflict", "Projection source revision mismatch")
                db.execute(
                    "INSERT INTO lexical VALUES (?,?,?,?,?,?,?)",
                    (
                        " ".join(terms(source.text)),
                        scope.tenant_id,
                        scope.project_id,
                        change.entity_key,
                        source.event_id,
                        source.revision,
                        source.conversation_id,
                    ),
                )
            # Projection and contiguous coverage share one index transaction.
            db.execute(
                "INSERT INTO watermarks VALUES (?,?,?,?) ON CONFLICT(tenant,project) "
                "DO UPDATE SET seq=excluded.seq",
                (scope.tenant_id, scope.project_id, change.log_id, change.commit_seq),
            )
