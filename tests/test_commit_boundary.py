import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import override

import pytest

from fakeyou.audio.commit import CommitProgress, WriteWithheld
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.publication import (
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
)
from fakeyou.observability.participant_audio import measure_participant_publication
from fakeyou.perception.timeline import SCREEN_SOURCE_ID
from fakeyou.speech.contract import PlaybackReceipt
from tests._audio_support import FakeWriter
from tests._participant_audio_fakes import ScriptedParticipantObserver, granted
from tests._perception_fakes import START
from tests.test_publication_activation import (
    CAPTURED_AT,
    Meeting,
    Turn,
    ask,
    show_budget,
)
from tests.test_publication_freshness import confirm_same
from tests.test_publication_measurement import _post

Change = Callable[[Meeting], None]


def edit_slide(meeting: Meeting) -> None:
    latest = meeting.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    captured_at = START + timedelta(seconds=meeting.clock.at - CAPTURED_AT)
    meeting.timeline.confirm(
        replace(latest.record, sequence=latest.record.sequence + 1, digest="0" * 64),
        captured_at=captured_at,
        capture=meeting.page_clock.map(captured_at.timestamp() * 1000),
    )


def confirm_now(meeting: Meeting) -> None:
    confirm_same(meeting, at=meeting.clock.at)


def context_of(meeting: Meeting) -> PublicationContext:
    context = meeting.publication_context
    assert context is not None
    return context


def after_preliminary(meeting: Meeting, change: Change) -> None:
    context = context_of(meeting)
    inner = context.gate
    assert inner is not None

    async def gate(binding: SentenceBinding) -> PublicationDecision | None:
        decision = await inner(binding)
        change(meeting)
        return decision

    meeting.publication_context = replace(context, gate=gate)
    meeting.engine.configure_publication(meeting.publication_context)


def during_preliminary(meeting: Meeting, change: Change) -> None:
    context = context_of(meeting)
    inner = context.gate
    assert inner is not None

    async def gate(binding: SentenceBinding) -> PublicationDecision | None:
        change(meeting)
        return await inner(binding)

    meeting.publication_context = replace(context, gate=gate)
    meeting.engine.configure_publication(meeting.publication_context)


async def beta_turn(meeting: Meeting) -> Turn:
    return await ask(meeting, "When is beta?", needs=("screen",))


def terminal(meeting: Meeting) -> PlaybackReceipt:
    [receipt] = meeting.settled
    return receipt


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (show_budget, "invalid:scene_changed"),
        (edit_slide, "unknown:content_changed_unverified"),
    ],
)
@pytest.mark.parametrize("position", [during_preliminary, after_preliminary])
async def test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer(
    position: Callable[[Meeting, Change], None], change: Change, reason: str
) -> None:
    meeting = Meeting()
    position(meeting, change)

    turn = await beta_turn(meeting)

    assert meeting.prevented == reason
    assert meeting.writer.written == []
    assert turn.counted == []


async def test_exact_sample_before_the_commit_is_recorded_on_the_committed_decision() -> None:
    meeting = Meeting()
    decisions: list[PublicationDecision | None] = []
    context = context_of(meeting)
    commit = context.commit
    assert commit is not None

    def recording(binding: SentenceBinding) -> PublicationDecision | None:
        decisions.append(commit(binding))
        return decisions[-1]

    meeting.publication_context = replace(context, commit=recording)
    meeting.engine.configure_publication(meeting.publication_context)
    after_preliminary(meeting, confirm_now)

    await beta_turn(meeting)

    [decision] = decisions
    assert decision is not None
    [basis] = decision.basis
    assert (decision.validity, basis.match) == ("valid", "exact")
    assert basis.confirmation is not None
    assert basis.confirmation.record.sequence == 2
    assert terminal(meeting).outcome == "completed"


