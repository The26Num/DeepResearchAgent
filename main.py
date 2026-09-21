import sys

from app.research.orchestrator import ResearchOrchestrator


def main() -> int:
    query = " ".join(sys.argv[1:]).strip() or input("Research question: ").strip()
    if not query:
        print("A research question is required.", file=sys.stderr)
        return 2

    try:
        ResearchOrchestrator().run(query)
    except Exception as exc:
        print(f"Research failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
