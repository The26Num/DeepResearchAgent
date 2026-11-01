import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Lock
from typing import Callable

from app.api.schemas import (
    ErrorDetail, EvidenceExplorerResponse, PlanResponse, ReportResponse, RunAcceptedResponse, RunListResponse,
    RunResponse, TaskResponse,
)
from app.persistence.models import RunStatus
from app.persistence.serialization import IncompatibleCheckpointError, StateIntegrityError
from app.persistence.store import NoCheckpointError, ResearchRunStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.verification_store import VerificationStore
from app.research.events import ProgressData, ProgressUpdate
from app.api.services.events import EventHub
from app.api.services.stage_progress import phase_progress

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class RunService:
    """Single-process admission control. SQLite is the source of run state.

    A slot is reserved before creating/resuming a run; excess work is rejected
    rather than accumulated in an unbounded executor queue. Each worker owns
    a separate orchestrator, whose sync entry creates its own asyncio loop.
    """

    def __init__(self, store: ResearchRunStore, *, max_concurrent_runs=1,
                 pipeline_factory: Callable = ResearchOrchestrator, sse_heartbeat_seconds=15,
                 sse_state_check_seconds=5, sse_buffer_size=128, sse_max_connections=64):
        self.store = store
        self.pipeline_factory = pipeline_factory
        self.limit = max_concurrent_runs
        self.events = EventHub(buffer_size=sse_buffer_size, max_connections=sse_max_connections, redact=store.redact)
        self.sse_heartbeat_seconds = sse_heartbeat_seconds
        self.sse_state_check_seconds = sse_state_check_seconds
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent_runs, thread_name_prefix="research-api")
        self._lock = Lock()
        self._active: set[str] = set()
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
        # A synchronous model/tool call cannot safely be cancelled. Graceful
        # shutdown drains workers; a killed server is recovered on next startup.
        self._executor.shutdown(wait=True)

    def health(self):
        with self.store.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    def _capacity(self):
        if self._closed:
            raise ApiError(503, "SERVER_STOPPING", "The research server is stopping.")
        if len(self._active) >= self.limit:
            raise ApiError(429, "RUN_CAPACITY_EXCEEDED", "All research slots are busy. Try again after a run finishes.")

    def create(self, question: str) -> RunAcceptedResponse:
        with self._lock:
            self._capacity()
            pipeline = self.pipeline_factory(run_store=self.store)
            run = pipeline.prepare_run(question)
            self._submit(run.run_id, pipeline, question=run.goal)
            return RunAcceptedResponse(run_id=run.run_id, status=run.status)

    def resume(self, run_id: str) -> RunAcceptedResponse:
        with self._lock:
            run = self.store.get_run(run_id)
            if run.status == RunStatus.COMPLETED:
                raise ApiError(409, "RUN_ALREADY_COMPLETED", "This research run is completed; read its saved report.")
            if run_id in self._active or run.status == RunStatus.RUNNING:
                raise ApiError(409, "RUN_ALREADY_RUNNING", "This research run is already running.")
            self._checkpoint(run_id)  # Phase 6 validation before accepting work.
            self._capacity()
            pipeline = self.pipeline_factory(run_store=self.store)
            # Persist immediately, so polling sees RUNNING before the worker
            # reaches Phase 6 resume(). That method alone increments resume_count.
            self.store.mark_scheduled(run_id)
            self._submit(run_id, pipeline, resume=True)
            return RunAcceptedResponse(run_id=run_id, status=RunStatus.RUNNING)

    def _submit(self, run_id, pipeline, *, question=None, resume=False):
        pipeline.event_sink = self.events.sink(run_id)
        self.events.start(run_id)
        self._active.add(run_id)
        try:
            self._executor.submit(self._execute, run_id, pipeline, question, resume)
        except Exception as error:
            self._active.discard(run_id)
            self.store.mark_failed(run_id, error)
            self._publish_lifecycle(run_id, "run_failed")
            self.events.finish(run_id)
            raise ApiError(503, "BACKGROUND_START_FAILED", "Research could not be started; its checkpoint was retained.") from error

    def _execute(self, run_id, pipeline, question, resume):
        try:
            self._publish_lifecycle(run_id, "run_started")
            if resume:
                pipeline.resume(run_id)
            else:
                pipeline.run(question, prepared_run_id=run_id)
            # The real orchestrator persists terminal status itself. Treat an
            # unexpected early return as failure instead of leaving RUNNING forever.
            if self.store.get_run(run_id).status == RunStatus.RUNNING:
                raise RuntimeError("Pipeline returned without a terminal persisted run status.")
        except BaseException as error:
            try:
                if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    self.store.mark_interrupted(run_id)
                elif self.store.get_run(run_id).status != RunStatus.FAILED:
                    self.store.mark_failed(run_id, error)
            except Exception:
                logger.error("Could not persist background failure for run %s", run_id)
            logger.error("Research run %s failed (%s)", run_id, type(error).__name__)
        finally:
            try:
                run = self.store.get_run(run_id)
                kind = {RunStatus.COMPLETED: "run_completed", RunStatus.FAILED: "run_failed",
                        RunStatus.INTERRUPTED: "run_interrupted"}.get(run.status)
                if kind:
                    self._publish_lifecycle(run_id, kind, run)
            except Exception:
                logger.error("Could not read final streaming state for run %s", run_id)
            finally:
                self.events.finish(run_id)
            with self._lock:
                self._active.discard(run_id)

    def _publish_lifecycle(self, run_id, kind, run=None):
        try:
            run = run or self.store.get_run(run_id)
            self.events.publish(run_id, ProgressUpdate(event_type=kind, stage=run.current_stage,
                progress_data=ProgressData(run_status=run.status.value, resume_count=run.resume_count)))
        except Exception:
            logger.warning("Progress notification unavailable for run %s", run_id)

    def _checkpoint(self, run_id):
        try:
            return self.store.load_latest_checkpoint(run_id)
        except NoCheckpointError as error:
            raise ApiError(409, "CHECKPOINT_UNAVAILABLE", "This run has no saved checkpoint to resume or read.") from error
        except (IncompatibleCheckpointError, StateIntegrityError) as error:
            raise ApiError(409, "CHECKPOINT_INVALID", "The saved checkpoint is incompatible or invalid.") from error

    def _public(self, model):
        # Redact even historic data if credentials have changed since it was saved.
        return type(model).model_validate(self.store.redact(model.model_dump(mode="json")))

    def get(self, run_id: str) -> RunResponse:
        run = self.store.get_run(run_id)
        state = self._checkpoint(run_id).state if run.last_checkpoint_id is not None else None
        plan = PlanResponse.model_validate(state.research_plan.model_dump()) if state and state.research_plan else None
        supplemental = [TaskResponse.model_validate(task.model_dump(include=set(TaskResponse.model_fields)))
                        for p in state.reresearch_summary.plans for task in p.tasks] if state else []
        error = None
        if run.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
            # Raw exception strings can contain provider URLs, credentials, or
            # local paths. Keep them in the redacted store, never the HTTP response.
            timed_out = run.error_type in {"TimeoutError", "OpenAITimeoutError", "APITimeoutError"}
            code = "RESEARCH_TIMEOUT" if timed_out else f"RESEARCH_{run.status.value}"
            message = "A research request timed out." if timed_out else (
                "Research execution failed." if run.status == RunStatus.FAILED else "Research execution was interrupted.")
            error = ErrorDetail(code=code, message=message + " Resume explicitly from the saved checkpoint if available.")
        return self._public(RunResponse(
            run_id=run.run_id, status=run.status, question=run.goal, current_stage=run.current_stage,
            created_at=run.created_at, updated_at=run.updated_at, resume_count=run.resume_count,
            checkpoint_available=state is not None,
            resumable=state is not None and run.status in {RunStatus.FAILED, RunStatus.INTERRUPTED},
            plan=plan, supplemental_tasks=supplemental, error=error, phase_statuses=phase_progress(run, state),
        ))

    def list(self, limit: int) -> RunListResponse:
        return RunListResponse(runs=[self.get(run.run_id) for run in self.store.list_runs(limit)])

    def report(self, run_id: str) -> ReportResponse:
        run = self.store.get_run(run_id)
        if run.status != RunStatus.COMPLETED:
            raise ApiError(409, "REPORT_NOT_READY", "A completed research run is required to read its report.")
        state = self._checkpoint(run_id).state
        if state.research_report is None or state.rendered_report is None:
            raise ApiError(409, "REPORT_UNAVAILABLE", "This completed run has no saved rendered research report.")
        return self._public(ReportResponse(
            run_id=run_id, report=state.research_report,
            markdown=state.rendered_report.markdown, references=state.rendered_report.references,
        ))

    def evidence(self, run_id: str) -> EvidenceExplorerResponse:
        """Read one validated checkpoint; never research or re-verify anything."""
        run = self.store.get_run(run_id)
        if run.last_checkpoint_id is None:
            return EvidenceExplorerResponse(run_id=run_id, checkpoint_available=False)
        checkpoint = self._checkpoint(run_id)
        state = checkpoint.state
        verified = VerificationStore.restore([v.model_dump(mode="json") for v in state.verification_results])
        return self._public(EvidenceExplorerResponse(
            run_id=run_id, checkpoint_available=True, checkpoint_stage=checkpoint.stage,
            claims=state.evidence_store.claims, evidence=state.evidence_store.evidence,
            sources=state.evidence_store.sources, verification_results=verified.latest_results,
            relevance_results=list(state.relevance_results.values()),
        ))