async def test_confirmation_fresh_at_the_preliminary_decision_and_old_at_the_commit() -> None:
    written = CAPTURED_AT + 30.0

    def time_passes(rig: Meeting) -> None:
        rig.advance(written + 5.5)

    meeting = Meeting(
        look_at=written - 1.0,
        write_at=written,
        before_write=lambda rig: confirm_same(rig, at=written - 0.4),
    )
    after_preliminary(meeting, time_passes)

    await beta_turn(meeting)

    assert meeting.prevented == "invalid:confirmation_too_old"


async def test_freeze_after_the_preliminary_decision_commits_after_the_thaw() -> None:
    meeting = Meeting()

    def freeze_then_change(rig: Meeting) -> None:
        rig.engine.freeze()
        loop = asyncio.get_running_loop()
        loop.call_soon(show_budget, rig)
        loop.call_soon(rig.engine.thaw)

    after_preliminary(meeting, freeze_then_change)

    await beta_turn(meeting)

    assert meeting.prevented == "invalid:scene_changed"
    assert meeting.writer.written == []


async def test_freeze_without_a_change_is_published_after_the_thaw() -> None:
    meeting = Meeting()

    def freeze_briefly(rig: Meeting) -> None:
        rig.engine.freeze()
        asyncio.get_running_loop().call_soon(rig.engine.thaw)

    after_preliminary(meeting, freeze_briefly)

    await beta_turn(meeting)

    assert terminal(meeting).outcome == "completed"


class ProbedWriter(FakeWriter):
    def __init__(self) -> None:
        super().__init__()
        self.probe_ran_before_handoff: bool | None = None
        self.probed = False

    def probe(self) -> None:
        self.probed = True

    @override
    async def write(self, pcm: bytes) -> None:
        if self.probe_ran_before_handoff is None:
            self.probe_ran_before_handoff = self.probed
        await super().write(pcm)


async def test_no_suspension_point_lies_between_the_final_evaluation_and_the_handoff() -> None:
    writer = ProbedWriter()
    meeting = Meeting(writer=writer)
    context = context_of(meeting)
    commit = context.commit
    assert commit is not None

    def probing(binding: SentenceBinding) -> PublicationDecision | None:
        decision = commit(binding)
        asyncio.get_running_loop().call_soon(writer.probe)
        return decision

    meeting.publication_context = replace(context, commit=probing)
    meeting.engine.configure_publication(meeting.publication_context)

    await beta_turn(meeting)

    assert writer.probe_ran_before_handoff is False
    assert writer.probed
    assert terminal(meeting).outcome == "completed"


class ChangingWriter(FakeWriter):
    def __init__(self, change: Change, *, after_writes: int = 0) -> None:
        super().__init__()
        self.change = change
        self.meeting: Meeting | None = None
        self._remaining = after_writes

    @override
    async def write(self, pcm: bytes) -> None:
        if self._remaining == 0 and self.meeting is not None:
            self.change(self.meeting)
        self._remaining -= 1
        await super().write(pcm)


def changing_meeting(change: Change, *, after_writes: int = 0) -> Meeting:
    writer = ChangingWriter(change, after_writes=after_writes)
    meeting = Meeting(writer=writer)
    writer.meeting = meeting
    return meeting


async def test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone() -> None:
    meeting = changing_meeting(show_budget)

    turn = await beta_turn(meeting)

    receipt = terminal(meeting)
    assert receipt.outcome == "completed"
    assert receipt.commit is not None
    assert receipt.commit.evaluations == 1
    [sample] = receipt.commit.post_commit
    assert (sample.role, sample.subject_id) == ("screen", SCREEN_SOURCE_ID)
    assert sample.before_first_write is True
    assert sample.captured_before_commit is True
    assert receipt.publication_id == turn.meeting.bindings[0].publication_id
    assert len(turn.counted) == 1


async def test_sample_captured_after_the_commit_is_marked_as_such() -> None:
    def later_sample(rig: Meeting) -> None:
        rig.advance(rig.clock.at + 0.1)
        edit_slide(rig)

    meeting = changing_meeting(later_sample)

    await beta_turn(meeting)

    commit = terminal(meeting).commit
    assert commit is not None
    [sample] = commit.post_commit
    assert sample.captured_before_commit is False


