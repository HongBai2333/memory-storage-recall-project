import json
import sqlite3

import pytest
from pydantic import ValidationError

from fusion_memory.evaluation.adapter import run_case
from fusion_memory.evaluation.contracts import Experiment, PublicCase, Variant
from fusion_memory.evaluation.dataset import canonical, digest, load_dataset
from fusion_memory.evaluation.pilot import build_pilot
from fusion_memory.evaluation.reporting import verify_artifacts
from fusion_memory.evaluation.runner import create_run, resume_run
from fusion_memory.evaluation.scoring import score
from fusion_memory.evaluation.statistics import paired_comparison, ratio, summarize
from fusion_memory.evaluation.store import RunStore
from fusion_memory.evaluation.worker import execute


@pytest.fixture
def pilot(tmp_path):
    path = tmp_path / "夹具 data"
    dataset = build_pilot(path)
    experiment = Experiment.model_validate_json((path / "experiment.json").read_bytes())
    return path, dataset, experiment


def public_case(dataset, experiment, qid="budget-immediate", mode="operational", variant=None):
    q = dataset.questions[qid]
    return PublicCase(
        subjects=dataset.manifest.subjects,
        events=dataset.prefix(q),
        question=q,
        variant=variant or experiment.variants[1],
        mode=mode,
        budget=experiment.budget,
        query_deadline_ms=experiment.query_deadline_ms,
    )


def rewrite_file(root, name, values):
    payload = ("".join(canonical(value) + "\n" for value in values)).encode()
    (root / name).write_bytes(payload)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = digest(payload)
    (root / "manifest.json").write_text(canonical(manifest), encoding="utf-8")


def test_public_gold_contracts_and_frozen_hashes(pilot):
    path, dataset, experiment = pilot
    assert len(dataset.questions) == 19
    payload = public_case(dataset, experiment).model_dump(mode="json")
    assert not {"gold", "tags", "expected_action", "answer_atoms"} & payload["question"].keys()
    payload["question"]["gold"] = {"answer": "150000"}
    with pytest.raises(ValidationError):
        PublicCase.model_validate(payload)
    (path / "public" / "questions.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_dataset(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "future_prefix",
        "naive_time",
        "duplicate_seq",
        "future_gold",
        "wrong_quote",
        "deleted_gold",
        "unknown_subject",
        "empty_atom",
    ],
)
def test_dataset_rejects_invalid_time_identity_and_gold(pilot, mutation):
    root, d, _ = pilot
    questions = [q.model_dump(mode="json") for q in d.questions.values()]
    units = [u.model_dump(mode="json") for u in d.units.values()]
    labels = [g.model_dump(mode="json") for g in d.gold.values()]
    if mutation == "future_prefix":
        questions[0]["cutoff_replay_seq"] = 20
    elif mutation == "naive_time":
        questions[0]["query_at"] = "2026-10-01T09:00:06"
    elif mutation == "duplicate_seq":
        questions[0]["replay_seq"] = 10
    elif mutation == "future_gold":
        labels[0]["gold_evidence_sets"] = [["budget-2#b1"]]
    elif mutation == "wrong_quote":
        units[0]["quote"] = "伪造引用"
    elif mutation == "deleted_gold":
        index = next(i for i, q in enumerate(questions) if q["question_id"] == "budget-deleted")
        labels[index] = {**labels[0], "question_id": "budget-deleted"}
    elif mutation == "unknown_subject":
        questions[0]["auth_subject_ref"] = "mallory"
    elif mutation == "empty_atom":
        labels[0]["answer_atoms"][0]["accepted_text"] = [""]
    rewrite_file(root, "public/questions.jsonl", questions)
    rewrite_file(root, "evaluator_only/evidence_units.jsonl", units)
    rewrite_file(root, "evaluator_only/gold_questions.jsonl", labels)
    with pytest.raises(ValueError):
        load_dataset(root)


def test_manifest_rejects_extra_paths(pilot):
    path, _, _ = pilot
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["../../other.jsonl"] = "00"
    (path / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="allowlist"):
        load_dataset(path)


