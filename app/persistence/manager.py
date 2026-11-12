"""Serialize safe snapshots and marshal worker permits back to the main loop."""

import asyncio
from concurrent.futures import Future

from app.persistence.models import ResearchRunState


class CheckpointWriteError(RuntimeError):
    """Persistence/failpoint failures must not become optional research failures."""


class CheckpointManager:
    def __init__(self, store, run_id, *, failure_injector=None):
        self.store = store
        self.run_id = run_id
        self.failure_injector = failure_injector
        self.safe_state: ResearchRunState | None = None
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def enter(self, stage):
        self.store.set_stage(self.run_id, stage)

    def save(self, stage, state):
        try:
            checkpoint = self.store.save_checkpoint(self.run_id, stage, state)
        except Exception as error:
            raise CheckpointWriteError(f"Checkpoint {stage} could not be saved: {type(error).__name__}.") from error
        self.safe_state = checkpoint.state.model_copy(deep=True)
        if self.failure_injector:
            try:
                self.failure_injector(stage)
            except Exception as error:
                raise CheckpointWriteError(f"Injected failure after {stage}: {error}") from error
        return checkpoint

    def reserve_budget(self, snapshot):
        """Called by workers, but only the orchestrator's loop writes SQLite.

        Copy the LAST SAFE state, never the live state being modified by workers.
        A permit may be conservatively charged if the process dies before HTTP.
        """
        future = Future()

        def persist():
            if self.closed:
                future.set_exception(RuntimeError("Research run is no longer active."))
                return
            try:
                state = self.safe_state.model_copy(deep=True)
                state.reresearch_budget = snapshot
                state.reresearch_summary.llm_calls = snapshot["calls"]
                state.reresearch_summary.calls_by_stage = snapshot["by_stage"]
                self.save("RERESEARCH_CALL_RESERVED", state)
                future.set_result(None)
            except BaseException as error:
                future.set_exception(error)

        self.loop.call_soon_threadsafe(persist)
        return future.result()

    def call_on_main(self, function):
        future = Future()
        def run():
            try:
                if self.closed:
                    raise RuntimeError("Research run is no longer active.")
                future.set_result(function())
            except BaseException as error:
                future.set_exception(error)
        self.loop.call_soon_threadsafe(run)
        return future.result()

    def reserve_resources(self, run_id, skills, record):
        """Persist permits/private references against the last safe graph only."""
        future = Future()
        def persist():
            try:
                if self.closed or run_id != self.run_id:
                    raise RuntimeError("Research resource run is no longer active.")
                state = self.safe_state.model_copy(deep=True)
                previous = state.resources.tasks.get(record.task_id)
                if previous is not None and previous.revision >= record.revision:
                    future.set_result(None)
                    return
                state.resources.run_id = run_id
                state.resources.skills = dict(skills)
                state.resources.tasks[record.task_id] = record.model_copy(deep=True)
                supplemental = any(task.id == record.task_id for plan in state.reresearch_summary.plans for task in plan.tasks)
                prefix = "RERESEARCH_RESOURCE_RESERVED" if supplemental else "TASK_RESOURCE_RESERVED"
                self.save(f"{prefix}:{record.task_id}", state)
                future.set_result(None)
            except BaseException as error:
                future.set_exception(error)
        try:
            on_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            persist()
        else:
            self.loop.call_soon_threadsafe(persist)
        return future.result()
