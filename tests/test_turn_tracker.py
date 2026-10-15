from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.turn_tracker import detect_turn_cue, empty_turn_state, note_event


def _event(id: str, text: str, participant_id: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=id,
        timestamp="2026-10-15T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def test_explicit_cue_with_name_sets_my_turn():
    cue, target = detect_turn_cue("Harun, you're up", ("harun",))
    assert (cue, target) == ("explicit", "harun")


def test_quote_about_twin_is_not_a_cue():
    cue, _ = detect_turn_cue('she said "Harun agrees"', ("harun",))
    assert cue == "none"


def test_unknown_ids_never_join_spoken_order():
    state = empty_turn_state()
    state = note_event(state, _event("t1", "hello", "unknown"), ("harun",))
    state = note_event(state, _event("t2", "hello", "unknown"), ("harun",))
    assert state.spoken_ids == ()
    assert state.my_turn_likely is False
