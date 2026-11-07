from collections.abc import Callable
from dataclasses import dataclass, field

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.floor import SpeechFloor
from fakeyou.conversation.participation_graph import TurnJudging
from fakeyou.conversation.reply_draft import ReplyDrafts
from fakeyou.conversation.thinking_cue import ThinkingCue
from fakeyou.conversation.turn_graph import (
    CommandPort,
    ConversationPhase,
    EvidencePort,
    PlannedTurn,
    Responder,
    SentenceLimits,
    Speaker,
    SpeechGraph,
    StateSpeech,
    StreamTurnFactory,
    TurnPlanning,
    WholeResponder,
    WholeTurnFactory,
    drain_turns,
)
from fakeyou.conversation.turn_queue import AdmittedFinals, BargeLatch, PendingFinals
from fakeyou.conversation.turn_recovery import TurnRecovery
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import JournalSink


@dataclass
class TurnTestContext:
    speaker: Speaker
    journal: JournalSink
    speech: SpeechGraph | None = None
    responder: Responder | None = None
    judging: TurnJudging | None = None
    sentences: SentenceLimits | None = None
    whole: WholeResponder | None = None
    whole_turn: WholeTurnFactory | None = None
    stream_turn: StreamTurnFactory | None = None
    chat_turn: WholeTurnFactory | None = None
    planning: TurnPlanning | None = None
    commands: CommandPort | None = None
    state_speech: StateSpeech | None = None
    evidence: EvidencePort | None = None
    cue: ThinkingCue | None = None
    drafts: ReplyDrafts | None = None
    faults: FaultReporter | None = None
    others_speaking: Callable[[], bool] | None = None
    floor: SpeechFloor = field(default_factory=lambda: SpeechFloor(generation=4))
    queue: PendingFinals = field(default_factory=PendingFinals)
    admitted: AdmittedFinals = field(default_factory=AdmittedFinals)
    barge: BargeLatch = field(default_factory=BargeLatch)
    plans: dict[str, PlannedTurn] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    moves: list[tuple[ConversationPhase, str]] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)
    barge_in: bool = True
    shadow: bool = False
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    is_closed: bool = False
    is_paused: bool = False
    is_bound: bool = True
    is_busy: bool = False
    requested: bool = False

    def __post_init__(self) -> None:
        self._recovery = TurnRecovery(
            journal=self.journal,
            faults=self.faults,
            language=DEFAULTS.speech.language,
            is_closed=lambda: self.is_closed,
        )

    @property
    def recovery(self) -> TurnRecovery:
        assert self._recovery is not None
        return self._recovery

    def early_gate(self, transcript_id: str) -> None:
        del transcript_id
        return None

    @property
    def note_spoken(self) -> Callable[[str], None]:
        return self.notes.append

    def request_pending(self) -> None:
        self.requested = True

    async def start_pending(self) -> None:
        if self.speech is None or not self.requested:
            return
        self.requested = False
        await drain_turns(self, self.speech, self.queue)

    def move(self, phase: ConversationPhase, detail: str) -> None:
        self.moves.append((phase, detail))

    def fail(self, cause: Exception) -> None:
        self.failures.append(cause)

    def raise_if_failed(self) -> None:
        if self.failures:
            raise self.failures[-1]
