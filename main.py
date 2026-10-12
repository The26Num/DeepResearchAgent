import argparse
import sys

from app.config import get_settings
from app.persistence.store import ResearchRunStore
from app.research.orchestrator import ResearchOrchestrator


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run or resume an evidence-grounded research pipeline.")
    parser.add_argument("question", nargs="*")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", metavar="RUN_ID")
    mode.add_argument("--list-runs", action="store_true")
    parser.add_argument("--db-path", help="Override RESEARCH_DB_PATH (default: workspace/research.db)")
    args = parser.parse_args(argv)
    if args.question and (args.resume or args.list_runs):
        parser.error("question cannot be combined with --resume or --list-runs")

    try:
        settings = get_settings()
        store = ResearchRunStore(args.db_path or settings.research_db_path)
        if args.list_runs:
            print("RUN ID\tSTATUS\tUPDATED\tGOAL")
            for run in store.list_runs():
                print(f"{run.run_id}\t{run.status.value}\t{run.updated_at}\t{run.goal}")
            return 0
        orchestrator = ResearchOrchestrator(run_store=store)
        if args.resume:
            orchestrator.resume(args.resume)
        else:
            query = " ".join(args.question).strip() or input("Research question: ").strip()
            if not query:
                print("A research question is required.", file=sys.stderr)
                return 2
            orchestrator.run(query)
    except KeyboardInterrupt:
        print("Research interrupted; resume using the printed Run ID.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Research failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
