import csv
import io
import json
from collections import defaultdict
from pathlib import Path

from fusion_memory.evaluation.dataset import FILES, canonical, digest, load_dataset
from fusion_memory.evaluation.statistics import paired_comparison, summarize


def atomic_write(path: Path, content: str):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def emit_report(root, resolved, dataset, rows, attempts):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["mode"], row["variant_id"])].append(row)
    summary = {f"{mode}/{variant}": summarize(items) for (mode, variant), items in sorted(groups.items())}
    comparisons = {}
    config = resolved["experiment"]
    baseline = config["variants"][0]["variant_id"]
    for mode in config["modes"]:
        for variant in config["variants"][1:]:
            candidate = variant["variant_id"]
            comparisons[f"{mode}/{baseline}->{candidate}"] = paired_comparison(
                groups[(mode, baseline)],
                groups[(mode, candidate)],
                config["bootstrap_repetitions"],
                config["random_seed"],
            )
    violations = sum(bool(r["score"]["boundary_violations"]) for r in rows)
    unjudged = sum(r["score"]["judgment"] == "unjudged" for r in rows)
    coverage = []
    succeeded = [r for r in rows if r["bundle"]["status"] == "succeeded"]
    routes = []
    for row in succeeded:
        response = row["bundle"].get("response")
        evidence = response.get("evidence", []) if isinstance(response, dict) else []
        route_set = set()
        if isinstance(evidence, list):
            for item in evidence:
                values = item.get("retrieval_routes", []) if isinstance(item, dict) else []
                if isinstance(values, list):
                    route_set.update(value for value in values if isinstance(value, str))
        routes.append(route_set)
    measured = set()
    if succeeded:
        measured.add("T02")
    if any(r["bundle"].get("receipts") for r in rows):
        measured.add("T06")
    if any("lexical" in value for value in routes):
        measured.add("T18")
    if any({"lexical", "recent_raw"} <= value for value in routes):
        measured.add("T21")
    available_slices = {"T02", "T06", "T17", "T18", "T21", "T28"}
    for index in range(1, 29):
        ident = f"T{index:02d}"
        coverage.append(
            {
                "technology_id": ident,
                "status": "partial_diagnostic"
                if ident in measured
                else ("not_run" if ident in available_slices else "blocked_dependency"),
                "declared": ident in available_slices,
                "dependency_ready": "partial" if ident in available_slices else False,
                "executed": ident in measured,
                "contract_passed": None,
                "quality_measured": "literal_text_slice" if ident in measured else None,
                "reason": "raw/lexical/context slice only"
                if ident in measured
                else "not executed by this evaluator release; see docs/evaluation.md",
            }
        )
    report = {
        "run_id": resolved["run_id"],
        "lifecycle": "COMPLETED",
        "acceptance": "incomplete",
        "boundary": "trusted local core + deterministic extractive host; not an LLM quality evaluation",
        "summaries": summary,
        "comparisons": comparisons,
        "diagnostics": [
            {
                "question_id": r["question_id"],
                "mode": r["mode"],
                "variant_id": r["variant_id"],
                "status": r["bundle"]["status"],
                "reasons": r["score"]["reasons"],
                "missing_dependencies": r["bundle"].get("missing", []),
                "boundary_violations": r["score"]["boundary_violations"],
            }
            for r in rows
            if r["score"]["strict_success"] is not True
        ],
        "technology_coverage": coverage,
        "gates": {
            "G0": {"status": "incomplete", "reason": "No OS-enforced Gold ACL or human label review proof"},
            "G1": {
                "status": "fail" if violations else "incomplete",
                "observed_violation_cases": violations,
                "reason": "Full P0 engineering suite not executed",
            },
            "G2": {"status": "inconclusive", "reason": "No preregistered quality target or fixed LLM host"},
            "G3": {
                "status": "incomplete",
                "reason": "Simulated worker timing; no production load acceptance",
            },
            "G4": {"status": "incomplete", "reason": "P0/P1 evaluator foundation, not T01-T28 acceptance"},
            "G5": {"status": "incomplete" if unjudged else "pass", "unjudged_cases": unjudged},
        },
        "cost": {"model_calls": 0, "model_cost": 0, "compute_storage_cost": None},
        "limitations": [
            "Synthetic development data; no independent human annotation or business generalization.",
            "Process and payload separation is not an OS sandbox against malicious SUT code.",
            "Operational is discrete-event simulation; received_at is actual execution wall time.",
            "Only final released evidence/context is observable; earlier retrieval stages are unavailable.",
            "Literal quotations test plumbing, not natural-language reasoning, facts or model quality.",
            "Per-case rebuild recovery is local and side-effect free; no remote exactly-once claim.",
        ],
    }
    atomic_write(root / "results.jsonl", "".join(canonical(row) + "\n" for row in rows))
    atomic_write(root / "attempts.jsonl", "".join(canonical(a) + "\n" for a in attempts))
    atomic_write(root / "summary.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(
        [
            "mode",
            "variant",
            "question_id",
            "repetition",
            "execution",
            "judgment",
            "strict_success",
            "evidence_recall",
            "complete_evidence",
            "reasons",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["mode"],
                r["variant_id"],
                r["question_id"],
                r["repetition"],
                r["bundle"]["status"],
                r["score"]["judgment"],
                r["score"]["strict_success"],
                r["score"]["evidence_recall"],
                r["score"]["complete_evidence"],
                ";".join(r["score"]["reasons"] + r["score"]["boundary_violations"]),
            ]
        )
    atomic_write(root / "results.csv", csv_buffer.getvalue())
    lines = [
        "# Fusion Memory Eval 回测报告",
        "",
        f"运行：`{resolved['run_id']}`；数据：`{dataset.manifest.dataset_id}@{dataset.manifest.revision}`。",
        "",
        "生命周期：COMPLETED。整体验收：**incomplete**。本报告为真实本地核心的合成开发集诊断。",
        "固定宿主为原样摘录上下文的确定性程序；以下严格成功率仅适用于 literal-extractive-v1 契约。",
        "",
        "| 模式/方案 | 严格通过/应测 | 已判定/应测 | 依赖未就绪 | 违规题数 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for key, s in summary.items():
        lines.append(
            f"| {key} | {s['strict_success']['numerator']}/{s['scheduled']} | "
            f"{s['judged']}/{s['scheduled']} | {s['blocked_dependency']} | "
            f"{s['boundary_violation_cases']} |"
        )
    lines += [
        "",
        "capability：重建允许前缀并等待加工完成。operational：固定到达时间与 FIFO 加工耗时仿真。",
        "两种模式分别统计；查询耗时为实测，仿真加工耗时不作为生产性能。",
        "",
        "## 配对比较",
        "",
    ]
    for key, value in comparisons.items():
        lines.append(
            f"- {key}：Δ={value['question_weighted_delta']}；"
            f"簇数={value['clusters']}；95% CI={value['paired_cluster_bootstrap_95ci']}；"
            f"改善 {value['improvements']}，退步 {value['regressions']}，"
            f"未判定配对 {value['unjudged_pairs']}。结论 inconclusive。"
        )
    lines += ["", "## 未通过与未执行", ""]
    for item in report["diagnostics"]:
        lines.append(
            f"- {item['mode']}/{item['variant_id']}/{item['question_id']}：{item['status']}；"
            f"{', '.join(item['reasons'] + item['missing_dependencies'] + item['boundary_violations'])}。"
        )
    lines += ["", "## 验收门槛", ""]
    for gate, value in report["gates"].items():
        lines.append(f"- {gate}：{value['status']}；{value.get('reason', '逐题输出与评分已封存')}。")
    lines += [
        "",
        "## 验证边界与复现",
        "",
        "Gold 仅由父评测进程读取；SUT 收到公开 DTO，使用独立临时库。当前没有 OS ACL/容器强隔离。",
        "原始回执、实际请求、证据包、宿主输入、评分原因见 results.jsonl；尝试记录见 attempts.jsonl。",
        "manifest、源码/依赖摘要和数据文件均冻结；artifact-manifest.json 提供产物 SHA-256。",
        "T01—T28 覆盖矩阵见 summary.json；未执行项不记为通过。模型调用次数为 0，计算/存储成本未计量。",
        "",
        "复现命令：`fm-eval resume --run-dir <本报告目录>`。校验：`fm-eval verify --run-dir <目录>`。",
        "",
    ]
    atomic_write(root / "report.md", "\n".join(lines))
    seal_artifacts(root)
    return report


def artifact_paths():
    return [
        "resolved-manifest.json",
        "results.jsonl",
        "attempts.jsonl",
        "summary.json",
        "results.csv",
        "report.md",
        "dataset/manifest.json",
    ] + [f"dataset/{name}" for name in FILES]


def seal_artifacts(root):
    paths = artifact_paths()
    hashes = {name: digest((root / name).read_bytes()) for name in paths}
    atomic_write(root / "artifact-manifest.json", json.dumps(hashes, indent=2) + "\n")


def verify_artifacts(root):
    root = Path(root).resolve()
    hashes = json.loads((root / "artifact-manifest.json").read_text(encoding="utf-8"))
    if set(hashes) != set(artifact_paths()):
        raise ValueError("Artifact manifest does not cover the required outputs")
    for name, sha in hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or digest(path.read_bytes()) != sha:
            raise ValueError(f"Artifact hash mismatch: {name}")
    load_dataset(root / "dataset")
    return {"status": "verified", "artifacts": len(hashes)}
