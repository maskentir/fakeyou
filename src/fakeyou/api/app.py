import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fakeyou.api.admin import router as admin_router
from fakeyou.api.auth import Principal, TokenStore, require_admin, require_owner
from fakeyou.api.debrief import router as debrief_router
from fakeyou.api.errors import DomainConflict, error_response, http_status_for
from fakeyou.api.events import router as events_router
from fakeyou.api.memory import router as memory_router
from fakeyou.api.ratelimit import RateLimiter
from fakeyou.api.sessions import router as sessions_router
from fakeyou.catalog.errors import ErrorCode, FakeYouError, render_error
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import create_session_status
from fakeyou.transport.streams import StreamTransport

logger = logging.getLogger(__name__)

_FRAMEWORK_CODES: dict[int, ErrorCode] = {
    401: ErrorCode.API_UNAUTHORIZED,
    403: ErrorCode.API_FORBIDDEN,
    404: ErrorCode.API_NOT_FOUND,
    429: ErrorCode.API_RATE_LIMITED,
}


@dataclass(frozen=True)
class ApiDeps:
    transport: Callable[[], object] | None = None
    snapshot_store: Callable[[], object] | None = None
    now_iso: Callable[[], str] | None = None
    memory_service: Callable[[], object] | None = None
    debrief_service: Callable[[], object] | None = None
    memory_init: Callable[[], Awaitable[None]] | None = None
    chat_turn: Callable[..., Awaitable[str]] | None = None
    test_routes: bool = False
    event_keepalive_seconds: float | None = None


def _framework_code(status: int) -> ErrorCode:
    if status in _FRAMEWORK_CODES:
        return _FRAMEWORK_CODES[status]
    if 400 <= status < 500:
        return ErrorCode.INPUT_INVALID
    return ErrorCode.INTERNAL_FAILURE


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FakeYouError)
    async def fakeyou_error(_request: Request, error: FakeYouError) -> JSONResponse:
        status = http_status_for(error)
        return error_response(str(error.code), str(error), status=status)

    @app.exception_handler(DomainConflict)
    async def domain_conflict(_request: Request, conflict: DomainConflict) -> JSONResponse:
        error = conflict.error
        return error_response(
            str(error.code), str(error), status=http_status_for(error), details=conflict.details
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(
            str(ErrorCode.INPUT_INVALID),
            render_error(ErrorCode.INPUT_INVALID, reason="request validation failed"),
            status=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = _framework_code(error.status_code)
        message = (
            render_error(ErrorCode.INPUT_INVALID, reason="request rejected")
            if code is ErrorCode.INPUT_INVALID
            else render_error(code)
        )
        return error_response(str(code), message, status=error.status_code)

    @app.exception_handler(Exception)
    async def unhandled_error(_request: Request, error: Exception) -> JSONResponse:
        logger.error("unhandled API error", exc_info=error)
        return error_response(
            str(ErrorCode.INTERNAL_FAILURE),
            render_error(ErrorCode.INTERNAL_FAILURE),
            status=500,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.settings)
    async with AsyncExitStack() as exits:
        store = create_session_status(settings)
        exits.push_async_callback(store.close)
        exits.push_async_callback(_close_stream_transport, app)
        app.state.session_status = store
        yield


async def _close_stream_transport(app: FastAPI) -> None:
    transport = cast(StreamTransport | None, app.state.stream_transport)
    if transport is not None:
        await transport.aclose()


def create_app(settings: Settings, deps: ApiDeps | None = None) -> FastAPI:
    app = FastAPI(openapi_url="/openapi.json", lifespan=_lifespan)
    resolved_deps = deps if deps is not None else ApiDeps()
    app.state.settings = settings
    app.state.deps = resolved_deps
    app.state.stream_transport = None
    app.state.session_status = None
    app.state.token_store = TokenStore.from_settings(settings)
    app.state.rate_limiter = RateLimiter(settings.api.rate_limit_per_minute)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(sessions_router)
    app.include_router(events_router)
    app.include_router(memory_router)
    app.include_router(debrief_router)
    app.include_router(admin_router)

    if resolved_deps.test_routes:

        @app.get("/v1/test/owner")
        async def test_owner(
            principal: Annotated[Principal, Depends(require_owner)],
        ) -> dict[str, str | None]:
            return {"owner_id": principal.owner_id}

        @app.get("/v1/test/admin")
        async def test_admin(
            principal: Annotated[Principal, Depends(require_admin)],
        ) -> dict[str, str]:
            return {"role": principal.kind}

    _install_error_handlers(app)
    return app
