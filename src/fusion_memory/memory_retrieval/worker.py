from fusion_memory.contracts import MemoryChangeFeed, MemoryReadPort, Scope, SearchIndexWriter


class IndexWorker:
    def __init__(self, feed: MemoryChangeFeed, memory: MemoryReadPort, writer: SearchIndexWriter):
        self._feed, self._memory, self._writer = feed, memory, writer

    def run_once(self, scope: Scope, limit: int = 100) -> int:
        log_id, _ = self._memory.position(scope)
        covered = self._writer.watermark(scope, log_id)
        changes = self._feed.changes(scope, covered, limit)
        for change in changes:
            source = self._memory.read(scope, change.event_id) if change.operation == "upsert" else None
            self._writer.apply(scope, change, source)
        return len(changes)
