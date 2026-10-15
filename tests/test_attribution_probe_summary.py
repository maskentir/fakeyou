import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fakeyou.audio.probe_replay import (
    summarize_probe_session,
)


def test_summarize_probe_session_reports_gate_metrics(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "vad",
                        "active": True,
                        "audio_ms": 100.0,
                        "mono_ms": 7400.0,
                        "wall": "2026-10-05T10:00:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "vad",
                        "active": False,
                        "audio_ms": 1100.0,
                        "mono_ms": 7700.0,
                        "wall": "2026-10-05T10:00:01+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "page",
                        "mono_ms": 8000.0,
                        "wall": "2026-10-05T10:00:03+00:00",
                        "page_ms": 2000.0,
                        "peer_connections": 1,
                        "samples": [
                            {
                                "page_ms": 1500.0,
                                "sources": [{"source": 3, "audioLevel": 0.4}],
                            },
                            {
                                "page_ms": 1600.0,
                                "sources": [{"source": 3, "audioLevel": 0.5}],
                            },
                        ],
                        "roster": [
                            {
                                "page_ms": 1500.0,
                                "tiles": [
                                    {
                                        "id": "a",
                                        "name": "Rani",
                                        "self": False,
                                        "speaking": {
                                            "es310d": True,
                                            "knownClass": False,
                                        },
                                        "muted": False,
                                    },
                                    {
                                        "id": "b",
                                        "name": "Rani Twin",
                                        "self": True,
                                        "speaking": {
                                            "es310d": False,
                                            "knownClass": False,
                                        },
                                        "muted": True,
                                    },
                                ],
                            }
                        ],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    record_path = tmp_path / "record.jsonl"
    record_path.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": "2026-10-05T10:00:02+00:00",
                "id": "t1",
                "spoken_at": "2026-10-05T10:00:02+00:00",
                "source": "meeting_audio",
                "text": "good morning all",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        ),
        encoding="utf-8",
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps([{"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "morning"}]),
        encoding="utf-8",
    )
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["turns"] == 1
    assert report["aligned"] == 1
    assert report["lag_ms"]["median"] == 100.0
    assert report["source_sets"] == {"Rani": [3]}
    assert report["reference"]["precision"] == 1.0
    assert report["markers"]["es310d"]["accuracy"] == 1.0
    assert report["mute"]["keeps_speaker"] == 1.0
    assert report["gates"]["name_precision"] is True


def test_summarize_skips_corrupt_lines_with_counts(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text("{bad json\n", encoding="utf-8")
    record_path = tmp_path / "record.jsonl"
    record_path.write_text("not json either\n", encoding="utf-8")
    truth_path = tmp_path / "truth.json"
    truth_path.write_text("[]", encoding="utf-8")
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["turns"] == 0
    assert report["skipped_lines"] == {"probe": 1, "record": 1}


def test_summarize_merges_per_track_levels_as_sources(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "vad",
                        "active": True,
                        "audio_ms": 100.0,
                        "mono_ms": 7400.0,
                        "wall": "2026-10-05T10:00:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "vad",
                        "active": False,
                        "audio_ms": 1100.0,
                        "mono_ms": 7700.0,
                        "wall": "2026-10-05T10:00:01+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "page",
                        "mono_ms": 8000.0,
                        "wall": "2026-10-05T10:00:03+00:00",
                        "page_ms": 2000.0,
                        "peer_connections": 1,
                        "receiver_count": 1,
                        "samples": [],
                        "levels": [
                            {"page_ms": 1500.0, "source": 0, "audioLevel": 0.7},
                            {"page_ms": 1600.0, "source": 0, "audioLevel": 0.8},
                        ],
                        "roster": [],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    record_path = tmp_path / "record.jsonl"
    record_path.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": "2026-10-05T10:00:02+00:00",
                "id": "t1",
                "spoken_at": "2026-10-05T10:00:02+00:00",
                "source": "meeting_audio",
                "text": "good morning all",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        ),
        encoding="utf-8",
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps([{"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "morning"}]),
        encoding="utf-8",
    )
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["aligned"] == 1
    assert report["lag_ms"]["median"] == 100.0
    assert report["source_sets"] == {"Rani": [0]}
    assert report["reference"]["precision"] == 1.0


def test_vad_windows_follow_mono_order_and_drop_inverted(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    edges = [
        {"kind": "vad", "active": False, "mono_ms": 3000.0, "wall": "2026-10-05T10:00:02+00:00"},
        {"kind": "vad", "active": True, "mono_ms": 1000.0, "wall": "2026-10-05T10:00:00+00:00"},
        {"kind": "vad", "active": True, "mono_ms": 4000.0, "wall": "2026-10-05T09:59:59+00:00"},
        {"kind": "vad", "active": False, "mono_ms": 4000.0, "wall": "2026-10-05T10:00:00+00:00"},
        {
            "kind": "page",
            "mono_ms": 0.0,
            "page_ms": 0.0,
            "peer_connections": 1,
            "samples": [
                {"page_ms": 1500.0, "sources": [{"source": 3, "audioLevel": 0.5}]},
                {"page_ms": 4000.0, "sources": [{"source": 9, "audioLevel": 0.5}]},
            ],
            "roster": [],
        },
    ]
    probe_path.write_text("\n".join(json.dumps(edge) for edge in edges), encoding="utf-8")
    record_path = tmp_path / "record.jsonl"
    transcripts = [
        ("2026-10-05T10:00:03+00:00", "alpha said"),
        ("2026-10-05T10:00:01+00:00", "bravo said"),
    ]
    record_path.write_text(
        "\n".join(
            json.dumps({"kind": "transcript", "spoken_at": wall, "text": text})
            for wall, text in transcripts
        ),
        encoding="utf-8",
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps(
            [
                {"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "alpha"},
                {"turn": 2, "speaker": "Budi", "kind": "solo", "fragment": "bravo"},
            ]
        ),
        encoding="utf-8",
    )
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["aligned"] == 2
    assert report["source_sets"] == {"Rani": [3]}


def test_percentile_uses_nearest_rank(tmp_path: Path) -> None:
    base = datetime(2026, 10, 5, 10, 0, 0, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for index in range(100):
        start_mono = 100_000.0 + index * 10_000.0
        start_wall = base + timedelta(seconds=10 * index)
        end_wall = start_wall + timedelta(seconds=5)
        rows.append(
            {"kind": "vad", "active": True, "mono_ms": start_mono, "wall": start_wall.isoformat()}
        )
        rows.append(
            {
                "kind": "vad",
                "active": False,
                "mono_ms": start_mono + 5000.0,
                "wall": end_wall.isoformat(),
            }
        )
        transcripts.append(
            {
                "kind": "transcript",
                "spoken_at": (end_wall + timedelta(seconds=1)).isoformat(),
                "text": f"word{index} said",
            }
        )
        turns.append({"turn": index, "speaker": "Rani", "kind": "solo", "fragment": f"word{index}"})
        samples.append(
            {"page_ms": start_mono + index, "sources": [{"source": 3, "audioLevel": 0.5}]}
        )
    rows.append(
        {
            "kind": "page",
            "mono_ms": 0.0,
            "page_ms": 0.0,
            "peer_connections": 1,
            "samples": samples,
            "roster": [],
        }
    )
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    record_path = tmp_path / "record.jsonl"
    record_path.write_text(
        "\n".join(json.dumps(transcript) for transcript in transcripts), encoding="utf-8"
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(turns), encoding="utf-8")
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["lag_ms"]["samples"] == 100
    assert report["lag_ms"]["p95"] == 94
