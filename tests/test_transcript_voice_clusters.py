from fakeyou.foundation.transcript import CaptureContext, TranscriptUpdate, transcript_event


def test_transcript_event_carries_the_diarized_voice_clusters() -> None:
    update = TranscriptUpdate(
        text="Good morning.",
        final=True,
        timestamp="2026-10-15T00:00:00+00:00",
        voice_clusters=("A", "B"),
    )

    event = transcript_event(update, "unknown", None, CaptureContext(""))

    assert event.voice_clusters == ("A", "B")


def test_voice_clusters_do_not_change_the_transcript_identity() -> None:
    plain = TranscriptUpdate(
        text="Good morning.", final=True, timestamp="2026-10-15T00:00:00+00:00"
    )
    labelled = TranscriptUpdate(
        text="Good morning.",
        final=True,
        timestamp="2026-10-15T00:00:00+00:00",
        voice_clusters=("A",),
    )

    assert (
        transcript_event(plain, "unknown", None, CaptureContext("")).id
        == transcript_event(labelled, "unknown", None, CaptureContext("")).id
    )


def test_refs_survive_into_transcript_event() -> None:
    update = TranscriptUpdate(text="look at this slide", final=True, capture_ms=3_000.0)
    capture = CaptureContext(capture_ms=3_000.0, visual_refs=("f-early",))
    event = transcript_event(update, "speaker-a", None, capture)
    assert event.visual_refs == ("f-early",)
