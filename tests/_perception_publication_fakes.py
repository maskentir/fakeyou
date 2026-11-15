from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC, ClockExchange, RemoteClock
from fakeyou.foundation.publication import (
    PublicationDecision,
    SentenceBinding,
    decide_visual_publication,
    sentence_digest,
)
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import MeetingEvidence, PerceptionContext
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.revalidation import SceneRevalidator
from fakeyou.perception.timeline import CameraSample, MeetingTimeline
from tests._perception_fakes import START, keyframe, reading, slide_jpeg, vision_model
from tests.test_camera_evidence import jpeg_of

CAPTURED_AT = 100.0
ROADMAP = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])
BUDGET = slide_jpeg("Budget", ["Total: 4 million"], background=(90, 20, 20))


class Clock:
    def __init__(self) -> None:
        self.at = CAPTURED_AT

    def __call__(self) -> float:
        return self.at


@dataclass
class Meeting:
    inspections: tuple[VisualInspection, ...] = (
        VisualInspection(verdict="supported", answer="Beta is 17 March 2027."),
    )
    look_at: float = CAPTURED_AT + 1.0
    write_at: float = CAPTURED_AT + 2.0
    before_write: Callable[["Meeting"], None] | None = None
    clock: Clock = field(default_factory=Clock)
    page_clock: RemoteClock = field(init=False)
    timeline: MeetingTimeline = field(init=False)
    perception: PerceptionContext = field(init=False)
    bindings: list[SentenceBinding] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.page_clock = RemoteClock(now=self.clock, drift_ppm=0)
        self.timeline = MeetingTimeline()
        self.timeline.capture_clock = lambda captured_at: self.page_clock.map(
            captured_at.timestamp() * 1000
        )
        self.advance(CAPTURED_AT)
        presenter = self.timeline.participant("device-ayu", "Ayu")
        self.timeline.participant("device-budi", "Budi")
        self.timeline.show_screen(keyframe(ROADMAP, seconds=0), presenter, data=ROADMAP)
        self.timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March"))
        self.timeline.camera_frames.put(
            "device-budi",
            CameraSample(
                data=jpeg_of((120, 120, 120)),
                stream_id="stream-budi",
                captured_at=START,
                epoch=0,
                seen_at=CAPTURED_AT,
            ),
        )
        interpreter = PerceptionInterpreter(
            vision_model(inspections=self.inspections),
            PerceptionBudget(budget_usd=1.0, max_calls=10),
            max_concurrent=1,
            snapshots=self.timeline.camera_frames,
            monotonic=self.clock,
            provider="fake/vision",
        )
        self.perception = PerceptionContext(
            self.timeline,
            interpreter,
            max_chars=6000,
            clock=lambda: START,
            camera_monotonic=self.clock,
        )

    def advance(self, to: float) -> None:
        self.clock.at = to
        page_ms = (START + timedelta(seconds=to - CAPTURED_AT)).timestamp() * 1000
        assert self.page_clock.observe(ClockExchange(page_ms, to - 0.01, to))

    async def decide(self, *, needs: tuple[str, ...]) -> PublicationDecision:
        self.advance(self.look_at)
        gathered = await MeetingEvidence(self.perception)(
            "What is Budi wearing?" if "participant" in needs else "What is shown?", needs
        )
        binding = SentenceBinding(
            "publication",
            "meeting",
            "turn",
            "turn",
            "request",
            0,
            sentence_digest("Observed finding."),
            gathered.gather_attempt_id,
            needs,
            gathered.provenance,
            "current",
        )
        self.bindings.append(binding)
        self.advance(self.write_at)
        if self.before_write is not None:
            self.before_write(self)
        validator = SceneRevalidator(self.perception, now=self.clock)
        return decide_visual_publication(
            binding,
            {item.kind: validator.validate(item) for item in binding.evidence},
            at=self.clock(),
            clock_domain=LOCAL_MONOTONIC,
        )


def show_budget(meeting: Meeting) -> None:
    presenter = meeting.timeline.participant("device-ayu", "Ayu")
    meeting.timeline.show_screen(keyframe(BUDGET, seconds=1.5), presenter, data=BUDGET)


def stop_sharing(meeting: Meeting) -> None:
    meeting.timeline.stop_screen(START + timedelta(seconds=1.5))
