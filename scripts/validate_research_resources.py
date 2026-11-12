"""Credential-free offline acceptance; preserves small simulated metrics only.

Run from any directory with the project's dev Python environment. This does not
load .env, access the user's research database, or create a provider client.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run the complete offline Python suite.")
    parser.add_argument("--output", type=Path, default=Path("workspace/validations/research-resources"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = ["tests"] if args.all else [
        "tests/test_source_archive.py", "tests/test_archive_workflow.py",
        "tests/test_research_skills.py", "tests/test_resource_limits.py", "tests/test_research_resume_finalization.py",
        "tests/test_estimated_token_quota.py",
        "tests/test_synthesis_context_recovery.py",
        "tests/test_reresearch_allocations.py",
    ]
    with tempfile.TemporaryDirectory(prefix="research-resources-offline-") as temporary:
        sandbox = Path(temporary)
        # Existing fixtures use relative app/evaluation paths. Copy only code
        # and public resources, never credentials, workspace, or SQLite files.
        for directory in ("app", "evaluation"):
            for source in (root / directory).rglob("*"):
                if (source.is_file() and not source.is_symlink() and source.suffix in {".py", ".md", ".json"}
                        and "__pycache__" not in source.parts and not source.name.startswith(".env")):
                    target = sandbox / source.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        env = {**os.environ, "PYTHONPATH": str(sandbox) + os.pathsep + str(root),
               "PYTHONIOENCODING": "utf-8", "LANGSMITH_TRACING": "false"}
        for key in list(env):
            if key.startswith(("MODEL_", "SILICONFLOW_", "VERIFIER_MODEL_", "TAVILY_", "EVAL_",
                               "RESEARCH_DB_PATH", "SOURCE_ARCHIVE_PATH", "LANGSMITH_API_KEY", "LANGCHAIN_")):
                env.pop(key)
        env.pop("RUN_DOCKER_TESTS", None)  # Docker acceptance is a separate opt-in command.
        log_path = output / "pytest.log"
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run([sys.executable, "-m", "pytest", *(str(root / name) for name in selected),
                                     "-q", "--tb=short", "--basetemp", str(sandbox / "test-data")],
                                    cwd=sandbox, env=env, stdout=log, stderr=subprocess.STDOUT)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        summary = {"exit_code": result.returncode, "isolated_cwd": True,
                   "mode": "offline_simulation", "actual_provider_calls": 0,
                   "summary": lines[-1] if lines else ""}
        metrics = list((sandbox / "test-data").rglob("offline-resource-metrics.json"))
        if metrics:
            shutil.copyfile(metrics[0], output / "offline-resource-metrics.json")
            summary["metrics_file"] = "offline-resource-metrics.json"
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("\n".join(lines[-25:]))
        print(f"Offline validation artifacts: {output}")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
