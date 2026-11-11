from datetime import UTC, datetime
from typing import Any, cast

from fakeyou.config.settings import PerceptionSettings
from fakeyou.observability.journal import Journal
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.camera_evidence import CameraObservation
from fakeyou.perception.context import CAMERA_LIVENESS_TIMEOUT_SECONDS, PerceptionContext
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.sensors import ActiveSampler, DrainBatch, DrainedFrame, PerceptionSensors
from fakeyou.perception.timeline import MeetingTimeline
from tests._meet_protocol_fakes import collection_payload, output, user_info
from tests._perception_fakes import vision_model

JOINED = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
JOINED_MS = JOINED.timestamp() * 1000
AYU = "spaces/s/devices/ayu"
BUDI = "spaces/s/devices/budi"
TWIN = "spaces/s/devices/twin"


def observation(participant_id: str = BUDI) -> CameraObservation:
    return CameraObservation(
        participant_id=participant_id,
        observed_at=datetime.now(UTC),
        clothing="light ivory shirt",
        visible_objects=[],
        background="indoor room",
        appearance="a person speaking",
    )


def jpeg(color: tuple[int, int, int]) -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def frame(stream_id: str, data: bytes, *, at_ms: float = JOINED_MS + 5000) -> DrainedFrame:
    import base64

    return DrainedFrame.model_validate(
        {
            "stream_id": stream_id,
            "role": "camera",
            "page_ms": at_ms,
            "width": 64,
            "height": 48,
            "reason": "changed",
            "jpeg_b64": base64.b64encode(data).decode(),
        }
    )


def people(*, budi_camera: bool = True) -> bytes:
    outputs = [output(BUDI, "stream-budi", disabled=not budi_camera)]
    return collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=outputs,
    )


class Stack:
    def __init__(self) -> None:
        self.clock = [0.0]
        self.journal = Journal("m1", emit=lambda _: None)
        self.timeline = MeetingTimeline(journal=self.journal)
        self.model = vision_model()
        self.interpreter = PerceptionInterpreter(
            self.model,
            PerceptionBudget(budget_usd=10.0, max_calls=50),
            max_concurrent=2,
            journal=self.journal,
            monotonic=lambda: self.clock[0],
        )
        self.sensors = PerceptionSensors(
            self.timeline,
            PerceptionSettings(enabled=True),
            joined_at=JOINED,
            journal=self.journal,
            on_camera_invalidate=self._invalidate,
        )
        self.sensors.monotonic = lambda: self.clock[0]
        self.context = PerceptionContext(
            self.timeline,
            self.interpreter,
            max_chars=6000,
            state=self.sensors.roster.state,
            camera_epoch=lambda: self.sensors.epoch,
            camera_monotonic=lambda: self.clock[0],
        )

    def _invalidate(self, participant_id: str | None) -> None:
        if participant_id is None:
            self.interpreter.clear_camera_observations()
        else:
            self.interpreter.drop_camera_observation(participant_id)

    async def setup_camera(self, data: bytes) -> None:
        await self.sensors.accept_batch(DrainBatch(messages=[]))
        import base64

        from fakeyou.perception.sensors import DrainedMessage

        msg = DrainedMessage(
            channel="collections",
            page_ms=JOINED_MS + 1000,
            b64=base64.b64encode(people()).decode(),
        )
        await self.sensors.accept_batch(
            DrainBatch(messages=[msg], frames=[frame("stream-budi", data)])
        )

    async def heartbeat(self) -> None:
        await self.sensors.accept_batch(
            DrainBatch(
                active=[
                    ActiveSampler(stream_id="stream-budi", role="camera", page_ms=JOINED_MS + 2000)
                ]
            )
        )

    async def look(self) -> dict[str, Any]:
        import json

        raw = await self.context.look_at_participant("Budi", "what are they wearing?")
        return cast(dict[str, Any], json.loads(raw))


