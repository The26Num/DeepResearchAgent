"""A thread-safe, run-local hard limit on supplemental model requests only."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from copy import deepcopy


class ReResearchBudgetExhausted(RuntimeError):
    pass


class ReResearchTaskBudgetExhausted(RuntimeError):
    """A task reached its stage allocation; other tasks/audits retain theirs."""


class ReResearchCallBudget:
    def __init__(self, maximum: int, *, on_reserve=None):
        self.maximum = maximum
        self.calls = 0
        self.by_stage: Counter = Counter()
        self._closed = False
        self._lock = Lock()
        self._on_reserve = on_reserve
        self._allocation = None

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._closed or self.calls >= self.maximum

    def configure_tasks(self, task_ids, *, extraction_calls=1, verification_calls=1, relevance_calls=1):
        """Divide remaining calls before workers start; never enlarge the total."""
        with self._lock:
            if self._allocation is not None:
                if set(task_ids) != set(self._allocation["task_limits"]):
                    raise ValueError("Saved supplemental call allocations do not match the plan.")
                return
            task_ids = list(dict.fromkeys(task_ids))
            if not task_ids:
                return
            remaining = self.maximum - self.calls
            audit_cost = len(task_ids) * (extraction_calls + verification_calls) + relevance_calls
            normal = min(3, (remaining - audit_cost) // len(task_ids))
            if normal < 1:
                raise ReResearchBudgetExhausted("Remaining supplemental budget cannot cover research, extraction, verification and relevance; no workers started.")
            # Unused capacity supports bounded extraction/verification recovery.
            limits = {key: {"researcher": normal, "extraction": extraction_calls, "verification": verification_calls} for key in task_ids}
            spare = remaining - audit_cost - normal * len(task_ids)
            for stage, ceiling in (("extraction", 3), ("verification", 6)):
                while spare and any(item[stage] < ceiling for item in limits.values()):
                    for key in task_ids:
                        if spare and limits[key][stage] < ceiling:
                            limits[key][stage] += 1
                            spare -= 1
            self._allocation = {"task_limits": limits, "task_calls": {key: {} for key in task_ids},
                "global_limits": {"relevance": relevance_calls + spare}, "global_calls": {},
                "base_calls": self.calls, "base_by_stage": dict(self.by_stage)}

    def has_task_allocation(self, task_id):
        with self._lock:
            return self._allocation is not None and task_id in self._allocation["task_limits"]

    def remaining_for(self, stage, task_id=None):
        with self._lock:
            remaining = 0 if self._closed else max(0, self.maximum - self.calls)
            if self._allocation is None:
                return remaining
            if task_id in self._allocation["task_limits"] and stage in self._allocation["task_limits"][task_id]:
                cap = self._allocation["task_limits"][task_id].get(stage, 0)
                used = self._allocation["task_calls"][task_id].get(stage, 0)
            else:
                cap = self._allocation["global_limits"].get(stage, 0)
                used = self._allocation["global_calls"].get(stage, 0)
            return min(remaining, max(0, cap - used))

    def reserve(self, stage: str, *, task_id=None, before_reserve=None) -> None:
        with self._lock:
            if self._closed or self.calls >= self.maximum:
                raise ReResearchBudgetExhausted("Re-Research budget exhausted; no further model requests allowed.")
            scoped_calls = None
            if self._allocation is not None:
                if task_id in self._allocation["task_limits"] and stage in self._allocation["task_limits"][task_id]:
                    limits = self._allocation["task_limits"][task_id]
                    scoped_calls = self._allocation["task_calls"][task_id]
                else:
                    limits = self._allocation["global_limits"]
                    scoped_calls = self._allocation["global_calls"]
                if stage not in limits or scoped_calls.get(stage, 0) >= limits[stage]:
                    raise ReResearchTaskBudgetExhausted(
                        f"Supplemental {stage} allocation exhausted for {task_id or 'round'}; other tasks and audit calls remain reserved.")
            # Shared admission is checked first. A rejected shared permit must
            # not inflate the task's independently persisted model counters.
            if before_reserve is not None:
                before_reserve()
            self.calls += 1
            self.by_stage[stage] += 1
            if scoped_calls is not None:
                scoped_calls[stage] = scoped_calls.get(stage, 0) + 1
            try:
                if self._on_reserve is not None:
                    # The orchestrator persists this permit before the HTTP call.
                    self._on_reserve(self._snapshot_unlocked())
            except BaseException:
                self.calls -= 1
                self.by_stage[stage] -= 1
                if scoped_calls is not None:
                    scoped_calls[stage] -= 1
                raise

    def _snapshot_unlocked(self):
        result = {"maximum": self.maximum, "calls": self.calls, "by_stage": dict(self.by_stage)}
        if self._allocation is not None:
            result["allocation"] = deepcopy(self._allocation)
        return result

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_unlocked()

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
        if "allocation" in data:
            allocation = deepcopy(data["allocation"])
            try:
                limits, used = allocation["task_limits"], allocation["task_calls"]
                if not limits or set(limits) != set(used):
                    raise ValueError("Mismatched supplemental task allocations.")
                capacity = allocation["base_calls"] + sum(allocation["global_limits"].values())
                actual = allocation["base_calls"]
                expected = Counter(allocation["base_by_stage"])
                pairs = [(limits[key], used[key]) for key in limits]
                pairs.append((allocation["global_limits"], allocation["global_calls"]))
                if (type(actual) is not int or actual < 0 or sum(expected.values()) != actual
                        or any(type(value) is not int or value < 0 for value in expected.values())):
                    raise ValueError("Invalid allocation baseline.")
                for task_id in limits:
                    capacity += sum(limits[task_id].values())
                for cap, spent in pairs:
                    if (any(type(value) is not int or value < 0 for value in cap.values())
                            or any(type(value) is not int or value < 0 or value > cap.get(key, -1) for key, value in spent.items())):
                        raise ValueError("Invalid supplemental stage quota.")
                    actual += sum(spent.values())
                    expected.update(spent)
                if capacity > maximum or actual != calls or expected != budget.by_stage:
                    raise ValueError("Supplemental allocation totals do not match durable usage.")
            except (KeyError, TypeError, AttributeError) as error:
                raise ValueError("Invalid persisted supplemental allocation.") from error
            budget._allocation = allocation
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
