"""Single-controller durable case ledger, with fencing and immutable attempts."""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fusion_memory.evaluation.dataset import canonical, digest

DDL = """
CREATE TABLE IF NOT EXISTS run_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
INSERT OR IGNORE INTO run_state VALUES (1,'READY');
CREATE TABLE IF NOT EXISTS cases (
 case_id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'scheduled',
 fence INTEGER NOT NULL DEFAULT 0, lease_until REAL, result TEXT, result_hash TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
 case_id TEXT NOT NULL, fence INTEGER NOT NULL, state TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, result TEXT, result_hash TEXT,
 PRIMARY KEY(case_id,fence)
);
CREATE TABLE IF NOT EXISTS transitions (
 seq INTEGER PRIMARY KEY, at REAL NOT NULL, state TEXT NOT NULL
);
"""


class RunStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript(DDL)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self, value):
        with self.connect() as db:
            db.execute("UPDATE run_state SET state=? WHERE id=1", (value,))
            db.execute("INSERT INTO transitions(at,state) VALUES (?,?)", (time.time(), value))

    def schedule(self, bindings):
        with self.connect() as db:
            expected = {}
            for binding in bindings:
                body = canonical(binding)
                case_id = digest(body.encode())
                expected[case_id] = body
                db.execute("INSERT OR IGNORE INTO cases(case_id,binding) VALUES (?,?)", (case_id, body))
            actual = {r["case_id"]: r["binding"] for r in db.execute("SELECT case_id,binding FROM cases")}
            if actual != expected:
                raise ValueError("Task ledger differs from the frozen experiment population")

    def recover(self):
        # Caller must hold the OS run lock: no surviving controller may still dispatch.
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET state='interrupted',finished_at=? WHERE state='running'", (time.time(),)
            )
            db.execute("UPDATE cases SET state='scheduled',lease_until=NULL WHERE state='running'")

    def claim(self, case_id, lease_seconds):
        with self.connect() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if row is None or row["state"] != "scheduled":
                raise ValueError("Case is not scheduled")
            fence = row["fence"] + 1
            now = time.time()
            db.execute(
                "UPDATE cases SET state='running',fence=?,lease_until=? WHERE case_id=?",
                (fence, now + lease_seconds, case_id),
            )
            db.execute(
                "INSERT INTO attempts(case_id,fence,state,started_at) VALUES (?,?,'running',?)",
                (case_id, fence, now),
            )
            return fence

    def seal(self, case_id, fence, bundle):
        text = canonical(bundle)
        sha = digest(text.encode())
        with self.connect() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if row["state"] != "running" or row["fence"] != fence or row["lease_until"] < time.time():
                raise ValueError("Expired or stale attempt cannot seal a result")
            db.execute(
                "UPDATE attempts SET state='sealed',finished_at=?,result=?,result_hash=? "
                "WHERE case_id=? AND fence=?",
                (time.time(), text, sha, case_id, fence),
            )
            db.execute(
                "UPDATE cases SET state='sealed',result=?,result_hash=?,lease_until=NULL WHERE case_id=?",
                (text, sha, case_id),
            )

    def rows(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY case_id")]
        for row in rows:
            if digest(row["binding"].encode()) != row["case_id"]:
                raise ValueError("Case binding hash mismatch")
            if row["result"] is not None and digest(row["result"].encode()) != row["result_hash"]:
                raise ValueError("Sealed case hash mismatch")
        return rows

    def attempts(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY case_id,fence")]
