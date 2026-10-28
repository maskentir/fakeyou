import hashlib
import json
from typing import Any, override
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage

from fakeyou.observability.journal import Journal

DIGEST_CHARS = 16
CORRELATION_KEYS = ("transcript_id", "request_id")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def _model_name(serialized: dict[str, Any], params: dict[str, Any]) -> str | None:
    for source in (params, serialized.get("kwargs") or {}):
        for key in ("model", "model_name"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class ModelInputJournal(AsyncCallbackHandler):
    def __init__(self, journal: Journal, slot: str) -> None:
        self._journal = journal
        self._slot = slot

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        batch = messages[0] if messages else []
        system = "\n".join(message.text for message in batch if isinstance(message, SystemMessage))
        window = json.dumps(
            [
                [message.type, message.text]
                for message in batch
                if not isinstance(message, SystemMessage)
            ],
            ensure_ascii=False,
        )
        context = metadata or {}
        self._journal.record(
            "model_input_recorded",
            source="llm",
            slot=self._slot,
            model=_model_name(serialized, kwargs.get("invocation_params") or {}),
            run_id=str(run_id),
            system_sha256=digest(system),
            window_sha256=digest(window),
            messages=len(batch),
            chars=len(system) + len(window),
            **{key: context[key] for key in CORRELATION_KEYS if context.get(key)},
        )


def journal_model_inputs(model: BaseChatModel, journal: Journal | None, slot: str) -> BaseChatModel:
    if journal is None:
        return model
    callbacks = getattr(model, "callbacks", None)
    existing = callbacks if isinstance(callbacks, list) else []
    model.callbacks = [*existing, ModelInputJournal(journal, slot)]
    return model