@pytest.mark.parametrize("mode", ["capability", "operational"])
@pytest.mark.parametrize(
    "qid",
    [
        "budget-before",
        "budget-immediate",
        "budget-isolation",
        "budget-duplicate",
        "budget-deleted",
        "budget-other-tenant",
        "project-denied",
        "combined-evidence",
        "alternative-evidence",
        "block-content",
        "conversation-isolation",
        "no-evidence",
        "japanese-model",
    ],
)
def test_real_core_prefix_replay(pilot, tmp_path, mode, qid):
    _, d, experiment = pilot
    public = public_case(d, experiment, qid, mode)
    bundle = execute(public, tmp_path / "sut")
    assert bundle["status"] == "succeeded", bundle
    scored = score(d, public.question, bundle, experiment)
    assert scored["strict_success"] is True, scored
    if qid == "budget-immediate":
        assert bundle["pending_processing_jobs"] == (1 if mode == "operational" else 0)
        routes = bundle["response"]["evidence"][0]["retrieval_routes"]
        assert ("recent_raw" if mode == "operational" else "lexical") in routes
    if qid == "budget-duplicate":
        receipts = bundle["receipts"]
        retry = next(r["receipt"] for r in receipts if r["fixture_event_id"] == "budget-retry")
        original = next(r["receipt"] for r in receipts if r["fixture_event_id"] == "budget-2")
        assert retry["status"] == "duplicate"
        assert retry["event_id"] == original["event_id"]


def test_unavailable_capability_not_counted_as_pass(pilot, tmp_path):
    _, d, experiment = pilot
    public = public_case(d, experiment, "budget-history-blocked")
    bundle = execute(public, tmp_path / "sut")
    assert bundle["status"] == "blocked_dependency"
    assert bundle["missing"] == ["bitemporal_query"]
    assert score(d, public.question, bundle, experiment)["strict_success"] is None
    assert not (tmp_path / "sut").exists()


def test_strong_consistency_pending_is_not_task_success(pilot, tmp_path):
    _, d, experiment = pilot
    public = public_case(d, experiment)
    q = public.question.model_copy(
        update={"required_capabilities": ("lexical",), "consistency_mode": "at_least"}
    )
    public = public.model_copy(update={"question": q})
    bundle = execute(public, tmp_path / "sut")
    assert bundle["action"] == "pending"
    assert score(d, q, bundle, experiment)["strict_success"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "id_only",
        "wrong_revision",
        "wrong_text",
        "wrong_scope",
        "missing_trace",
        "trimmed_host",
        "future_id",
        "forbidden",
        "deadline",
        "writeback",
    ],
)
def test_scorer_mutation_detection(pilot, tmp_path, mutation):
    _, d, experiment = pilot
    public = public_case(d, experiment)
    bundle = execute(public, tmp_path / "sut")
    assert score(d, public.question, bundle, experiment)["strict_success"] is True
    evidence = bundle["response"]["evidence"][0]
    if mutation == "id_only":
        evidence["excerpt"] = "项目"
    elif mutation == "wrong_revision":
        evidence["revision"] = "999"
    elif mutation == "wrong_text":
        evidence["excerpt"] = "虚构内容150000"
    elif mutation == "wrong_scope":
        bundle["request"]["project_id"] = "foreign"
    elif mutation == "missing_trace":
        del bundle["host_input"]
    elif mutation == "trimmed_host":
        bundle["host_input"] = ""
    elif mutation == "future_id":
        evidence["source_id"] = "unmapped-future"
    elif mutation == "forbidden":
        bundle["answer"] += "100000"
    elif mutation == "deadline":
        bundle["query_elapsed_ms"] = experiment.query_deadline_ms + 1
    elif mutation == "writeback":
        bundle["after_query_positions"]["alice"] += 1
    result = score(d, public.question, bundle, experiment)
    assert result["strict_success"] is not True
    if mutation == "missing_trace":
        assert result["judgment"] == "unjudged"


def test_context_budget_drops_content_even_if_source_id_matches(pilot, tmp_path):
    _, d, experiment = pilot
    experiment = experiment.model_copy(
        update={"budget": experiment.budget.model_copy(update={"max_context_tokens": 90})}
    )
    public = public_case(d, experiment, "block-content")
    bundle = execute(public, tmp_path / "sut")
    result = score(d, public.question, bundle, experiment)
    assert bundle["response"]["evidence"]
    assert result["complete_evidence"] is False
    assert result["strict_success"] is False


