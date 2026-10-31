import base64
import binascii
import zlib
from datetime import UTC, datetime
from typing import Any, Literal

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError, Message
from pydantic import BaseModel, ConfigDict

from fakeyou.catalog.errors import ErrorCode, FakeYouError

COLLECTIONS_CHANNEL = "collections"
SYNC_COLLECTIONS_PATH = "MeetingSpaceService/SyncMeetingSpaceCollections"
MAX_INFLATED_BYTES = 4 * 1024 * 1024
STATUS_IN_MEETING = 1
STATUS_REMOVED = 7
MILLISECOND_EPOCH_FLOOR = 100_000_000_000
MICROSECOND_EPOCH_FLOOR = 100_000_000_000_000
OUTPUT_KINDS: dict[int, "OutputKind"] = {1: "audio", 2: "video"}
SCHEMA_FILE = "fakeyou_meet_collections.proto"
SCHEMA_PACKAGE = "fakeyou.meet"
FieldKind = Literal["string", "int64", "int32", "bool", "message"]
FieldSpec = tuple[str, int, FieldKind, str | None, bool]
OutputKind = Literal["audio", "video", "unknown"]

_FIELD_TYPES: dict[FieldKind, descriptor_pb2.FieldDescriptorProto.Type.ValueType] = {
    "string": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "int64": descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "int32": descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    "bool": descriptor_pb2.FieldDescriptorProto.TYPE_BOOL,
    "message": descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
}
SCHEMA: dict[str, tuple[FieldSpec, ...]] = {
    "CollectionEvent": (("body", 1, "message", "CollectionBody", False),),
    "CollectionBody": (("update", 2, "message", "SpaceUpdate", False),),
    "SpaceUpdate": (
        ("devices", 3, "message", "DeviceInfo", False),
        ("space", 13, "message", "SpaceContent", False),
    ),
    "SpaceContent": (
        ("users", 1, "message", "UserList", False),
        ("chat", 4, "message", "ChatEnvelope", True),
    ),
    "DeviceInfo": (("outputs", 2, "message", "DeviceOutput", True),),
    "DeviceOutput": (
        ("output_type", 2, "int32", None, False),
        ("stream_id", 4, "string", None, False),
        ("device_id", 6, "string", None, False),
        ("status", 10, "message", "DeviceOutputStatus", False),
    ),
    "DeviceOutputStatus": (("disabled", 1, "bool", None, False),),
    "SyncResponse": (("wrapper", 2, "message", "SyncWrapper", False),),
    "SyncWrapper": (("users", 2, "message", "UserList", False),),
    "UserList": (
        ("event", 1, "message", "UserEvent", False),
        ("users", 2, "message", "UserInfo", True),
    ),
    "UserEvent": (("number", 1, "int64", None, False),),
    "UserInfo": (
        ("device_id", 1, "string", None, False),
        ("full_name", 2, "string", None, False),
        ("status", 4, "int32", None, False),
        ("current_user", 7, "string", None, False),
        ("parent_device_id", 21, "string", None, False),
        ("display_name", 29, "string", None, False),
        ("host", 34, "int32", None, False),
    ),
    "ChatEnvelope": (("message", 2, "message", "ChatMessage", False),),
    "ChatMessage": (
        ("message_id", 1, "string", None, False),
        ("device_id", 2, "string", None, False),
        ("timestamp", 3, "int64", None, False),
        ("content", 5, "message", "ChatContent", False),
    ),
    "ChatContent": (("text", 1, "string", None, False),),
}


def _schema_file() -> descriptor_pb2.FileDescriptorProto:
    file = descriptor_pb2.FileDescriptorProto(
        name=SCHEMA_FILE, package=SCHEMA_PACKAGE, syntax="proto3"
    )
    for name, fields in SCHEMA.items():
        message = file.message_type.add(name=name)
        for field_name, number, kind, type_name, repeated in fields:
            field = message.field.add(
                name=field_name,
                number=number,
                type=_FIELD_TYPES[kind],
                label=(
                    descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
                    if repeated
                    else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
                ),
            )
            if type_name is not None:
                field.type_name = f".{SCHEMA_PACKAGE}.{type_name}"
    return file


