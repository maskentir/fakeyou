import base64
import zlib
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.perception.meet_protocol import (
    MAX_INFLATED_BYTES,
    chat_time,
    decode_collection_event,
    decode_sync_response,
    inflate,
)
from tests._meet_protocol_fakes import (
    chat_message,
    collection_bytes,
    collection_payload,
    output,
    sync_body,
    user_info,
)

SENT_MS = 1_791_588_559_333
AYU = "devices/ayu"
BUDI = "devices/budi"


@pytest.mark.parametrize("compress", ["zlib", "gzip"])
def test_a_collection_event_yields_users_outputs_and_chat(compress: str) -> None:
    payload = collection_payload(
        compress,
        users=[user_info(AYU, "Ayu"), user_info("devices/ayu-screen", "", parent=AYU)],
        outputs=[output(AYU, "stream-1"), output(AYU, "audio-1", kind=1)],
        chat=[chat_message("m1", BUDI, "deck link?", SENT_MS * 1000)],
    )

    update = decode_collection_event(payload)

    assert [(user.device_id, user.name, user.present) for user in update.users] == [
        (AYU, "Ayu", True),
        ("devices/ayu-screen", "", True),
    ]
    assert update.users[1].is_screen
    assert [(item.stream_id, item.kind) for item in update.outputs] == [
        ("stream-1", "video"),
        ("audio-1", "audio"),
    ]
    [chat] = update.chat
    assert (chat.message_id, chat.device_id, chat.text) == ("m1", BUDI, "deck link?")
    assert chat.sent_at == datetime.fromtimestamp(SENT_MS / 1000, UTC)


def test_fields_the_schema_does_not_know_are_ignored() -> None:
    unknown_field = b"\xa2\x06\x05hello"
    raw = collection_bytes(users=[user_info(AYU, "Ayu")]) + unknown_field

    update = decode_collection_event(zlib.compress(raw))

    assert [user.name for user in update.users] == ["Ayu"]


def test_incomplete_entries_are_skipped() -> None:
    payload = collection_payload(
        users=[user_info("", "Nobody")],
        outputs=[output(AYU, "")],
        chat=[chat_message("m1", BUDI, "", SENT_MS)],
    )

    update = decode_collection_event(payload)

    assert (update.users, update.outputs, update.chat) == ((), (), ())


def test_a_user_outside_the_meeting_is_not_present() -> None:
    payload = collection_payload(users=[user_info(AYU, "Ayu", status=6)])

    assert not decode_collection_event(payload).users[0].present


def test_the_twin_recognises_itself() -> None:
    payload = collection_payload(users=[user_info("devices/twin", "Rani Twin", current=True)])

    assert decode_collection_event(payload).users[0].current_user


def test_a_sync_response_lists_everyone() -> None:
    users = decode_sync_response(sync_body([user_info(AYU, "Ayu"), user_info(BUDI, "Budi")]))

    assert [user.name for user in users] == ["Ayu", "Budi"]


@pytest.mark.parametrize(
    ("decode", "payload", "reason"),
    [
        (decode_collection_event, b"not deflated", "inflate"),
        (decode_sync_response, b"%%%not base64%%%", "base64"),
        (decode_sync_response, base64.b64encode(b"\xff\xff\xff"), "SyncResponse"),
    ],
)
def test_unreadable_messages_are_a_named_error(
    decode: Callable[[bytes], object], payload: bytes, reason: str
) -> None:
    with pytest.raises(FakeYouError, match=reason) as caught:
        decode(payload)

    assert caught.value.code is ErrorCode.PERCEPTION_PROTOCOL_UNREADABLE


def test_inflating_refuses_a_decompression_bomb() -> None:
    bomb = zlib.compress(bytes(MAX_INFLATED_BYTES + 1))

    with pytest.raises(FakeYouError, match="too_large"):
        inflate(bomb)


@pytest.mark.parametrize("scale", [1, 1_000, 1_000_000])
def test_chat_times_are_read_in_seconds_milliseconds_or_microseconds(scale: int) -> None:
    seconds = SENT_MS // 1000

    assert chat_time(seconds * scale) == datetime.fromtimestamp(seconds, UTC)


def test_a_missing_chat_time_stays_unknown() -> None:
    assert chat_time(0) is None
