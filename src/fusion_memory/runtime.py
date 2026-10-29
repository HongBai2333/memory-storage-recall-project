import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from fusion_memory.config import Settings
from fusion_memory.contracts import DomainError, Scope
from fusion_memory.execution import Checkpoints
from fusion_memory.memory_management.service import MemoryService
from fusion_memory.memory_retrieval.index import LexicalIndex
from fusion_memory.memory_retrieval.service import RetrievalService
from fusion_memory.memory_retrieval.worker import IndexWorker
from fusion_memory.providers.sqlite import SQLite

logger = logging.getLogger(__name__)


class RootLock:
    """An OS lock, released after process death; a leftover file is not a held lock."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self._file = (root / "service.lock").open("a+b")
        self._file.seek(0, 2)
        if self._file.tell() == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise RuntimeError("Another service owns this data root") from None

    def close(self):
        self._file.close()


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.memory = MemoryService(SQLite(settings.data_root / "db" / "memory.sqlite"))
        self.checkpoints = Checkpoints(SQLite(settings.data_root / "db" / "execution.sqlite"))
        self.scopes = list(
            {(c.scope.tenant_id, c.scope.project_id): c.scope for c in settings.credentials}.values()
        )
        for scope in self.scopes:
            self.memory.register_scope(scope)
        self.worker = None
        self.index_error = None
        reader = None
        if settings.index_enabled:
            try:
                index = LexicalIndex(SQLite(settings.data_root / "db" / "retrieval.sqlite"))
                reader = index.reader()
                self.worker = IndexWorker(self.memory, self.memory, index.writer())
            except (sqlite3.Error, DomainError):
                self.index_error = "index_initialization_failed"
                logger.warning("Lexical index unavailable; raw ingestion remains available")
        self.stop = threading.Event()
        self.failures: dict[tuple[str, str], int] = {}
        self.retry_at: dict[tuple[str, str], float] = {}
        self.retrieval = RetrievalService(self.memory, self.memory, reader, self._failure_reason)
        self.thread = threading.Thread(target=self._loop, name="fusion-index", daemon=True)

    def _failure_reason(self, scope: Scope) -> str | None:
        if self.index_error:
            return self.index_error
        if self.failures.get((scope.tenant_id, scope.project_id), 0) >= 5:
            return "retry_limit_reached_restart_after_repair"
        return None

    def _loop(self):
        while not self.stop.is_set():
            if self.worker:
                for scope in self.scopes:
                    key = (scope.tenant_id, scope.project_id)
                    if self.failures.get(key, 0) >= 5:
                        continue
                    if time.monotonic() < self.retry_at.get(key, 0):
                        continue
                    try:
                        self.worker.run_once(scope)
                        self.failures[key] = 0
                        self.retry_at.pop(key, None)
                    except Exception:
                        self.failures[key] = self.failures.get(key, 0) + 1
                        self.retry_at[key] = time.monotonic() + min(30, 2 ** (self.failures[key] - 1))
                        # No private body, SQL parameters or credentials in failure logs.
                        logger.warning(
                            "Index batch failed (attempt %d/5); committed data retained", self.failures[key]
                        )
            self.stop.wait(self.settings.index_interval_ms / 1000)

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()  # Do not release the root lock while a writer still runs.
