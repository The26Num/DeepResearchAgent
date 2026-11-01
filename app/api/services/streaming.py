"""SSE frames with heartbeats and SQLite reconciliation; no stdout parsing."""

import asyncio
from time import monotonic

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from app.research.events import ProgressData, ProgressUpdate

TERMINAL = {"COMPLETED": "run_completed", "FAILED": "run_failed", "INTERRUPTED": "run_interrupted"}


class RunEventResponse(StreamingResponse):
    """Release admission even if the client drops before the first body frame."""
    def __init__(self, content, *, hub=None, subscription=None, **kwargs):
        super().__init__(content, **kwargs)
        self.hub, self.subscription = hub, subscription

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.body_iterator.aclose()
            finally:
                if self.hub is not None and self.subscription is not None:
                    self.hub.unsubscribe(self.subscription)


def encode_event(event):
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"


async def event_stream(request, service, subscription):
    hub = service.events
    run_id = subscription.run_id

    def frame(kind, run, *, live=False):
        data = hub.live(subscription).model_dump() if live and run.status.value == "RUNNING" else {}
        data.update(run_status=run.status.value, resume_count=run.resume_count)
        return encode_event(hub.event(run_id, ProgressUpdate(event_type=kind, stage=run.current_stage,
                                                            progress_data=ProgressData(**data))))
    try:
        # Register before this read, so events racing with connection setup remain
        # buffered. This is a current-state snapshot, not historical replay.
        run = await run_in_threadpool(service.store.get_run, run_id)
        yield "retry: 2000\n\n"
        yield frame("run_snapshot", run, live=True)
        if run.status.value in TERMINAL:
            yield frame(TERMINAL[run.status.value], run)
            return
        fingerprint = (run.status, run.current_stage, run.last_checkpoint_id)
        check_at = monotonic() + service.sse_state_check_seconds
        heartbeat_at = monotonic() + service.sse_heartbeat_seconds
        while not await request.is_disconnected():
            events, missed = hub.read(subscription)
            if missed:
                run = await run_in_threadpool(service.store.get_run, run_id)
                yield frame("stream_reset", run)
                yield frame("run_snapshot", run, live=True)
            for event in events:
                yield encode_event(event)
                if event.event_type in TERMINAL.values():
                    return
            now = monotonic()
            if now >= check_at:
                run = await run_in_threadpool(service.store.get_run, run_id)
                check_at = monotonic() + service.sse_state_check_seconds
                current = (run.status, run.current_stage, run.last_checkpoint_id)
                if current != fingerprint:
                    yield frame("run_snapshot", run, live=True)
                    fingerprint = current
                if run.status.value in TERMINAL:
                    yield frame(TERMINAL[run.status.value], run)
                    return
            if now >= heartbeat_at:
                yield ": heartbeat\n\n"
                heartbeat_at = monotonic() + service.sse_heartbeat_seconds
            delay = min(check_at - monotonic(), heartbeat_at - monotonic(), 1.0)
            try:
                await asyncio.wait_for(subscription.wake.wait(), timeout=max(.01, delay))
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        # Streaming HTTP headers have already been sent; keep errors structured
        # and generic, without leaking an exception, URL or local path.
        yield encode_event(hub.event(run_id, ProgressUpdate(event_type="stream_error", stage="STREAM")))
    finally:
        hub.unsubscribe(subscription)