async def test_camera_off_makes_evidence_unavailable_and_invalidates_cache() -> None:
    stack = Stack()
    data = jpeg((200, 180, 160))
    stack.model.observations = [observation(), observation()]
    await stack.setup_camera(data)
    await stack.heartbeat()

    first = await stack.look()
    assert first["code"] == "ok"

    import base64

    from fakeyou.perception.sensors import DrainedMessage

    off = DrainedMessage(
        channel="collections",
        page_ms=JOINED_MS + 3000,
        b64=base64.b64encode(people(budi_camera=False)).decode(),
    )
    await stack.sensors.accept_batch(DrainBatch(messages=[off]))

    unavailable = await stack.look()
    assert unavailable["code"] == "camera_unavailable"
    assert stack.timeline.camera_frames.get(BUDI) is None

    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 4000,
                    b64=base64.b64encode(people(budi_camera=True)).decode(),
                )
            ],
            frames=[frame("stream-budi", data, at_ms=JOINED_MS + 4000)],
        )
    )
    await stack.heartbeat()

    fresh = await stack.look()
    assert fresh["code"] == "ok"
    assert fresh["resolution"] == "fresh"


async def test_participant_leave_invalidates_frame_and_cache() -> None:
    stack = Stack()
    data = jpeg((120, 120, 120))
    stack.model.observations = [observation()]
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    leave = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi", status=2),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[],
    )
    import base64

    from fakeyou.perception.sensors import DrainedMessage

    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 3000,
                    b64=base64.b64encode(leave).decode(),
                )
            ]
        )
    )

    result = await stack.look()
    assert result["code"] in {"camera_unavailable", "unknown_participant"}
    assert stack.timeline.camera_frames.get(BUDI) is None
    assert BUDI not in vars(stack.interpreter)["_camera_cache"].entries


async def test_rejoin_epoch_rejects_previous_frame() -> None:
    stack = Stack()
    data = jpeg((90, 90, 90))
    stack.model.observations = [observation(), observation()]
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    stack.sensors.begin_epoch()
    unavailable = await stack.look()
    assert unavailable["code"] == "camera_unavailable"

    await stack.setup_camera(data)
    await stack.heartbeat()
    fresh = await stack.look()
    assert fresh["code"] == "ok"
    assert fresh["resolution"] == "fresh"


async def test_a_stable_camera_stays_live_with_heartbeats() -> None:
    stack = Stack()
    data = jpeg((60, 70, 80))
    stack.model.observations = [observation(), observation()]
    await stack.setup_camera(data)

    for step in range(20):
        stack.clock[0] = step * 4.5
        await stack.heartbeat()
    stack.clock[0] = 90.0
    await stack.heartbeat()

    result = await stack.look()
    assert result["code"] == "ok"

    stack.clock[0] = 90.0 + 61.0
    await stack.heartbeat()
    later = await stack.look()
    assert later["code"] == "ok"
    assert later["resolution"] == "fresh"


async def test_a_stream_without_heartbeats_becomes_unavailable() -> None:
    stack = Stack()
    data = jpeg((30, 40, 50))
    stack.model.observations = [observation()]
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    stack.clock[0] = CAMERA_LIVENESS_TIMEOUT_SECONDS + 1.0
    result = await stack.look()
    assert result["code"] == "camera_unavailable"


async def test_a_new_stream_replaces_the_old_observation() -> None:
    stack = Stack()
    data = jpeg((10, 20, 30))
    stack.model.observations = [observation(), observation()]
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    import base64

    from fakeyou.perception.sensors import DrainedMessage

    rebound = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[output(BUDI, "stream-budi-2")],
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 6100,
                    b64=base64.b64encode(rebound).decode(),
                )
            ],
            frames=[frame("stream-budi-2", jpeg((200, 210, 220)), at_ms=JOINED_MS + 6100)],
        )
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            active=[
                ActiveSampler(stream_id="stream-budi-2", role="camera", page_ms=JOINED_MS + 6200)
            ]
        )
    )

    result = await stack.look()
    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
