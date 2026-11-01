"""FastAPI application factory; importing this module performs no DB/model I/O."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.api.routes import router
from app.api.schemas import ErrorDetail, ErrorResponse
from app.api.services.process_lock import ApiProcessLock
from app.api.services.runs import ApiError, RunService
from app.config import Settings, get_settings
from app.persistence.store import ResearchRunNotFoundError, ResearchRunStore
from app.research.orchestrator import ResearchOrchestrator

logger = logging.getLogger(__name__)


def error_response(status, code, message):
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def create_app(*, settings: Settings | None = None, store: ResearchRunStore | None = None,
               pipeline_factory=ResearchOrchestrator) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application):
        def start():
            db = store if store is not None else ResearchRunStore(config.research_db_path)
            lease = ApiProcessLock(db.path)
            try:
                recovered = db.recover_unfinished_runs()
                if recovered:
                    logger.info("Marked %s unfinished runs interrupted; explicit resume required.", recovered)
                runs = RunService(db, max_concurrent_runs=config.api_max_concurrent_runs,
                                  pipeline_factory=pipeline_factory,
                                  sse_heartbeat_seconds=config.api_sse_heartbeat_seconds,
                                  sse_state_check_seconds=config.api_sse_state_check_seconds,
                                  sse_buffer_size=config.api_sse_buffer_size,
                                  sse_max_connections=config.api_sse_max_connections)
                return runs, lease
            except BaseException:
                lease.close()
                raise
        runs, lease = await run_in_threadpool(start)
        application.state.run_service = runs
        try:
            yield
        finally:
            def stop():
                try:
                    runs.close()
                finally:
                    lease.close()
            await run_in_threadpool(stop)

    application = FastAPI(title="Research Agent API", version="8C", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware, allow_origins=config.api_cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
    )

    @application.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message)

    @application.exception_handler(ResearchRunNotFoundError)
    async def missing_run(request: Request, exc):
        return error_response(404, "RUN_NOT_FOUND", "The requested research run does not exist.")

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc):
        # Do not echo input/ctx: unexpected fields can include credentials.
        return error_response(422, "INVALID_REQUEST", "Invalid request parameters. Check the API schema.")

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return error_response(exc.status_code, "HTTP_ERROR", "The requested endpoint or HTTP method is unavailable.")

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc):
        logger.error("API request failed (%s)", type(exc).__name__)
        return error_response(500, "INTERNAL_ERROR", "The server could not process this request.")

    application.include_router(router)
    return application


app = create_app()
