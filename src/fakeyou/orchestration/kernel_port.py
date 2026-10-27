import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudge, TurnJudgement
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.identity.identity import SpeakerRow
from fakeyou.observability.journal import Journal
from fakeyou.perception.roster import MeetRoster
from fakeyou.session.contracts import (
    AudioStreamObserved,
    CallState,
    CallStateObserved,
    ChatObserved,
    Endpoint,
    Observation,
    OwnerCommand,
    OwnerControlObserved,
    ParticipantCountObserved,
    PlaybackObserved,
    PlaybackPhase,
    RosterObserved,
    SessionEvent,
    Source,
    SourceHealthObserved,
    SourceStatus,
    TranscriptObserved,
    VoiceTrackObserved,
)
from fakeyou.session.health import Mode, modes, side_effects_allowed
from fakeyou.session.identity import PresenceFact, attribute, presence
from fakeyou.session.kernel import Mailbox, SessionKernel
from fakeyou.session.policy import UNATTRIBUTED, Actor
from fakeyou.session.world import SessionState

_PLAYBACK_PHASES: dict[str, PlaybackPhase] = {
    "playback_started": "started",
    "playback_finished": "finished",
    "playback_dropped": "dropped",
    "playback_failed": "failed",
}


def roster_observations(roster: MeetRoster) -> tuple[Observation, ...]:
    endpoints = tuple(
        Endpoint(
            device_id=user.device_id,
            display_name=user.name,
            present=user.present,
            parent_device_id=user.parent_device_id,
            current_user=user.current_user,
            host=user.host,
        )
        for user in roster.users()
    )
    streams = tuple(
        AudioStreamObserved(device_id=device, stream_id=f"csrc:{csrc}")
        for csrc, device in sorted(roster.audio_sources().items())
    )
    return (RosterObserved(endpoints=endpoints, synced=roster.synced), *streams)


def voice_track_observation(row: SpeakerRow) -> VoiceTrackObserved:
    return VoiceTrackObserved(
        track_id=row.uuid,
        device_id=row.device,
        stream_id=f"csrc:{row.csrcs[0]}" if row.csrcs else None,
        enrolled_person=row.verified_enrollment,
    )


def transcript_observation(event: TranscriptEvent, track_id: str | None) -> TranscriptObserved:
    return TranscriptObserved(
        transcript_id=event.id,
        text=event.text,
        final=event.finalized,
        track_id=track_id or None,
    )


