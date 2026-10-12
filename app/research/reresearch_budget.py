"""A thread-safe, run-local hard limit on supplemental model requests only."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock


class ReResearchBudgetExhausted(RuntimeError):
    pass


class ReResearchCallBudget:
    def __init__(self, maximum: int, *, on_reserve=None):
        self.maximum = maximum
        self.calls = 0
        self.by_stage: Counter = Counter()
        self._closed = False
        self._lock = Lock()
        self._on_reserve = on_reserve

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
            try:
                if self._on_reserve is not None:
                    # The orchestrator persists this permit before the HTTP call.
                    self._on_reserve({"maximum": self.maximum, "calls": self.calls,
                                      "by_stage": dict(self.by_stage)})
            except BaseException:
                self.calls -= 1
                self.by_stage[stage] -= 1
                raise

    def snapshot(self) -> dict:
        with self._lock:
            return {"maximum": self.maximum, "calls": self.calls, "by_stage": dict(self.by_stage)}

    @classmethod
    def restore(cls, data: dict, *, on_reserve=None) -> "ReResearchCallBudget":
        maximum, calls, stages = data["maximum"], data["calls"], data["by_stage"]
        if (type(maximum) is not int or type(calls) is not int or not 0 <= calls <= maximum <= 100
                or any(type(value) is not int or value < 0 for value in stages.values())
                or sum(stages.values()) != calls):
            raise ValueError("Invalid persisted Re-Research budget.")
        budget = cls(maximum, on_reserve=on_reserve)
        budget.calls = calls
        budget.by_stage = Counter(stages)
        return budget

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
