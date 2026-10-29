"""One trusted local SUT process per question; reads only PublicCase from stdin."""

import heapq
import json
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from fusion_memory.config import Credential, Settings
from fusion_memory.contracts import DomainError, SearchRequest
from fusion_memory.evaluation.contracts import PublicCase
from fusion_memory.runtime import Runtime


def execute(case: PublicCase, root: Path) -> dict:
    available = {"raw", "lexical"} if case.variant.index_enabled else {"raw"}
    missing = set(case.question.required_capabilities) - available
    if case.question.time_mode != "current":
        missing.add("bitemporal_query")
    base = {
        "clock": "prefix_settled" if case.mode == "capability" else "simulated",
        "isolation": "fresh_process_fresh_store_public_stdin",
        "os_sandbox": False,
        "capabilities": sorted(available),
        "model_calls": 0,
        "receipts": [],
        "processing": [],
    }
    if missing:
        return {**base, "status": "blocked_dependency", "missing": sorted(missing)}
    settings = Settings(
        data_root=root,
        index_enabled=case.variant.index_enabled,
        credentials=[
            Credential(token=f"eval-ephemeral-{i:024d}", scope=scope)
            for i, scope in enumerate(case.subjects.values())
        ],
    )
    runtime = Runtime(settings)  # No background thread: the replay scheduler owns worker advancement.
    queue, due_by_scope = [], {}
    serial = 0

    def advance(until=None):
        while queue and (until is None or queue[0][0] <= until):
            at, _, subject, target = heapq.heappop(queue)
            scope = case.subjects[subject]
            if runtime.worker is None:
                raise RuntimeError("Declared lexical dependency failed to initialize")
            count = runtime.worker.run_once(scope, limit=1)
            log_id, _ = runtime.memory.position(scope)
            progress = runtime.retrieval.progress(scope, log_id, target)["lexical"]
            base["processing"].append(
                {
                    "subject": subject,
                    "target_commit_seq": target,
                    "changes_applied": count,
                    "available_at_simulated": at.isoformat() if case.mode == "operational" else None,
                    "state": progress.model_dump(mode="json"),
                }
            )
            if progress.covered_commit_seq < target:
                raise RuntimeError("Continuous processing frontier did not reach the scheduled commit")

    try:
        base["empty_positions"] = {
            ref: runtime.memory.position(scope)[1] for ref, scope in case.subjects.items()
        }
        if any(base["empty_positions"].values()):
            raise RuntimeError("SUT namespace was not empty")
        for fixture in case.events:
            if fixture.replay_seq > case.question.cutoff_replay_seq:
                raise ValueError("SUT input crosses the query prefix")
            if case.mode == "operational":
                advance(fixture.fixture_received_at)
            scope = case.subjects[fixture.auth_subject_ref]
            receipt = runtime.memory.ingest(scope, fixture.payload, fixture.fixture_event_id)
            base["receipts"].append(
                {
                    "fixture_event_id": fixture.fixture_event_id,
                    "fixture_received_at": fixture.fixture_received_at.isoformat(),
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
            if receipt.status == "committed" and runtime.worker:
                scope_key = (scope.tenant_id, scope.project_id)
                due = max(
                    fixture.fixture_received_at, due_by_scope.get(scope_key, fixture.fixture_received_at)
                ) + timedelta(milliseconds=case.variant.worker_delay_ms)
                due_by_scope[scope_key] = due
                serial += 1
                heapq.heappush(queue, (due, serial, fixture.auth_subject_ref, receipt.commit_seq))
        advance(None if case.mode == "capability" else case.question.query_at)
        q = case.question
        scope = case.subjects[q.auth_subject_ref]
        log, target = runtime.memory.position(scope)
        before = {ref: runtime.memory.position(s)[1] for ref, s in case.subjects.items()}
        request = SearchRequest(
            project_id=q.project_id,
            query=q.question,
            conversation_id=q.conversation_id,
            log_id=log,
            min_commit_seq=target,
            consistency_mode=q.consistency_mode,
            wait_timeout_ms=0,
            required_capabilities=tuple(
                name for name in q.required_capabilities if name in {"lexical", "facts", "vector"}
            ),
            optional_capabilities=(),
            budget=case.budget,
        )
        start = time.perf_counter()
        try:
            if "read" not in scope.permissions or q.project_id != scope.project_id:
                raise DomainError("forbidden", "Query credential does not permit this project", 403)
            response = runtime.retrieval.search(scope, request)
            action = (
                "pending"
                if response["status"] == "pending"
                else "answer"
                if response["evidence"]
                else "insufficient_evidence"
            )
        except DomainError as exc:
            if exc.status != 403:
                raise
            response = {"context": "", "evidence": [], "estimated_tokens": 0, "error": exc.code}
            action = "deny"
        elapsed_ms = (time.perf_counter() - start) * 1000
        after = {ref: runtime.memory.position(s)[1] for ref, s in case.subjects.items()}
        return {
            **base,
            "status": "succeeded",
            "request": request.model_dump(mode="json"),
            "response": response,
            "action": action,
            "answer": response["context"] if action == "answer" else "",
            "host": "extractive-context-v1",
            "host_input": response["context"],
            "query_elapsed_ms": elapsed_ms,
            "query_deadline_exceeded": elapsed_ms > case.query_deadline_ms,
            "before_query_positions": before,
            "after_query_positions": after,
            "pending_processing_jobs": len(queue),
        }
    except Exception as exc:
        return {
            **base,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_code": getattr(exc, "code", "sut_execution_failed"),
        }
    finally:
        runtime.close()


def main():
    case = PublicCase.model_validate_json(sys.stdin.buffer.read())
    with tempfile.TemporaryDirectory(prefix="fusion-eval-sut-") as directory:
        result = execute(case, Path(directory))
    result["cleanup"] = "disposed"
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
