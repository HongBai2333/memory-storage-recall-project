import random
from collections import defaultdict
from statistics import mean


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def summarize(rows):
    n = len(rows)
    judged = [r for r in rows if r["score"]["strict_success"] is not None]
    answerable = [r for r in rows if r["expected_action"] == "answer"]
    return {
        "scheduled": n,
        "executed": sum(r["bundle"]["status"] == "succeeded" for r in rows),
        "judged": len(judged),
        "blocked_dependency": sum(r["bundle"]["status"] == "blocked_dependency" for r in rows),
        "strict_success": ratio(sum(r["score"]["strict_success"] is True for r in rows), n),
        "judgment_coverage": ratio(len(judged), n),
        "complete_evidence": ratio(
            sum(r["score"]["complete_evidence"] is True for r in answerable), len(answerable)
        ),
        "evidence_judgment_coverage": ratio(
            sum(r["score"]["complete_evidence"] is not None for r in answerable), len(answerable)
        ),
        "boundary_violation_cases": sum(bool(r["score"]["boundary_violations"]) for r in rows),
        "failed_or_timed_out": sum(r["bundle"]["status"] in {"failed", "timed_out"} for r in rows),
    }


def paired_comparison(baseline, candidate, repetitions=2000, seed=101):
    def indexed(rows):
        result = {(r["question_id"], r["repetition"]): r for r in rows}
        if len(result) != len(rows):
            raise ValueError("Duplicate paired case")
        return result

    a, b = indexed(baseline), indexed(candidate)
    if a.keys() != b.keys():
        raise ValueError("Paired comparison requires the same question/repetition population")
    clusters = defaultdict(list)
    counts = {"both_pass": 0, "both_fail": 0, "regressions": 0, "improvements": 0}
    changes, unjudged = [], 0
    for key in sorted(a):
        left, right = a[key], b[key]
        if left["mode"] != right["mode"] or left["cluster_id"] != right["cluster_id"]:
            raise ValueError("Paired mode and cluster bindings differ")
        x, y = left["score"]["strict_success"], right["score"]["strict_success"]
        if x is None or y is None:
            unjudged += 1
            continue
        diff = int(y) - int(x)
        clusters[left["cluster_id"]].append(diff)
        category = (
            "both_pass"
            if x and y
            else "both_fail"
            if not x and not y
            else ("improvements" if y else "regressions")
        )
        counts[category] += 1
        if diff:
            changes.append({"question_id": key[0], "repetition": key[1], "delta": diff})
    grouped = list(clusters.values())
    differences = [value for group in grouped for value in group]
    interval = None
    if len(grouped) >= 2 and not unjudged:
        rng = random.Random(seed)
        samples = sorted(
            mean(v for group in rng.choices(grouped, k=len(grouped)) for v in group)
            for _ in range(repetitions)
        )
        interval = [samples[int((repetitions - 1) * 0.025)], samples[int((repetitions - 1) * 0.975)]]
    return {
        **counts,
        "scheduled_pairs": len(a),
        "judged_pairs": len(differences),
        "unjudged_pairs": unjudged,
        "clusters": len(grouped),
        "question_weighted_delta": mean(differences) if differences else None,
        "cluster_equal_delta": mean(mean(group) for group in grouped) if grouped else None,
        "paired_cluster_bootstrap_95ci": interval,
        "bootstrap_seed": seed,
        "bootstrap_repetitions": repetitions,
        "changes": changes,
        "conclusion": "inconclusive",
        "warning": "Development diagnostics; few clusters and unreviewed labels cannot establish gains.",
    }
