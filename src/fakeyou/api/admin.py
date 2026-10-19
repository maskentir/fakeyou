from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from fakeyou.api.auth import Principal, require_admin
from fakeyou.api.chat import chat
from fakeyou.config.sanitize import sanitized_settings_dump
from fakeyou.config.settings import Settings
from fakeyou.memory.maintenance import initialize_indexes
from fakeyou.memory.memory import open_graphiti_memory
from fakeyou.orchestration.providers import memory_config

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_Admin = Annotated[Principal, Depends(require_admin)]

OPENAPI_SUMMARY = (
    "Admin deploy surface. It is not a BYOF client surface; owners reach the "
    "meeting, memory and debrief routes instead."
)


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    thread_id: str
    message: str
    memory: bool = False


class MemoryInitView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status: str


class ChatTurnView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    reply: str


async def _initialize_memory(settings: Settings) -> None:
    await initialize_indexes(lambda: open_graphiti_memory(memory_config(settings)))


async def _run_chat_turn(
    *, thread_id: str, message: str, use_browser: bool, use_memory: bool, settings: Settings
) -> str:
    return await chat(
        message,
        thread_id,
        use_browser=use_browser,
        use_memory=use_memory,
        settings=settings,
    )


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> "ApiDeps":
    return cast("ApiDeps", request.app.state.deps)


@router.post("/memory-init", response_model=MemoryInitView, summary=OPENAPI_SUMMARY)
async def memory_init(_admin: _Admin, request: Request) -> MemoryInitView:
    settings = _settings(request)
    initialize: Callable[[], Awaitable[None]] = _deps(request).memory_init or partial(
        _initialize_memory, settings
    )
    await initialize()
    return MemoryInitView(status="initialized")


@router.get("/check", summary=OPENAPI_SUMMARY)
async def admin_check(_admin: _Admin, request: Request) -> dict[str, Any]:
    return sanitized_settings_dump(_settings(request))


@router.post("/chat/turns", response_model=ChatTurnView, summary=OPENAPI_SUMMARY)
async def chat_turn(payload: ChatTurn, _admin: _Admin, request: Request) -> ChatTurnView:
    settings = _settings(request)
    run: Callable[..., Awaitable[str]] = _deps(request).chat_turn or partial(
        _run_chat_turn, settings=settings
    )
    reply = await run(
        thread_id=payload.thread_id,
        message=payload.message,
        use_browser=False,
        use_memory=payload.memory,
    )
    return ChatTurnView(reply=reply)
