"""Run-local verification history; no changes to Claims or EvidenceStore."""

from threading import RLock

from app.schemas.verification import VerificationResult


class VerificationStore:
    def __init__(self) -> None:
        self._results: dict[str, VerificationResult] = {}
        self._lock = RLock()

    def add_verification(self, result: VerificationResult) -> None:
        snapshot = VerificationResult.model_validate(result.model_dump())
        with self._lock:
            previous = self._results.get(snapshot.verification_id)
            if previous is not None and previous != snapshot:
                raise ValueError(f"Conflicting verification_id: {snapshot.verification_id}.")
            self._results[snapshot.verification_id] = snapshot

    def get_verifications_for_claim(self, claim_id: str) -> list[VerificationResult]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._results.values() if item.claim_id == claim_id]

    def get_latest_verification(self, claim_id: str) -> VerificationResult | None:
        history = self.get_verifications_for_claim(claim_id)
        return history[-1] if history else None

    def get_verifications_for_task(self, task_id: str) -> list[VerificationResult]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._results.values() if item.task_id == task_id]

    @property
    def latest_results(self) -> list[VerificationResult]:
        with self._lock:
            latest = {item.claim_id: item for item in self._results.values()}
            return [item.model_copy(deep=True) for item in latest.values()]