class KernelPort:
    def __init__(
        self,
        kernel: SessionKernel,
        mailbox: Mailbox | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        journal: Journal | None = None,
    ) -> None:
        self._kernel = kernel
        self._journal = journal
        self._modes: frozenset[Mode] = frozenset()
        self._health: dict[Source, SourceStatus] = {}
        kernel.subscribe(self._note_modes)
        self._mailbox = mailbox or Mailbox()
        self._clock = clock
        self._browser_epoch = 0
        self._clip = 0
        self._interims = 0
        self._playing: str | None = None
        self._dom_remote: int | None = None
        self._call: CallState | None = None

    @classmethod
    def for_meeting(cls, meeting_id: str, journal: Journal | None = None) -> "KernelPort":
        return cls(
            SessionKernel(session_id=meeting_id, attempt_id=uuid.uuid4().hex), journal=journal
        )

    def modes(self) -> frozenset[Mode]:
        return modes(self._kernel.state)

    def side_effects_allowed(self) -> bool:
        return side_effects_allowed(self.modes())

    def _note_modes(self, state: SessionState, _event: SessionEvent) -> None:
        current = modes(state)
        if current == self._modes:
            return
        previous, self._modes = self._modes, current
        if self._journal is not None:
            self._journal.record(
                "session_mode_transitioned",
                source="runtime",
                entered=sorted(current - previous),
                left=sorted(previous - current),
                kernel_seq=state.seq,
            )

    @property
    def attempt_id(self) -> str:
        return self._kernel.attempt_id

    @property
    def state(self) -> SessionState:
        return self._kernel.state

    def actor(self, source_id: str) -> Actor:
        state = self._kernel.state
        if "identity_degraded" in modes(state):
            return UNATTRIBUTED
        for line in reversed(state.dialogue.lines):
            if line.source_id != source_id:
                continue
            attribution = attribute(state, line.speaker)
            if attribution.device_id is not None and attribution.status in (
                "confirmed",
                "probable",
            ):
                return Actor("participant", attribution.device_id)
            return UNATTRIBUTED
        return UNATTRIBUTED

    def presence(self) -> PresenceFact:
        return presence(self._kernel.state)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        task = asyncio.create_task(self._kernel.run(self._mailbox), name="session-kernel")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def drain(self) -> None:
        while (event := self._mailbox.take_nowait()) is not None:
            self._kernel.note_mailbox(self._mailbox)
            self._kernel.accept(event)

    def observe(
        self,
        source: Source,
        observation: Observation,
        *,
        event_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self._mailbox.offer(
            SessionEvent(
                event_id=event_id or uuid.uuid4().hex,
                session_id=self._kernel.session_id,
                attempt_id=self._kernel.attempt_id,
                source=source,
                received_at=self._clock(),
                observation=observation,
                correlation_id=correlation_id,
            )
        )

    def roster(self, roster: MeetRoster) -> None:
        for observation in roster_observations(roster):
            self.observe("meet_protocol", observation)

    def transcript(self, event: TranscriptEvent, row: SpeakerRow | None) -> None:
        if row is not None:
            self.observe("speaker_registry", voice_track_observation(row))
        self.observe(
            "stt",
            transcript_observation(event, row.uuid if row is not None else None),
            event_id=f"{event.id}:{'final' if event.finalized else 'interim'}:{event.text}",
            correlation_id=event.id,
        )

    def interim(self, text: str) -> None:
        self._interims += 1
        self.observe(
            "stt",
            TranscriptObserved(transcript_id=f"interim-{self._interims}", text=text, final=False),
        )

    def chat(self, *, message_id: str, device_id: str, text: str, own: bool, backlog: bool) -> None:
        self.observe(
            "meet_protocol",
            ChatObserved(
                message_id=message_id, device_id=device_id, text=text, own=own, backlog=backlog
            ),
            event_id=f"chat:{message_id}",
        )

    def call_state(self, state: CallState, *, new_epoch: bool = False) -> None:
        if state == self._call and not new_epoch:
            return
        self._call = state
        if new_epoch or self._browser_epoch == 0:
            self._browser_epoch += 1
        self.observe("browser", CallStateObserved(state=state, browser_epoch=self._browser_epoch))

    def dom_remote(self, remote: int | None) -> None:
        if remote is None or remote == self._dom_remote:
            return
        self._dom_remote = remote
        self.observe("meet_dom", ParticipantCountObserved(remote=remote))

    def protocol_health(self, *, available: bool) -> None:
        self.source_health("meet_protocol", "healthy" if available else "stale")

    def memory_outcome(self, outcome: str) -> None:
        self.source_health("memory", "down" if outcome == "unavailable" else "healthy")

    def source_health(self, source: Source, status: SourceStatus) -> None:
        if self._health.get(source) == status:
            return
        self._health[source] = status
        self.observe(source, SourceHealthObserved(source=source, status=status))

    def playback(self, event: PlaybackEvent) -> None:
        phase = _PLAYBACK_PHASES.get(event.kind)
        if phase is None or event.cue:
            return
        if phase == "started":
            self._clip += 1
            self._playing = event.text
        elif phase == "dropped" and event.text == self._playing:
            phase = "interrupted"
            self._playing = None
        elif phase in ("finished", "failed"):
            self._playing = None
        self.source_health("playback", "down" if phase == "failed" else "healthy")
        self.observe(
            "playback",
            PlaybackObserved(
                utterance_id=f"clip-{self._clip}", sentence_index=0, phase=phase, text=event.text
            ),
        )

    def owner(self, command: OwnerCommand) -> None:
        self.observe("owner_control", OwnerControlObserved(command=command))


class HealthReportingJudge:
    def __init__(self, judge: TurnJudge, port: KernelPort) -> None:
        self._judge = judge
        self._port = port

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        try:
            judgement = await self._judge.judge(window)
        except Exception:
            self._port.source_health("model", "down")
            raise
        self._port.source_health("model", "healthy")
        return judgement
