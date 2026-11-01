from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.schemas import (
    CreateRunRequest, ErrorResponse, EvidenceExplorerResponse, HealthResponse, ReportResponse,
    RunAcceptedResponse, RunListResponse, RunResponse,
)
from app.api.services.runs import ApiError, RunService
from app.api.services.streaming import RunEventResponse, event_stream

router = APIRouter(prefix="/api", responses={
    404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
    422: {"model": ErrorResponse}, 429: {"model": ErrorResponse},
    500: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})


def service(request: Request) -> RunService:
    return request.app.state.run_service


Service = Annotated[RunService, Depends(service)]


# Sync handlers use FastAPI's request threadpool for short SQLite operations.
# Research work runs in a separate bounded executor, never in that threadpool.
@router.get("/health", response_model=HealthResponse, tags=["health"])
def health(runs: Service):
    return runs.health()


@router.post("/runs", response_model=RunAcceptedResponse, status_code=202, tags=["runs"])
def create_run(body: CreateRunRequest, runs: Service):
    return runs.create(body.question)


@router.get("/runs", response_model=RunListResponse, tags=["runs"])
def list_runs(runs: Service, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return runs.list(limit)


@router.get("/runs/{run_id}", response_model=RunResponse, tags=["runs"])
def get_run(run_id: str, runs: Service):
    return runs.get(run_id)


@router.get("/runs/{run_id}/report", response_model=ReportResponse, tags=["runs"])
def get_report(run_id: str, runs: Service):
    return runs.report(run_id)


@router.get("/runs/{run_id}/evidence", response_model=EvidenceExplorerResponse, tags=["runs"])
def get_evidence(run_id: str, runs: Service):
    return runs.evidence(run_id)


@router.get("/runs/{run_id}/events", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}}, tags=["runs"])
async def get_events(run_id: str, request: Request, runs: Service):
    await run_in_threadpool(runs.store.get_run, run_id)  # 404 before streaming headers.
    try:
        subscription = runs.events.subscribe(run_id)
    except OverflowError as error:
        raise ApiError(429, "STREAM_CAPACITY_EXCEEDED", "Too many live event connections; use the status endpoint.") from error
    return RunEventResponse(event_stream(request, runs, subscription), hub=runs.events, subscription=subscription,
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.post("/runs/{run_id}/resume", response_model=RunAcceptedResponse, status_code=202, tags=["runs"])
def resume_run(run_id: str, runs: Service):
    return runs.resume(run_id)