async def test_change_after_the_first_write_does_not_rewrite_the_decision() -> None:
    meeting = changing_meeting(show_budget, after_writes=1)

    await beta_turn(meeting)

    started = next(item for item in meeting.receipts if item.outcome == "audio_started")
    receipt = terminal(meeting)
    assert started.commit is not None
    assert receipt.commit is not None
    assert receipt.outcome == "completed"
    assert receipt.commit.evaluated_at == started.commit.evaluated_at
    assert started.commit.post_commit == ()
    [sample] = receipt.commit.post_commit
    assert sample.before_first_write is False


class StalledFirstWrite(FakeWriter):
    def __init__(self) -> None:
        super().__init__()
        self.stalled = asyncio.Event()

    @override
    async def write(self, pcm: bytes) -> None:
        self.stalled.set()
        self.release.clear()
        try:
            await super().write(pcm)
        except asyncio.CancelledError:
            self.release.set()
            raise


async def test_committed_attempt_cancelled_before_its_first_write_is_not_spoken() -> None:
    writer = StalledFirstWrite()
    meeting = Meeting(writer=writer)
    asking = asyncio.create_task(beta_turn(meeting))
    await writer.stalled.wait()

    await meeting.engine.stop()
    turn = await asking

    receipt = terminal(meeting)
    assert receipt.outcome == "dropped_before_audio"
    assert receipt.local_audio_seconds == 0
    assert not receipt.local_audio_written
    assert receipt.commit is not None
    assert receipt.commit.accepted_at is not None
    assert receipt.commit.first_written_at is None
    assert turn.counted == []
    [binding] = meeting.bindings
    result = measure_participant_publication(
        binding,
        None,
        decision=PublicationDecision("valid", "ok", meeting.write_at, LOCAL_MONOTONIC),
        clock_domain=LOCAL_MONOTONIC,
        receipt=receipt,
    )
    assert result["status"] == "not_measurable"


async def test_committed_attempt_whose_first_write_fails_is_not_spoken() -> None:
    meeting = Meeting(writer=FakeWriter(error=OSError("device gone")))

    turn = await beta_turn(meeting)

    failed = [event for event in meeting.events if event.kind == "playback_failed"]
    assert len(failed) == 1
    receipt = failed[0].receipt
    assert receipt is not None
    assert receipt.local_audio_seconds == 0
    assert not receipt.local_audio_written
    assert receipt.commit is not None
    assert receipt.commit.first_written_at is None
    assert turn.counted == []


class DeadlineFakeWriter(FakeWriter):
    def __init__(self, meeting_clock_step: float, *, withhold: int, change: Change | None = None):
        super().__init__()
        self.step = meeting_clock_step
        self.withhold = withhold
        self.change = change
        self.meeting: Meeting | None = None
        self.deadlines: list[float] = []

    async def write_before(self, pcm: bytes, deadline: float) -> None:
        meeting = self.meeting
        assert meeting is not None
        self.deadlines.append(deadline)
        if self.withhold > 0:
            self.withhold -= 1
            if self.change is not None:
                self.change(meeting)
            meeting.advance(meeting.clock.at + self.step)
            if meeting.clock.at > deadline:
                raise WriteWithheld
        await self.write(pcm)


def deadline_meeting(*, withhold: int, change: Change | None = None) -> Meeting:
    writer = DeadlineFakeWriter(0.3, withhold=withhold, change=change)
    meeting = Meeting(writer=writer)
    writer.meeting = meeting
    return meeting


async def test_first_write_delayed_past_the_validation_window_is_withheld() -> None:
    meeting = deadline_meeting(withhold=2)

    turn = await beta_turn(meeting)

    assert meeting.prevented == "unknown:write_delayed_past_validation"
    assert meeting.writer.written == []
    assert turn.counted == []
    receipt = terminal(meeting)
    assert receipt.commit is not None
    assert receipt.commit.evaluations == 2
    assert receipt.commit.first_written_at is None


