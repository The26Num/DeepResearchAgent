"""Phase 7 benchmark CLI. Normal research CLI remains in main.py."""

import argparse
import sys

from evaluation.runner import EvaluationRunner


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate the research agent against a versioned JSON benchmark.")
    parser.add_argument("benchmark")
    parser.add_argument("--mode", choices=("full", "baseline", "compare"), default="full")
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--judge", action=argparse.BooleanOptionalAction, default=False,
                        help="Enable one evaluation-only model call per completed report (default: disabled).")
    parser.add_argument("--db-path")
    parser.add_argument("--output-dir", default="workspace/evaluations")
    parser.add_argument("--max-concurrency", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        runner = EvaluationRunner(output_root=args.output_dir, db_path=args.db_path,
                                  max_concurrency=args.max_concurrency, judge_enabled=args.judge)
        summary = runner.run(args.benchmark, mode=args.mode, case_id=args.case_id, limit=args.limit)
        return 1 if summary["cases_failed"] else 0
    except KeyboardInterrupt:
        print("Evaluation interrupted. Saved Research Run IDs can be resumed with main.py --resume.", file=sys.stderr)
        return 130
    except Exception as error:
        # CLI errors pass through the same secret scrubber as artifacts.
        from evaluation.output import Redactor
        print("Evaluation failed: " + Redactor().clean(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
