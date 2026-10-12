"""Run-local gap storage with deterministic identity-based deduplication."""

from threading import RLock

from app.schemas.gap import GapStatus, GapType, ResearchGap


class GapStore:
    def __init__(self) -> None:
        self._gaps: dict[str, ResearchGap] = {}
        self._keys: dict[tuple, str] = {}
        self._lock = RLock()

    def add_gap(self, gap: ResearchGap) -> ResearchGap:
        snapshot = ResearchGap.model_validate(gap.model_dump())
        key = (snapshot.task_id, snapshot.claim_id, snapshot.gap_type)
        with self._lock:
            previous = self._gaps.get(snapshot.gap_id)
            if previous is not None and previous != snapshot:
                raise ValueError(f"Conflicting gap_id: {snapshot.gap_id}.")
            if key not in self._keys:
                self._gaps[snapshot.gap_id] = snapshot
                self._keys[key] = snapshot.gap_id
            return self._gaps[self._keys[key]].model_copy(deep=True)

    def get_gap(self, gap_id: str) -> ResearchGap:
        with self._lock:
            if gap_id not in self._gaps:
                raise KeyError(f"Unknown gap_id: {gap_id}.")
            return self._gaps[gap_id].model_copy(deep=True)

    def update_status(self, gap_id: str, status: GapStatus | str) -> ResearchGap:
        status = GapStatus(status)
        with self._lock:
            previous = self.get_gap(gap_id)
            snapshot = ResearchGap.model_validate({**previous.model_dump(), "status": status})
            self._gaps[gap_id] = snapshot
            return snapshot.model_copy(deep=True)

    def all_gaps(self) -> list[ResearchGap]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._gaps.values()]

    def get_gaps_for_task(self, task_id: str) -> list[ResearchGap]:
        return [item for item in self.all_gaps() if item.task_id == task_id]

    def get_gaps_for_claim(self, claim_id: str) -> list[ResearchGap]:
        return [item for item in self.all_gaps() if item.claim_id == claim_id]

    def get_gaps_by_type(self, gap_type: GapType | str) -> list[ResearchGap]:
        kind = GapType(gap_type)
        return [item for item in self.all_gaps() if item.gap_type == kind]
