import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fusion_memory.evaluation.contracts import PublicCase
from fusion_memory.evaluation.dataset import digest


def run_case(case: PublicCase, timeout_seconds: int) -> dict:
    """SDK adapter to the actual core, separated by an allowlisted subprocess pipe.

    This is process/state separation, not an OS sandbox against malicious code.
    No source dataset path, evaluator directory, Gold label or production credential is passed.
    """
    payload = case.model_dump_json().encode("utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "LANG", "LC_ALL"}
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["PYTHONIOENCODING"] = "utf-8"
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="fusion-eval-process-") as directory:
        # The parent owns both cwd and TEMP so process timeout also permits complete cleanup.
        env["TEMP"] = env["TMP"] = directory
        try:
            process = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "fusion_memory.evaluation.worker"],
                input=payload,
                capture_output=True,
                cwd=directory,
                env=env,
                timeout=timeout_seconds,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if process.returncode:
                result = {
                    "status": "failed",
                    "error_code": "sut_process_failed",
                    "exit_code": process.returncode,
                    "stderr_sha256": digest(process.stderr),
                }
            else:
                import json

                result = json.loads(process.stdout)
                if not isinstance(result, dict) or "status" not in result:
                    raise ValueError("Malformed SUT response")
        except subprocess.TimeoutExpired:
            result = {"status": "timed_out", "error_code": "case_deadline_exceeded"}
        except (ValueError, UnicodeError):
            result = {"status": "failed", "error_code": "malformed_sut_response"}
    return {
        **result,
        "public_input_sha256": digest(payload),
        "cleanup": "disposed",
        "case_elapsed_ms": (time.perf_counter() - start) * 1000,
    }
