from collections.abc import Callable
from datetime import UTC, datetime

from fusion_memory.contracts import (
    Candidate,
    Capability,
    DomainError,
    EvidenceValidationPort,
    MemoryReadPort,
    Scope,
    SearchIndexReader,
    SearchRequest,
)
from fusion_memory.memory_retrieval.text import matches


class RetrievalService:
    def __init__(
        self,
        memory: MemoryReadPort,
        validation: EvidenceValidationPort,
        index: SearchIndexReader | None,
        failure_reason: Callable[[Scope], str | None] | None = None,
    ):
        self._memory, self._validation, self._index = memory, validation, index
        self._failure_reason = failure_reason

    def progress(self, scope: Scope, log_id: str, target: int) -> dict[str, Capability]:
        progress = {}
        for name in ("lexical", "vector", "facts"):
            if name == "lexical" and self._index:
                progress[name] = self._index.status(scope, log_id, target)
                reason = self._failure_reason(scope) if self._failure_reason else None
                if reason:
                    progress[name] = progress[name].model_copy(update={"status": "failed", "reason": reason})
            else:
                progress[name] = Capability(
                    capability=name,
                    generation=None,
                    log_id=log_id,
                    covered_commit_seq=0,
                    status="unavailable",
                    observed_at=datetime.now(UTC),
                    reason="not_implemented" if name in ("vector", "facts") else "index_disabled",
                )
                if name == "lexical" and self._failure_reason:
                    reason = self._failure_reason(scope)
                    if reason:
                        progress[name] = progress[name].model_copy(update={"reason": reason})
        return progress

    def search(self, scope: Scope, request: SearchRequest) -> dict:
        log_id, current = self._memory.position(scope)
        if request.project_id != scope.project_id or (request.log_id and request.log_id != log_id):
            raise DomainError("scope_mismatch", "Query and authenticated log scope differ", 409)
        if request.min_commit_seq and not request.log_id:
            raise DomainError("log_id_required", "min_commit_seq requires its log_id", 422)
        target = request.min_commit_seq or current
        progress = self.progress(scope, log_id, target)
        ready = all(progress[c].status == "ready" for c in request.required_capabilities)
        if request.min_commit_seq > current:
            ready = False
        status = "pending" if request.consistency_mode == "at_least" and not ready else "complete"
        result = {
            "status": status,
            "log_id": log_id,
            "target_commit_seq": target,
            "observed_commit_seq": current,
            "capabilities": {k: v.model_dump(mode="json") for k, v in progress.items()},
            "partial": not ready,
            "evidence": [],
            "uncovered_ranges": [],
            "insufficient_evidence": True,
            "context": "",
            "estimated_tokens": 0,
            "token_estimator": "utf8-bytes-upper-bound-v1",
            "processing_ref": "/v1/capabilities",
        }
        if status == "pending":
            return result
        candidates = (
            self._index.search(scope, request.query, 80, request.conversation_id) if self._index else []
        )
        covered = progress["lexical"].covered_commit_seq
        recent, truncated = self._memory.recent(scope, covered, current, request.budget.recent_limit)
        candidates.extend(
            Candidate(event_id=s.event_id, revision=s.revision, rank=i + 1, route="recent_raw")
            for i, s in enumerate(recent)
            if matches(request.query, s.text)
            and (request.conversation_id is None or s.conversation_id == request.conversation_id)
        )
        if covered < current:
            result["uncovered_ranges"] = [
                {"after": covered, "through": current, "recent_fallback_truncated": truncated}
            ]
        result["partial"] |= truncated
        # First gate before contents could reach any future reranker/model.
        first = self._validation.validate(scope, candidates)
        grouped = {}
        for candidate in candidates:
            if candidate.event_id not in first.allowed:
                continue
            group = grouped.setdefault(candidate.event_id, {"candidate": candidate, "score": 0, "routes": []})
            if candidate.route not in group["routes"]:
                group["score"] += 1 / (60 + candidate.rank)
                group["routes"].append(candidate.route)
        selected = []
        remaining = request.budget.max_context_tokens
        for group in sorted(grouped.values(), key=lambda g: -g["score"]):
            candidate = group["candidate"]
            source = self._memory.read(scope, candidate.event_id)
            if source is None:
                continue
            citation = f"[memory:{source.event_id}@{source.revision}]"
            # UTF-8 bytes give a conservative bound for common byte/subword tokenizers.
            overhead = len((citation + "\n\n").encode())
            if remaining <= overhead:
                break
            excerpt = source.text.encode()[: remaining - overhead].decode("utf-8", errors="ignore")
            if not excerpt:
                continue
            cost = len((citation + "\n" + excerpt + "\n").encode())
            selected.append(
                {
                    "source_id": source.event_id,
                    "revision": source.revision,
                    "excerpt": excerpt,
                    "occurred_at": source.occurred_at.isoformat() if source.occurred_at else None,
                    "known_at": source.received_at.isoformat(),
                    "memory_type": "raw_event",
                    "confidence_class": "source_record",
                    "retrieval_routes": group["routes"],
                    "citation": citation,
                    "excerpt_truncated": excerpt != source.text,
                    "estimated_tokens": cost,
                }
            )
            remaining -= cost
            if len(selected) >= request.budget.max_evidence_items:
                break
        # Final authoritative gate suppresses deletion or supersession during assembly.
        final = self._validation.validate(
            scope,
            [
                Candidate(event_id=e["source_id"], revision=e["revision"], rank=i + 1, route="release")
                for i, e in enumerate(selected)
            ],
        )
        released = [e for e in selected if e["source_id"] in final.allowed]
        result.update(
            evidence=released,
            insufficient_evidence=not released,
            validation_revision=final.validation_revision,
            validated_at=final.validated_at.isoformat(),
            context="".join(e["citation"] + "\n" + e["excerpt"] + "\n" for e in released),
            estimated_tokens=sum(e["estimated_tokens"] for e in released),
        )
        result["partial"] |= len(released) != len(selected)
        return result