def test_subprocess_public_boundary_and_cleanup(pilot):
    _, d, experiment = pilot
    public = public_case(d, experiment)
    result = run_case(public, 30)
    assert result["status"] == "succeeded"
    assert result["cleanup"] == "disposed"
    assert result["public_input_sha256"] == digest(public.model_dump_json().encode())
    assert "gold" not in result and "tags" not in result


def test_durable_fencing_recovery_and_tamper_detection(tmp_path):
    store = RunStore(tmp_path / "tasks.sqlite")
    store.schedule([{"q": "one"}])
    case_id = store.rows()[0]["case_id"]
    first = store.claim(case_id, 30)
    store.recover()
    second = store.claim(case_id, 30)
    with pytest.raises(ValueError, match="stale"):
        store.seal(case_id, first, {"status": "succeeded"})
    store.seal(case_id, second, {"status": "succeeded"})
    assert [row["state"] for row in store.attempts()] == ["interrupted", "sealed"]
    with pytest.raises(ValueError):
        store.seal(case_id, second, {"status": "failed"})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE cases SET result='{}'")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.rows()


def test_expired_lease_cannot_commit(tmp_path):
    store = RunStore(tmp_path / "tasks.sqlite")
    store.schedule([{"q": "one"}])
    case_id = store.rows()[0]["case_id"]
    token = store.claim(case_id, -1)
    with pytest.raises(ValueError, match="Expired"):
        store.seal(case_id, token, {})


def test_statistics_denominators_and_cluster_pairing():
    assert ratio(0, 0)["value"] is None
    baseline, candidate = [], []
    for repetition in range(10):
        for question in ("q1", "q2"):
            row = {
                "question_id": question,
                "repetition": repetition,
                "mode": "capability",
                "cluster_id": "one-episode",
                "score": {"strict_success": False},
            }
            baseline.append(row)
            candidate.append({**row, "score": {"strict_success": True}})
    result = paired_comparison(baseline, candidate, 100)
    assert result["clusters"] == 1 and result["scheduled_pairs"] == 20
    assert result["paired_cluster_bootstrap_95ci"] is None
    candidate[0]["score"]["strict_success"] = None
    assert paired_comparison(baseline, candidate, 100)["unjudged_pairs"] == 1
    with pytest.raises(ValueError, match="same"):
        paired_comparison(baseline, candidate[:-1])


