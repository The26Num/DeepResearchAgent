"""A thread-safe, run-local hard limit on supplemental model requests only."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock


class ReResearchBudgetExhausted(RuntimeError):
    pass


class ReResearchCallBudget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.calls = 0
        self.by_stage: Counter = Counter()
        self._closed = False
        self._lock = Lock()

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._closed or self.calls >= self.maximum

    def reserve(self, stage: str) -> None:
        with self._lock:
            if self._closed or self.calls >= self.maximum:
                raise ReResearchBudgetExhausted("Re-Research budget exhausted; no further model requests allowed.")
            self.calls += 1
            self.by_stage[stage] += 1

    def close(self) -> None:
        # Late workers from timed-out tasks must not spend further calls.
        with self._lock:
            self._closed = True


_ACTIVE_BUDGET: ContextVar[ReResearchCallBudget | None] = ContextVar("reresearch_call_budget", default=None)


def active_call_budget() -> ReResearchCallBudget | None:
    return _ACTIVE_BUDGET.get()


@contextmanager
def use_call_budget(budget: ReResearchCallBudget | None):
    token = _ACTIVE_BUDGET.set(budget)
    try:
        yield
    finally:
        _ACTIVE_BUDGET.reset(token)
