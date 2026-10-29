"""Deterministic, deliberately narrow literal/extractive rubric; never an LLM judge."""

from fusion_memory.evaluation.contracts import Experiment, Question
from fusion_memory.evaluation.dataset import Dataset, live_sources

RUBRIC_VERSION = "literal-extractive-v1"


def score(d: Dataset, q: Question, bundle: dict, experiment: Experiment) -> dict:
    result = {
        "rubric": RUBRIC_VERSION,
        "strict_success": None,
        "judgment": "unjudged",
        "reasons": [],
        "boundary_violations": [],
        "matched_units": [],
        "evidence_recall": None,
        "complete_evidence": None,
        "answer_atom_coverage": None,
    }
    status = bundle.get("status")
    if status == "blocked_dependency":
        result["reasons"] = ["blocked_dependency"]
        return result
    if status in {"failed", "timed_out"}:
        return {**result, "strict_success": False, "judgment": "fail", "reasons": [status]}
    required = {
        "response",
        "receipts",
        "request",
        "host_input",
        "action",
        "answer",
        "before_query_positions",
        "after_query_positions",
        "query_elapsed_ms",
        "empty_positions",
    }
    if status != "succeeded" or not required <= bundle.keys():
        result["reasons"] = ["required_trace_missing"]
        return result
    try:
        return _score(d, q, bundle, experiment, result)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {**result, "strict_success": None, "judgment": "unjudged", "reasons": ["malformed_trace"]}


def _score(d, q, bundle, experiment, result):
    label = d.gold[q.question_id]
    response = bundle["response"]
    prefix = d.prefix(q)
    allowed_ids = {e.fixture_event_id for e in prefix}
    receipts = bundle["receipts"]
    if {r["fixture_event_id"] for r in receipts} != allowed_ids or len(receipts) != len(prefix):
        result["reasons"] = ["receipt_prefix_mismatch"]
        return result
    mapped = {}
    for item in receipts:
        mapped.setdefault(item["receipt"]["event_id"], item["fixture_event_id"])
    live = live_sources(prefix, d.manifest.subjects)
    scope = d.manifest.subjects[q.auth_subject_ref]
    matched, violations, reasons = set(), [], []
    excerpts = response["evidence"]
    if any(bundle["empty_positions"].values()):
        violations.append("namespace_not_empty")
    if bundle["before_query_positions"] != bundle["after_query_positions"]:
        violations.append("query_wrote_memory")
    request = bundle["request"]
    if (
        request["query"] != q.question
        or request["project_id"] != q.project_id
        or request["conversation_id"] != q.conversation_id
        or request["budget"] != experiment.budget.model_dump(mode="json")
    ):
        violations.append("public_request_changed")
    if len(excerpts) > experiment.budget.max_evidence_items:
        reasons.append("evidence_budget_exceeded")
    for evidence in excerpts:
        if evidence["citation"] != f"[memory:{evidence['source_id']}@{evidence['revision']}]":
            violations.append("citation_reference_mismatch")
            continue
        fid = mapped.get(evidence["source_id"])
        if fid is None:
            violations.append("unmapped_source")
            continue
        fixture = d.events[fid]
        source_scope = d.manifest.subjects[fixture.auth_subject_ref]
        if (source_scope.tenant_id, source_scope.project_id) != (scope.tenant_id, scope.project_id):
            violations.append("cross_scope_source")
            continue
        if q.project_id != scope.project_id or "read" not in scope.permissions:
            violations.append("unauthorized_source")
            continue
        if q.conversation_id and q.conversation_id != fixture.payload.conversation_id:
            violations.append("cross_conversation_source")
            continue
        if fid not in live:
            violations.append("deleted_or_superseded_source")
            continue
        if evidence["revision"] != fixture.payload.source_revision:
            violations.append("source_revision_mismatch")
            continue
        text = "\n".join(block.text for block in fixture.payload.content)
        excerpt = evidence["excerpt"]
        if not excerpt or not text.startswith(excerpt):
            violations.append("source_content_mismatch")
            continue
        offset = 0
        for block in fixture.payload.content:
            for unit in d.units.values():
                if (
                    unit.fixture_event_id == fid
                    and unit.block_id == block.block_id
                    and offset + unit.end <= len(excerpt)
                    and excerpt[offset + unit.start : offset + unit.end] == unit.quote
                ):
                    matched.add(unit.unit_id)
            offset += len(block.text) + 1
    assembled = "".join(e["citation"] + "\n" + e["excerpt"] + "\n" for e in excerpts)
    if response["context"] != assembled or bundle["host_input"] != response["context"]:
        violations.append("final_context_mismatch")
    if len(bundle["host_input"].encode("utf-8")) > experiment.budget.max_context_tokens:
        reasons.append("context_budget_exceeded")
    if bundle["answer"] != (bundle["host_input"] if bundle["action"] == "answer" else ""):
        violations.append("extractive_host_output_mismatch")
    if bundle["action"] != label.expected_action:
        reasons.append("wrong_action")
    if bundle["query_elapsed_ms"] > experiment.query_deadline_ms:
        reasons.append("query_deadline_exceeded")
    if any(claim in bundle["answer"] for claim in label.forbidden_claims):
        reasons.append("forbidden_claim")
    if label.gold_evidence_sets:
        result["evidence_recall"] = max(
            len(set(group) & matched) / len(group) for group in label.gold_evidence_sets
        )
        result["complete_evidence"] = any(set(group) <= matched for group in label.gold_evidence_sets)
        if not result["complete_evidence"]:
            reasons.append("incomplete_evidence")
    if label.answer_atoms:
        correct = sum(
            any(text in bundle["answer"] for text in atom.accepted_text)
            and any(set(group) <= matched for group in atom.evidence_sets)
            for atom in label.answer_atoms
        )
        result["answer_atom_coverage"] = correct / len(label.answer_atoms)
        if correct != len(label.answer_atoms):
            reasons.append("missing_or_unsupported_answer_atom")
    success = not reasons and not violations
    return {
        **result,
        "strict_success": success,
        "judgment": "pass" if success else "fail",
        "reasons": sorted(set(reasons)),
        "boundary_violations": sorted(set(violations)),
        "matched_units": sorted(matched),
    }
