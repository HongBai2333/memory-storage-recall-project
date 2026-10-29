import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class SQLite:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self, *, write: bool = False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def migrate(self, ddl: str):
        # Called before workers start. One service owns this root (CLI root lock).
        with closing(sqlite3.connect(self.path)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Database schema newer than this service")
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;\n" + ddl + "\nPRAGMA user_version=1;\nCOMMIT;")
