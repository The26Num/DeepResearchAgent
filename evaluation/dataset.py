import json
from pathlib import Path

from evaluation.models import BenchmarkDataset


def load_benchmark(path: str | Path) -> BenchmarkDataset:
    return BenchmarkDataset.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def select_cases(dataset: BenchmarkDataset, *, case_id: str | None = None, limit: int | None = None):
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer.")
    cases = [item for item in dataset.cases if case_id is None or item.case_id == case_id]
    if not cases:
        raise ValueError(f"Unknown benchmark case: {case_id}.")
    return cases[:limit] if limit is not None else cases