async def test_withheld_write_is_re_evaluated_and_published_when_still_valid() -> None:
    meeting = deadline_meeting(withhold=1)

    turn = await beta_turn(meeting)

    receipt = terminal(meeting)
    writer = meeting.writer
    assert isinstance(writer, DeadlineFakeWriter)
    assert receipt.outcome == "completed"
    assert receipt.commit is not None
    assert receipt.commit.evaluations == 2
    assert receipt.commit.fresh_at_write_unproven is False
    assert writer.deadlines[1] == pytest.approx(writer.deadlines[0] + 0.3)
    assert receipt.commit.first_written_at == pytest.approx(receipt.commit.evaluated_at)
    assert len(turn.counted) == 1


async def test_withheld_write_whose_scene_changed_during_the_delay_is_prevented() -> None:
    meeting = deadline_meeting(withhold=1, change=show_budget)

    await beta_turn(meeting)

    assert meeting.prevented == "invalid:scene_changed"
    assert meeting.writer.written == []


async def test_deadline_is_the_validation_allowance_after_the_final_evaluation() -> None:
    meeting = deadline_meeting(withhold=0)

    await beta_turn(meeting)

    receipt = terminal(meeting)
    writer = meeting.writer
    assert isinstance(writer, DeadlineFakeWriter)
    assert receipt.commit is not None
    assert writer.deadlines == [pytest.approx(receipt.commit.evaluated_at + 0.25)]


def slow_plain_writer_meeting(delay: float) -> Meeting:
    def time_passes(rig: Meeting) -> None:
        rig.advance(rig.clock.at + delay)

    return changing_meeting(time_passes)


async def test_late_write_that_could_not_be_prevented_is_reported_and_marked_unproven() -> None:
    meeting = slow_plain_writer_meeting(0.3)

    turn = await beta_turn(meeting)

    receipt = terminal(meeting)
    assert receipt.outcome == "completed"
    assert receipt.local_audio_written
    assert receipt.commit is not None
    assert receipt.commit.fresh_at_write_unproven is True
    assert receipt.commit.first_written_at == pytest.approx(receipt.commit.evaluated_at + 0.3)
    [binding] = meeting.bindings
    observer = ScriptedParticipantObserver(
        consented=granted(clock_domain=LOCAL_MONOTONIC),
        heard_from=meeting.write_at + 0.4,
        heard_until=meeting.write_at + 0.9,
        clock_domain=LOCAL_MONOTONIC,
    )
    result = measure_participant_publication(
        binding,
        observer,
        decision=PublicationDecision("valid", "ok", meeting.write_at, LOCAL_MONOTONIC),
        clock_domain=LOCAL_MONOTONIC,
        receipt=receipt,
        queried_at=meeting.write_at + 1.0,
        post_validations=_post(),
    )
    assert "compliant" not in result
    assert result["status"] == "not_measurable"
    assert len(turn.counted) == 1


async def test_write_inside_the_validation_window_is_not_marked_unproven() -> None:
    meeting = slow_plain_writer_meeting(0.2)

    await beta_turn(meeting)

    commit = terminal(meeting).commit
    assert commit is not None
    assert commit.fresh_at_write_unproven is False


@pytest.mark.parametrize(
    ("evaluated_at", "first_written_at"),
    [(100.0, float("nan")), (float("inf"), 100.1), (100.0, 99.0), (100.0, 100.26)],
)
def test_missing_or_inconsistent_instants_leave_the_bound_unproven(
    evaluated_at: float, first_written_at: float
) -> None:
    progress = CommitProgress(
        decision=PublicationDecision("valid", "ok", 100.0, LOCAL_MONOTONIC),
        evaluated_at=evaluated_at,
        first_written_at=first_written_at,
        bounded=True,
    )

    timing = progress.timing(PublicationContext("meeting", LOCAL_MONOTONIC))

    assert timing is not None
    assert timing.fresh_at_write_unproven is True


async def test_scope_without_a_freshness_claim_is_not_bounded() -> None:
    meeting = slow_plain_writer_meeting(0.3)

    await ask(meeting, "How are you?", reply="I am well.")

    commit = terminal(meeting).commit
    assert commit is not None
    assert commit.fresh_at_write_unproven is False
