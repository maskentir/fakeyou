from datetime import datetime
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScreenContent = Literal[
    "slide",
    "document",
    "spreadsheet",
    "code",
    "web_page",
    "dashboard",
    "diagram",
    "video",
    "desktop",
    "meeting_ui",
    "blank",
    "loading",
    "other",
]
NOISE_CONTENT: frozenset[ScreenContent] = frozenset({"meeting_ui", "blank", "loading"})
SignalKind = Literal[
    "joined",
    "left",
    "presenting_started",
    "presenting_stopped",
    "hand_raised",
    "hand_lowered",
    "reaction",
    "camera_on",
    "camera_off",
]
SAME_CONTENT_RATIO = 0.85
SCREEN_REF_PREFIX = "screen#"
CHAT_REF_PREFIX = "chat#"
PERSON_REF_PREFIX = "person#"


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


class Percept(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScreenFigure(Percept):
    label: str = Field(description="What the number or date measures, as written on screen.")
    value: str = Field(description="The number, date or amount exactly as written on screen.")


class ScreenReading(Percept):
    content_kind: ScreenContent = Field(
        description=(
            "What the shared screen shows. Use meeting_ui when it shows a video call window, "
            "loading when content is still loading, and blank when nothing readable is shown."
        )
    )
    title: str = Field(description="The visible title or heading, or a short neutral label.")
    text: str = Field(
        description="All readable text on screen, verbatim, in reading order, original language."
    )
    key_points: list[str] = Field(
        default_factory=list, description="The main points a participant would take away."
    )
    figures: list[ScreenFigure] = Field(
        default_factory=list, description="Every number, date, price or amount with its label."
    )
    visuals: list[str] = Field(
        default_factory=list,
        description="Charts, diagrams, images or highlighted elements and what they show.",
    )
    people: list[str] = Field(
        default_factory=list, description="Names of people visible as text on screen."
    )

    @property
    def is_noise(self) -> bool:
        return self.content_kind in NOISE_CONTENT

    def matches(self, other: "ScreenReading") -> bool:
        if self.is_noise or other.is_noise or _folded(self.title) != _folded(other.title):
            return False
        matcher = SequenceMatcher(None, _folded(self.text), _folded(other.text))
        return matcher.quick_ratio() >= SAME_CONTENT_RATIO and matcher.ratio() >= SAME_CONTENT_RATIO


class ParticipantRef(Percept):
    ref: str
    participant_id: str
    name: str


class Keyframe(Percept):
    sha256: str
    signature: bytes = Field(repr=False, exclude=True)
    width: int
    height: int
    captured_at: datetime
    path: str | None = None


class ScreenVisit(Percept):
    started_at: datetime
    ended_at: datetime | None = None


class ScreenSegment(Percept):
    ref: str
    keyframe: Keyframe
    presenter: ParticipantRef | None
    visits: tuple[ScreenVisit, ...]
    reading: ScreenReading | None = None
    reading_failed: bool = False

    @property
    def first_seen(self) -> datetime:
        return self.visits[0].started_at

    @property
    def last_seen(self) -> datetime:
        last = self.visits[-1]
        return last.ended_at or last.started_at

    @property
    def showing(self) -> bool:
        return self.visits[-1].ended_at is None


class IncomingChat(Percept):
    message_id: str
    sender: ParticipantRef
    text: str
    sent_at: datetime
    own: bool = False
    backlog: bool = False


class ChatLine(IncomingChat):
    ref: str


class MeetingSignal(Percept):
    kind: SignalKind
    participant: ParticipantRef
    at: datetime
    detail: str | None = None


class CameraGlance(Percept):
    participant: ParticipantRef
    at: datetime
    description: str
