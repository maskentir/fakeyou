import base64
import gzip
import zlib
from collections.abc import Sequence
from typing import Any

from fakeyou.perception.meet_protocol import message_class


def user_info(
    device_id: str,
    name: str,
    *,
    status: int = 1,
    parent: str = "",
    current: bool = False,
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "display_name": name,
        "status": status,
        "parent_device_id": parent,
        "current_user": "self-marker" if current else "",
    }


def output(
    device_id: str, stream_id: str, *, kind: int = 2, disabled: bool = False
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "stream_id": stream_id,
        "output_type": kind,
        "status": {"disabled": disabled},
    }


def chat_message(message_id: str, device_id: str, text: str, timestamp: int) -> dict[str, Any]:
    return {
        "message": {
            "message_id": message_id,
            "device_id": device_id,
            "timestamp": timestamp,
            "content": {"text": text},
        }
    }


def collection_bytes(
    *,
    users: Sequence[dict[str, Any]] = (),
    outputs: Sequence[dict[str, Any]] = (),
    chat: Sequence[dict[str, Any]] = (),
) -> bytes:
    event: Any = message_class("CollectionEvent")()
    update = event.body.update
    for item in outputs:
        update.devices.outputs.add(**item)
    for item in users:
        update.space.users.users.add(**item)
    for item in chat:
        update.space.chat.add(**item)
    payload: bytes = event.SerializeToString()
    return payload


def collection_payload(compress: str = "zlib", **parts: Sequence[dict[str, Any]]) -> bytes:
    raw = collection_bytes(**parts)
    return gzip.compress(raw) if compress == "gzip" else zlib.compress(raw)


def sync_body(users: Sequence[dict[str, Any]]) -> bytes:
    response: Any = message_class("SyncResponse")()
    for item in users:
        response.wrapper.users.users.add(**item)
    payload: bytes = response.SerializeToString()
    return base64.b64encode(payload)