MESSAGES = message_factory.GetMessages([_schema_file()], pool=descriptor_pool.DescriptorPool())


def message_class(name: str) -> type[Message]:
    return MESSAGES[f"{SCHEMA_PACKAGE}.{name}"]


class ProtocolPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MeetUser(ProtocolPart):
    device_id: str
    name: str
    present: bool
    status: int = 0
    parent_device_id: str | None = None
    current_user: bool = False
    host: bool = False

    @property
    def is_screen(self) -> bool:
        return self.parent_device_id is not None


class DeviceOutput(ProtocolPart):
    device_id: str
    kind: OutputKind
    stream_id: str
    disabled: bool


class MeetChatMessage(ProtocolPart):
    message_id: str
    device_id: str
    text: str
    sent_at: datetime | None = None


class CollectionUpdate(ProtocolPart):
    users: tuple[MeetUser, ...] = ()
    outputs: tuple[DeviceOutput, ...] = ()
    chat: tuple[MeetChatMessage, ...] = ()


def inflate(payload: bytes) -> bytes:
    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 32)
    try:
        inflated = inflater.decompress(payload, MAX_INFLATED_BYTES)
    except zlib.error as error:
        raise FakeYouError(ErrorCode.PERCEPTION_PROTOCOL_UNREADABLE, reason="inflate") from error
    if inflater.unconsumed_tail:
        raise FakeYouError(ErrorCode.PERCEPTION_PROTOCOL_UNREADABLE, reason="too_large")
    return inflated


def _parse(name: str, data: bytes) -> Any:
    message = message_class(name)()
    try:
        message.ParseFromString(data)
    except DecodeError as error:
        raise FakeYouError(ErrorCode.PERCEPTION_PROTOCOL_UNREADABLE, reason=name) from error
    return message


def _user(info: Any) -> MeetUser | None:
    if not info.device_id:
        return None
    return MeetUser(
        device_id=info.device_id,
        name=info.display_name or info.full_name,
        present=info.status == STATUS_IN_MEETING,
        status=info.status,
        parent_device_id=info.parent_device_id or None,
        current_user=bool(info.current_user),
        host=bool(info.host),
    )


def _users(user_list: Any) -> tuple[MeetUser, ...]:
    return tuple(user for user in map(_user, user_list.users) if user is not None)


def chat_time(value: int) -> datetime | None:
    if value <= 0:
        return None
    if value >= MICROSECOND_EPOCH_FLOOR:
        return datetime.fromtimestamp(value / 1_000_000, UTC)
    if value >= MILLISECOND_EPOCH_FLOOR:
        return datetime.fromtimestamp(value / 1_000, UTC)
    return datetime.fromtimestamp(value, UTC)


def _output(output: Any) -> DeviceOutput | None:
    if not output.device_id or not output.stream_id:
        return None
    return DeviceOutput(
        device_id=output.device_id,
        kind=OUTPUT_KINDS.get(output.output_type, "unknown"),
        stream_id=output.stream_id,
        disabled=output.status.disabled,
    )


def _chat(envelope: Any) -> MeetChatMessage | None:
    message = envelope.message
    text = message.content.text
    if not message.message_id or not message.device_id or not text:
        return None
    return MeetChatMessage(
        message_id=message.message_id,
        device_id=message.device_id,
        text=text,
        sent_at=chat_time(message.timestamp),
    )


def decode_collection_event(payload: bytes) -> CollectionUpdate:
    event = _parse("CollectionEvent", inflate(payload))
    update = event.body.update
    return CollectionUpdate(
        users=_users(update.space.users),
        outputs=tuple(item for item in map(_output, update.devices.outputs) if item is not None),
        chat=tuple(item for item in map(_chat, update.space.chat) if item is not None),
    )


def decode_sync_response(body: bytes) -> tuple[MeetUser, ...]:
    try:
        raw = base64.b64decode(body.strip(), validate=True)
    except binascii.Error as error:
        raise FakeYouError(ErrorCode.PERCEPTION_PROTOCOL_UNREADABLE, reason="base64") from error
    return _users(_parse("SyncResponse", raw).wrapper.users)
