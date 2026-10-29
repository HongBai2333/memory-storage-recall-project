import argparse
import json
import sys
from pathlib import Path

from fusion_memory.evaluation.contracts import (
    DatasetManifest,
    EvidenceUnit,
    Experiment,
    FixtureEvent,
    GoldQuestion,
    PublicCase,
    Question,
)
from fusion_memory.evaluation.dataset import load_dataset
from fusion_memory.evaluation.pilot import build_pilot
from fusion_memory.evaluation.reporting import verify_artifacts
from fusion_memory.evaluation.runner import create_run, resume_run


def main():
    parser = argparse.ArgumentParser(description="Fusion Memory Eval: trusted local development evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    pilot = sub.add_parser("init-pilot", help="Create synthetic development data in a NEW directory")
    pilot.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--dataset", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--experiment", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    for command in ("resume", "verify"):
        sub.add_parser(command).add_argument("--run-dir", type=Path, required=True)
    sub.add_parser("export-contracts").add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command in {"init-pilot", "validate"}:
            d = build_pilot(args.output) if args.command == "init-pilot" else load_dataset(args.dataset)
            result = {
                "status": "valid",
                "dataset": d.manifest.dataset_id,
                "questions": len(d.questions),
                "episodes": len(d.episodes),
                "manifest_sha256": d.manifest_hash,
                "annotation": d.manifest.annotation_status,
            }
        elif args.command == "run":
            report = create_run(args.dataset, args.experiment, args.output)
            result = {
                "run_id": report["run_id"],
                "acceptance": report["acceptance"],
                "report": str((args.output / "report.md").resolve()),
            }
        elif args.command == "resume":
            report = resume_run(args.run_dir)
            result = {"run_id": report["run_id"], "acceptance": report["acceptance"]}
        elif args.command == "verify":
            result = verify_artifacts(args.run_dir)
        else:
            args.output.mkdir(parents=True, exist_ok=True)
            for model in (
                DatasetManifest,
                Experiment,
                FixtureEvent,
                Question,
                EvidenceUnit,
                GoldQuestion,
                PublicCase,
            ):
                (args.output / f"{model.__name__}.schema.json").write_text(
                    json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            result = {"status": "exported", "output": str(args.output)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Paused. Resume the same frozen run with fm-eval resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