def test_run_resume_keeps_sealed_outputs_and_versions(pilot, tmp_path, monkeypatch):
    path, _, experiment = pilot
    # Exercise the orchestration cheaply; SDK uses real isolated stores just like subprocess worker.
    count = 0

    def local_adapter(public, timeout):
        nonlocal count
        count += 1
        return execute(public, tmp_path / f"sut-{count}")

    monkeypatch.setattr("fusion_memory.evaluation.runner.run_case", local_adapter)
    config = experiment.model_copy(
        update={"modes": ("capability",), "variants": (Variant(variant_id="lexical"),)}
    )
    (path / "experiment.json").write_text(config.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "运行结果"
    result = create_run(path, path / "experiment.json", run_dir)
    assert count == 19
    assert result["summaries"]["capability/lexical"]["blocked_dependency"] == 2
    assert result["acceptance"] == "incomplete"
    assert verify_artifacts(run_dir)["status"] == "verified"
    first_results = (run_dir / "results.jsonl").read_bytes()
    resume_run(run_dir)
    assert count == 19
    assert (run_dir / "results.jsonl").read_bytes() == first_results
    resolved = json.loads((run_dir / "resolved-manifest.json").read_text(encoding="utf-8"))
    resolved["experiment"]["query_deadline_ms"] += 1
    (run_dir / "resolved-manifest.json").write_text(canonical(resolved), encoding="utf-8")
    with pytest.raises(ValueError, match="modified"):
        resume_run(run_dir)


@pytest.mark.parametrize("mutation", ["revision_conflict", "stale_revision", "cluster_split"])
def test_reject_invalid_revision_and_statistical_clusters(pilot, mutation):
    path, d, _ = pilot
    if mutation == "cluster_split":
        values = [g.model_dump(mode="json") for g in d.gold.values()]
        values[0]["cluster_id"] = "fake-independent-sample"
        rewrite_file(path, "evaluator_only/gold_questions.jsonl", values)
    else:
        values = [e.model_dump(mode="json") for e in d.events.values()]
        if mutation == "revision_conflict":
            item = next(e for e in values if e["fixture_event_id"] == "budget-retry")
            item["payload"]["content"][0]["text"] = "Changed duplicate"
        else:
            item = next(e for e in values if e["fixture_event_id"] == "budget-2")
            item["payload"]["source_sequence"] = 0
        rewrite_file(path, "public/events.jsonl", values)
    with pytest.raises(ValueError):
        load_dataset(path)


def test_new_message_business_update_exposes_current_core_host_gap(pilot, tmp_path):
    _, d, experiment = pilot
    case = public_case(d, experiment, "business-new-message-update")
    bundle = execute(case, tmp_path / "sut")
    result = score(d, case.question, bundle, experiment)
    assert result["complete_evidence"] is True
    assert result["strict_success"] is False
    assert result["reasons"] == ["forbidden_claim"]
    assert {route for e in bundle["response"]["evidence"] for route in e["retrieval_routes"]} == {
        "lexical",
        "recent_raw",
    }


def test_unjudged_evidence_stays_in_population():
    rows = [
        {
            "expected_action": "answer",
            "bundle": {"status": "blocked_dependency"},
            "score": {"strict_success": None, "complete_evidence": None, "boundary_violations": []},
        }
    ]
    result = summarize(rows)
    assert result["complete_evidence"] == {"numerator": 0, "denominator": 1, "value": 0}
    assert result["evidence_judgment_coverage"]["numerator"] == 0


def test_case_population_cannot_change_on_resume(tmp_path):
    store = RunStore(tmp_path / "tasks.sqlite")
    store.schedule([{"q": "one"}])
    with pytest.raises(ValueError, match="population"):
        store.schedule([{"q": "two"}])
    assert len(store.rows()) == 1


def test_incomplete_artifact_manifest_rejected(tmp_path):
    (tmp_path / "artifact-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="required outputs"):
        verify_artifacts(tmp_path)


def test_wrong_citation_cannot_pass_with_correct_quote(pilot, tmp_path):
    _, d, experiment = pilot
    case = public_case(d, experiment)
    bundle = execute(case, tmp_path / "sut")
    response = bundle["response"]
    response["evidence"][0]["citation"] = "[memory:forged@1]"
    response["context"] = "".join(e["citation"] + "\n" + e["excerpt"] + "\n" for e in response["evidence"])
    bundle["answer"] = bundle["host_input"] = response["context"]
    result = score(d, case.question, bundle, experiment)
    assert result["strict_success"] is False
    assert "citation_reference_mismatch" in result["boundary_violations"]


def test_runner_recovers_interrupted_case_without_replaying_sealed_cases(pilot, tmp_path, monkeypatch):
    path, _, experiment = pilot
    config = experiment.model_copy(
        update={"modes": ("capability",), "variants": (Variant(variant_id="lexical"),)}
    )
    (path / "experiment.json").write_text(config.model_dump_json(), encoding="utf-8")
    attempts = 0

    def interrupted(public, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise KeyboardInterrupt
        return execute(public, tmp_path / f"first-{attempts}")

    monkeypatch.setattr("fusion_memory.evaluation.runner.run_case", interrupted)
    run_dir = tmp_path / "interrupted-run"
    with pytest.raises(KeyboardInterrupt):
        create_run(path, path / "experiment.json", run_dir)
    ledger = RunStore(run_dir / "tasks.sqlite")
    assert sum(row["state"] == "sealed" for row in ledger.rows()) == 1
    resumed = 0

    def recovered(public, timeout):
        nonlocal resumed
        resumed += 1
        return execute(public, tmp_path / f"resumed-{resumed}")

    monkeypatch.setattr("fusion_memory.evaluation.runner.run_case", recovered)
    resume_run(run_dir)
    assert resumed == 18
    assert sum(row["state"] == "interrupted" for row in ledger.attempts()) == 1
    assert verify_artifacts(run_dir)["status"] == "verified"
