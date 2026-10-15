import pytest

from fakeyou.audio.probe_replay import (
    align_turns,
    csrc_lag_ms,
    merge_levels,
    page_to_mono,
    reference_precision,
    source_sets_by_speaker,
)


def test_align_turns_matches_fragments_to_transcripts() -> None:
    transcripts = [
        {"text": "good morning everyone", "wall": "2026-10-05T10:00:01+00:00"},
        {"text": "orchid demo next week", "wall": "2026-10-05T10:00:05+00:00"},
    ]
    turns = [
        {"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "GOOD MORNING"},
        {"turn": 2, "speaker": "Budi", "kind": "solo", "fragment": "orchid"},
    ]
    aligned = align_turns(transcripts, turns)
    assert [(entry["turn"], entry["speaker"]) for entry in aligned] == [
        (1, "Rani"),
        (2, "Budi"),
    ]


def test_align_turns_skips_missed_turn_without_consuming() -> None:
    transcripts = [
        {"text": "good morning everyone", "wall": "2026-10-05T10:00:01+00:00"},
        {"text": "orchid demo next week", "wall": "2026-10-05T10:00:05+00:00"},
    ]
    turns = [
        {"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "morning"},
        {"turn": 2, "speaker": "Nobody", "kind": "solo", "fragment": "never said"},
        {"turn": 3, "speaker": "Budi", "kind": "solo", "fragment": "orchid"},
    ]
    aligned = align_turns(transcripts, turns)
    assert [(entry["turn"], entry["speaker"]) for entry in aligned] == [
        (1, "Rani"),
        (3, "Budi"),
    ]


def test_csrc_lag_measures_first_active_sample_after_vad_start() -> None:
    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.0}]},
        {"page_ms": 1050.0, "sources": [{"source": 3, "audioLevel": 0.4}]},
        {"page_ms": 1100.0, "sources": [{"source": 3, "audioLevel": 0.5}]},
    ]
    anchors = [(1000.0, 5000.0)]
    lag = csrc_lag_ms(samples, anchors, vad_start_mono_ms=5025.0, threshold=0.01)
    assert lag == 25.0


def test_source_sets_and_reference_precision_from_replay() -> None:
    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.4}]},
        {"page_ms": 2000.0, "sources": [{"source": 9, "audioLevel": 0.6}]},
    ]
    anchors = [(1000.0, 5000.0)]
    windows = [
        {"speaker": "Rani", "kind": "solo", "start_mono_ms": 4900.0, "end_mono_ms": 5100.0},
        {"speaker": "Budi", "kind": "solo", "start_mono_ms": 5900.0, "end_mono_ms": 6100.0},
    ]
    assert source_sets_by_speaker(samples, anchors, windows) == {
        "Rani": (3,),
        "Budi": (9,),
    }
    report = reference_precision(samples, anchors, windows)
    assert report["precision"] == 1.0
    assert report["overlap_unknown_rate"] is None


def test_merge_levels_drops_malformed_entries() -> None:
    merged = merge_levels(
        [
            {"page_ms": 1500.0, "source": -1, "audioLevel": 0.9},
            {"page_ms": 1500.0, "source": 0, "audioLevel": float("nan")},
            {"page_ms": 1500.0, "source": 5},
            "broken",
            {"page_ms": 1500.0, "source": 2, "audioLevel": 0.6},
        ]
    )
    assert merged == [
        {
            "page_ms": 1500.0,
            "sources": [{"source": 2, "audioLevel": 0.6}],
        }
    ]


def test_dominant_sources_ignore_quiet_bleed() -> None:
    from fakeyou.audio.probe_replay import MonoWindow, dominant_sources_in_window

    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.34}]},
        {"page_ms": 1050.0, "sources": [{"source": 5, "audioLevel": 0.06}]},
    ]
    anchors = [(1000.0, 5000.0)]
    window = MonoWindow(4900.0, 5200.0)
    assert dominant_sources_in_window(samples, anchors, window) == {3}
    assert dominant_sources_in_window(samples, anchors, window, margin=0.1) == {
        3,
        5,
    }


def test_reference_precision_uses_dominant_margin() -> None:
    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.34}]},
        {"page_ms": 1050.0, "sources": [{"source": 5, "audioLevel": 0.06}]},
        {"page_ms": 2000.0, "sources": [{"source": 5, "audioLevel": 0.14}]},
        {"page_ms": 2050.0, "sources": [{"source": 3, "audioLevel": 0.009}]},
    ]
    anchors = [(1000.0, 5000.0)]
    windows = [
        {"speaker": "Rani", "kind": "solo", "start_mono_ms": 4900.0, "end_mono_ms": 5200.0},
        {"speaker": "Budi", "kind": "solo", "start_mono_ms": 5900.0, "end_mono_ms": 6200.0},
    ]
    report = reference_precision(samples, anchors, windows, margin=0.5)
    assert report["precision"] == 1.0


def test_page_to_mono_rejects_empty_anchors() -> None:
    with pytest.raises(ValueError, match="page clock mapping needs at least one anchor"):
        page_to_mono(1000.0, [])
