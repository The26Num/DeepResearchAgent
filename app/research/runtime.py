"""Per-task deadlines and progress, including bounded waiting for synchronous agents."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, TypeVar

from langchain_core.callbacks import BaseCallbackHandler


_ACTIVE_RUNTIME: ContextVar["ResearchRuntime | None"] = ContextVar("research_runtime", default=None)
_T = TypeVar("_T")


class ResearchTimeoutError(TimeoutError):
    """A research task exhausted its wall-clock budget."""


class ResearchRuntime:
    def __init__(self, task_id: str, timeout: float, progress_interval: float) -> None:
        self.task_id = task_id
        self.timeout = timeout
        self.progress_interval = progress_interval
        self.started = monotonic()
        self.closed = Event()
        self._lock = Lock()
        self._stages: dict[object, str] = {}

    def _timeout_error(self) -> ResearchTimeoutError:
        with self._lock:
            stage = next(reversed(self._stages.values()), "task execution")
        return ResearchTimeoutError(
            f"Research task {self.task_id} timed out after {self.timeout:g}s; last stage: {stage}."
        )

    def remaining(self) -> float:
        seconds = self.timeout - (monotonic() - self.started)
        if self.closed.is_set() or seconds <= 0:
            raise self._timeout_error()
        return seconds

    def log(self, message: str, category: str = "Progress") -> None:
        # Suppress late worker output after its caller has failed or timed out.
        with self._lock:
            if not self.closed.is_set():
                print(f"[{self.task_id}] {category}: {message}", flush=True)

    @contextmanager
    def activate(self):
        token = _ACTIVE_RUNTIME.set(self)
        try:
            yield
        finally:
            _ACTIVE_RUNTIME.reset(token)

    def start_stage(self, name: str) -> tuple[object, float]:
        self.remaining()
        token = object()
        with self._lock:
            self._stages[token] = name
        self.log(f"{name} started")
        return token, monotonic()

    def end_stage(self, stage: tuple[object, float], outcome: str) -> None:
        token, started = stage
        with self._lock:
            name = self._stages.pop(token, "operation")
        self.log(f"{name} {outcome} elapsed={monotonic() - started:.2f}s")

    @contextmanager
    def operation(self, name: str):
        stage = self.start_stage(name)
        outcome = {"status": "completed"}
        try:
            with self.activate():
                yield outcome
            self.remaining()
        except BaseException as exc:
            self.end_stage(stage, f"failed ({type(exc).__name__})")
            raise
        else:
            self.end_stage(stage, outcome["status"])

    def run(self, work: Callable[[], _T]) -> _T:
        """Bound the caller's wait without pretending that thread cancellation kills I/O.

        In-flight requests finish under their own HTTP timeout. Closing this runtime
        prevents subsequent tools/model calls and discards late results. The worker
        is daemonized to bound this caller's wait. Framework executor threads may
        still finish their HTTP requests during interpreter shutdown.
        """
        done = Event()
        result: list[_T] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                with self.activate():
                    result.append(work())
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        self.log(f"task started timeout={self.timeout:g}s")
        Thread(target=worker, name=f"research-{self.task_id}", daemon=True).start()
        try:
            while not done.wait(min(self.progress_interval, self.remaining())):
                self.remaining()
                with self._lock:
                    stages = ", ".join(self._stages.values()) or "task execution"
                self.log(f"waiting stage={stages} elapsed={monotonic() - self.started:.1f}s")
            self.remaining()
            if errors:
                raise errors[0]
            return result[0]
        except ResearchTimeoutError as exc:
            self.log(str(exc))
            raise
        finally:
            with self._lock:
                self.closed.set()


def bounded_request_timeout(default: float | tuple[float, float]):
    runtime = _ACTIVE_RUNTIME.get()
    if runtime is None:
        return default
    remaining = runtime.remaining()
    if isinstance(default, tuple):
        return tuple(min(value, remaining) for value in default)
    return min(default, remaining)


class ResearchModelCallbacks(BaseCallbackHandler):
    """Log each actual model request and stop new calls after the task deadline."""

    raise_error = True
    run_inline = True

    def __init__(self, runtime: ResearchRuntime) -> None:
        self.runtime = runtime
        self._requests: dict[object, tuple[object, float]] = {}
        self._lock = Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        stage = self.runtime.start_stage("model request")
        with self._lock:
            self._requests[run_id] = stage

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        self.on_chat_model_start(serialized, prompts, run_id=run_id)

    def _end(self, run_id, outcome: str) -> None:
        with self._lock:
            stage = self._requests.pop(run_id, None)
        if stage is not None:
            self.runtime.end_stage(stage, outcome)

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        self._end(run_id, "completed")
        self.runtime.remaining()

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        self._end(run_id, f"failed ({type(error).__name__})")
