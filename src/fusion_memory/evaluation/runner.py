import importlib.metadata
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fusion_memory.evaluation import EVALUATOR_VERSION
from fusion_memory.evaluation.adapter import run_case
from fusion_memory.evaluation.contracts import Experiment, PublicCase
from fusion_memory.evaluation.dataset import canonical, digest, load_dataset
from fusion_memory.evaluation.reporting import atomic_write, emit_report
from fusion_memory.evaluation.scoring import score
from fusion_memory.evaluation.store import RunStore
from fusion_memory.runtime import RootLock


def fingerprint():
    package_root = Path(__file__).resolve().parents[1]
    files = {
        str(p.relative_to(package_root)).replace("\\", "/"): digest(p.read_bytes())
        for p in sorted(package_root.rglob("*.py"))
    }
    dependencies = {
        d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]
    }
    return {
        "source_sha256": digest(canonical(files).encode()),
        "source_files": files,
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": dict(sorted(dependencies.items())),
        "evaluator_version": EVALUATOR_VERSION,
    }


def create_run(dataset_root: Path, experiment_path: Path, run_dir: Path):
    dataset = load_dataset(dataset_root)
    experiment = Experiment.model_validate_json(experiment_path.read_bytes())
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    # Freeze exact bytes, then validate again to catch a concurrent input edit.
    for name in ["manifest.json", *dataset.manifest.files]:
        target = run_dir / "dataset" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((dataset_root / name).read_bytes())
    frozen = load_dataset(run_dir / "dataset")
    if frozen.manifest_hash != dataset.manifest_hash:
        raise ValueError("Dataset changed while freezing")
    resolved = {
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "experiment": experiment.model_dump(mode="json"),
        "dataset_sha256": dataset.manifest_hash,
        "implementation": fingerprint(),
        "profile": "trusted-local-development",
    }
    resolved["manifest_sha256"] = digest(canonical(resolved).encode())
    atomic_write(run_dir / "resolved-manifest.json", json.dumps(resolved, ensure_ascii=False, indent=2))
    return resume_run(run_dir)


def read_frozen(run_dir: Path):
    resolved = json.loads((run_dir / "resolved-manifest.json").read_text(encoding="utf-8"))
    unsigned = {k: v for k, v in resolved.items() if k != "manifest_sha256"}
    if digest(canonical(unsigned).encode()) != resolved["manifest_sha256"]:
        raise ValueError("Frozen experiment manifest was modified")
    if fingerprint() != resolved["implementation"]:
        raise ValueError("Code/dependencies/platform changed; create a new run instead of resuming")
    dataset = load_dataset(run_dir / "dataset")
    if dataset.manifest_hash != resolved["dataset_sha256"]:
        raise ValueError("Frozen dataset manifest was modified")
    return resolved, dataset, Experiment.model_validate(resolved["experiment"])


def resume_run(run_dir: Path):
    run_dir = run_dir.resolve()
    lock = RootLock(run_dir)
    store = None
    try:
        resolved, dataset, experiment = read_frozen(run_dir)
        store = RunStore(run_dir / "tasks.sqlite")
        store.recover()
        store.schedule(
            [
                {
                    "run_id": resolved["run_id"],
                    "manifest_hash": resolved["manifest_sha256"],
                    "variant_id": variant.variant_id,
                    "mode": mode,
                    "repetition": repetition,
                    "episode_id": question.episode_id,
                    "question_id": question.question_id,
                }
                for mode in experiment.modes
                for repetition in range(experiment.repetitions)
                for question in dataset.questions.values()
                for variant in experiment.variants
            ]
        )
        store.state("REPLAYING")
        variants = {v.variant_id: v for v in experiment.variants}
        for row in store.rows():
            if row["state"] == "sealed":
                continue
            binding = json.loads(row["binding"])
            q = dataset.questions[binding["question_id"]]
            public = PublicCase(
                subjects=dataset.manifest.subjects,
                events=dataset.prefix(q),
                question=q,
                variant=variants[binding["variant_id"]],
                mode=binding["mode"],
                budget=experiment.budget,
                query_deadline_ms=experiment.query_deadline_ms,
            )
            fence = store.claim(row["case_id"], experiment.case_timeout_seconds + 30)
            bundle = run_case(public, experiment.case_timeout_seconds)
            store.seal(row["case_id"], fence, bundle)
        store.state("EXECUTION_SEALED")
        # Scoring can be resumed solely from sealed outputs; SUT calls are never repeated for a low score.
        read_frozen(run_dir)
        store.state("SCORING")
        results = []
        for row in store.rows():
            binding = json.loads(row["binding"])
            q = dataset.questions[binding["question_id"]]
            bundle = json.loads(row["result"])
            results.append(
                {
                    **binding,
                    "case_id": row["case_id"],
                    "attempt": row["fence"],
                    "cluster_id": dataset.gold[q.question_id].cluster_id,
                    "expected_action": dataset.gold[q.question_id].expected_action,
                    "tags": dataset.gold[q.question_id].tags,
                    "bundle": bundle,
                    "score": score(dataset, q, bundle, experiment),
                }
            )
        store.state("REPORTING")
        report = emit_report(run_dir, resolved, dataset, results, store.attempts())
        store.state("COMPLETED")
        return report
    except KeyboardInterrupt:
        if store:
            store.state("PAUSED")
        raise
    except Exception:
        if store:
            store.state("FAILED")
        raise
    finally:
        lock.close()
