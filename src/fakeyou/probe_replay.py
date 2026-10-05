import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from math import ceil, isfinite
from pathlib import Path
from typing import Any

from fakeyou.voice_activity import VoiceActivityTracker, VoiceAttribution

SOURCE_ACTIVE_THRESHOLD = 0.01
DOMINANT_MARGIN = 0.5
CSRC_LEAD_WINDOW_MS = 1000.0
NAME_PRECISION_GATE = 0.98
OVERLAP_UNKNOWN_GATE = 0.95
LABEL_STABILITY_GATE = 0.95


def align_turns(
    transcripts: Sequence[Mapping[str, Any]], turns: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    aligned: list[dict[str, Any]] = []
    cursor = 0
    for turn in turns:
        fragment = str(turn.get("fragment", "")).casefold().strip()
        if not fragment:
            continue
        scan = cursor
        while scan < len(transcripts):
            transcript = transcripts[scan]
            scan += 1
            if fragment in str(transcript.get("text", "")).casefold():
                aligned.append(
                    {
                        "turn": turn.get("turn"),
                        "speaker": turn.get("speaker"),
                        "kind": turn.get("kind", "solo"),
                        "wall": transcript.get("wall"),
                    }
                )
                cursor = scan
                break
    return aligned


def page_to_mono(page_ms: float, anchors: Sequence[tuple[float, float]]) -> float:
    ordered = sorted(anchors)
    if not ordered:
        raise ValueError("page clock mapping needs at least one anchor")
    chosen = ordered[0]
    for anchor in ordered:
        if anchor[0] <= page_ms:
            chosen = anchor
        else:
            break
    return chosen[1] + (page_ms - chosen[0])


def _active_sources(
    sample: Mapping[str, Any], threshold: float = SOURCE_ACTIVE_THRESHOLD
) -> set[int]:
    sources = sample.get("sources")
    if not isinstance(sources, list):
        return set()
    active: set[int] = set()
    for entry in sources:
        if not isinstance(entry, Mapping):
            continue
        try:
            level = float(entry.get("audioLevel", 0.0))
            source = int(entry.get("source", 0))
        except (TypeError, ValueError):
            continue
        if level >= threshold:
            active.add(source)
    return active


def csrc_lag_ms(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    *,
    vad_start_mono_ms: float,
    threshold: float = SOURCE_ACTIVE_THRESHOLD,
) -> float | None:
    for sample in samples:
        try:
            page_ms = float(sample["page_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if _active_sources(sample, threshold):
            return page_to_mono(page_ms, anchors) - vad_start_mono_ms
    return None


def active_sources_in_window(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    start_mono_ms: float,
    end_mono_ms: float,
    threshold: float = SOURCE_ACTIVE_THRESHOLD,
) -> set[int]:
    active: set[int] = set()
    for sample in samples:
        try:
            page_ms = float(sample["page_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        at_mono = page_to_mono(page_ms, anchors)
        if start_mono_ms <= at_mono <= end_mono_ms:
            active |= _active_sources(sample, threshold)
    return active


def dominant_sources_in_window(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    start_mono_ms: float,
    end_mono_ms: float,
    threshold: float = SOURCE_ACTIVE_THRESHOLD,
    margin: float = DOMINANT_MARGIN,
) -> set[int]:
    peaks: dict[int, float] = {}
    for sample in samples:
        try:
            page_ms = float(sample["page_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        at_mono = page_to_mono(page_ms, anchors)
        if not start_mono_ms <= at_mono <= end_mono_ms:
            continue
        sources = sample.get("sources")
        if not isinstance(sources, list):
            continue
        for entry in sources:
            if not isinstance(entry, Mapping):
                continue
            try:
                level = float(entry.get("audioLevel", 0.0))
                source = int(entry.get("source", 0))
            except (TypeError, ValueError):
                continue
            if level >= threshold:
                peaks[source] = max(peaks.get(source, 0.0), level)
    if not peaks:
        return set()
    ceiling = max(peaks.values()) * margin
    return {source for source, peak in peaks.items() if peak >= ceiling}


def source_sets_by_speaker(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    windows: Sequence[Mapping[str, Any]],
    threshold: float = SOURCE_ACTIVE_THRESHOLD,
    margin: float | None = None,
) -> dict[str, tuple[int, ...]]:
    sets: dict[str, set[int]] = {}
    for window in windows:
        speaker = str(window.get("speaker", ""))
        if not speaker:
            continue
        start = float(window["start_mono_ms"])
        end = float(window["end_mono_ms"])
        if margin is None:
            active = active_sources_in_window(samples, anchors, start, end, threshold)
        else:
            active = dominant_sources_in_window(samples, anchors, start, end, threshold, margin)
        sets.setdefault(speaker, set()).update(active)
    return {speaker: tuple(sorted(sources)) for speaker, sources in sets.items()}


def reference_precision(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    windows: Sequence[Mapping[str, Any]],
    threshold: float = SOURCE_ACTIVE_THRESHOLD,
    margin: float | None = None,
) -> dict[str, float | None]:
    solo = [window for window in windows if window.get("kind", "solo") == "solo"]
    overlap = [window for window in windows if window.get("kind") == "overlap"]

    def window_sources(window: Mapping[str, Any]) -> set[int]:
        start = float(window["start_mono_ms"])
        end = float(window["end_mono_ms"])
        if margin is None:
            return active_sources_in_window(samples, anchors, start, end, threshold)
        return dominant_sources_in_window(samples, anchors, start, end, threshold, margin)

    votes: dict[int, dict[str, int]] = {}
    for window in solo:
        active = window_sources(window)
        if len(active) != 1:
            continue
        source = next(iter(active))
        tally = votes.setdefault(source, {})
        speaker = str(window.get("speaker", ""))
        tally[speaker] = tally.get(speaker, 0) + 1
    mapping = {
        source: max(tally.items(), key=lambda item: (item[1], item[0]))[0]
        for source, tally in votes.items()
    }
    correct = 0
    for window in solo:
        active = window_sources(window)
        predicted = mapping.get(next(iter(active))) if len(active) == 1 else None
        if predicted is not None and predicted == str(window.get("speaker", "")):
            correct += 1
    unknown = 0
    for window in overlap:
        active = window_sources(window)
        if len(active) != 1:
            unknown += 1
    return {
        "precision": (correct / len(solo)) if solo else None,
        "overlap_unknown_rate": (unknown / len(overlap)) if overlap else None,
    }


def _parse_wall(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _percentile(values: Sequence[float], rank: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, ceil(rank * len(ordered)) - 1))
    return ordered[index]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _vad_windows(edges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    stamped: list[tuple[Mapping[str, Any], datetime, float]] = []
    for edge in edges:
        wall = _parse_wall(edge.get("wall"))
        try:
            mono_ms = float(edge["mono_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if wall is not None:
            stamped.append((edge, wall, mono_ms))
    ordered = sorted(stamped, key=lambda item: item[2])
    windows: list[dict[str, Any]] = []
    start: tuple[Mapping[str, Any], datetime, float] | None = None
    for edge, wall, mono_ms in ordered:
        if edge.get("active"):
            start = (edge, wall, mono_ms)
        elif start is not None:
            if mono_ms > start[2]:
                windows.append(
                    {
                        "start_wall": start[1],
                        "end_wall": wall,
                        "start_mono_ms": start[2],
                        "end_mono_ms": mono_ms,
                    }
                )
            start = None
    return windows


def _window_for_transcript(
    windows: Sequence[Mapping[str, Any]], wall: datetime | None
) -> Mapping[str, Any] | None:
    if wall is None:
        return None
    chosen: Mapping[str, Any] | None = None
    for window in windows:
        end = window.get("end_wall")
        if isinstance(end, datetime) and end <= wall:
            chosen = window
    return chosen


def _snapshots_in_window(
    snapshots: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    start_mono_ms: float,
    end_mono_ms: float,
) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    for snapshot in snapshots:
        try:
            page_ms = float(snapshot["page_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        at_mono = page_to_mono(page_ms, anchors)
        if start_mono_ms <= at_mono <= end_mono_ms:
            selected.append(snapshot)
    return selected


def _lit_names(snapshot: Mapping[str, Any], candidate: str) -> set[str]:
    tiles = snapshot.get("tiles")
    if not isinstance(tiles, list):
        return set()
    lit: set[str] = set()
    for tile in tiles:
        if not isinstance(tile, Mapping) or tile.get("self"):
            continue
        name = tile.get("name")
        speaking = tile.get("speaking")
        if (
            isinstance(name, str)
            and name
            and isinstance(speaking, Mapping)
            and speaking.get(candidate)
        ):
            lit.add(name)
    return lit


def _unmuted_names(snapshot: Mapping[str, Any]) -> set[str]:
    tiles = snapshot.get("tiles")
    if not isinstance(tiles, list):
        return set()
    names: set[str] = set()
    for tile in tiles:
        if not isinstance(tile, Mapping) or tile.get("self"):
            continue
        name = tile.get("name")
        if isinstance(name, str) and name and not tile.get("muted"):
            names.add(name)
    return names


def merge_levels(entries: Sequence[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            page_ms = float(entry["page_ms"])
            source = int(entry["source"])
            level = float(entry["audioLevel"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if source < 0 or not isfinite(page_ms) or not isfinite(level):
            continue
        merged.append(
            {
                "page_ms": page_ms,
                "sources": [{"source": source, "audioLevel": level}],
            }
        )
    return merged


def summarize_probe_session(
    probe_path: Path, record_path: Path, truth_path: Path
) -> dict[str, Any]:
    vad_edges: list[Mapping[str, Any]] = []
    samples: list[Mapping[str, Any]] = []
    snapshots: list[Mapping[str, Any]] = []
    anchors: list[tuple[float, float]] = []
    skipped_probe = 0
    for line in probe_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped_probe += 1
            continue
        if not isinstance(row, Mapping):
            skipped_probe += 1
            continue
        if row.get("kind") == "vad":
            vad_edges.append(row)
        elif row.get("kind") == "page":
            try:
                anchors.append((float(row["page_ms"]), float(row["mono_ms"])))
            except (KeyError, TypeError, ValueError):
                continue
            batch_samples = row.get("samples")
            if isinstance(batch_samples, list):
                samples.extend(entry for entry in batch_samples if isinstance(entry, Mapping))
            batch_levels = row.get("levels")
            if isinstance(batch_levels, list):
                samples.extend(merge_levels(batch_levels))
            batch_roster = row.get("roster")
            if isinstance(batch_roster, list):
                snapshots.extend(entry for entry in batch_roster if isinstance(entry, Mapping))
    transcripts: list[dict[str, Any]] = []
    skipped_record = 0
    for line in record_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped_record += 1
            continue
        if not isinstance(row, Mapping):
            skipped_record += 1
            continue
        if row.get("kind") == "transcript":
            transcripts.append({"text": row.get("text", ""), "wall": row.get("spoken_at")})
    turns = json.loads(truth_path.read_text(encoding="utf-8"))
    aligned = align_turns(transcripts, turns)
    windows = _vad_windows(vad_edges)
    replay_windows: list[dict[str, Any]] = []
    lags: list[float] = []
    for entry in aligned:
        window = _window_for_transcript(windows, _parse_wall(entry.get("wall")))
        if window is None:
            continue
        replay_windows.append(
            {
                "speaker": entry.get("speaker"),
                "kind": entry.get("kind", "solo"),
                "start_mono_ms": window["start_mono_ms"],
                "end_mono_ms": window["end_mono_ms"],
            }
        )
    for window in replay_windows:
        if window["kind"] != "solo":
            continue
        scoped = [
            sample
            for sample in samples
            if _in_scope(sample, anchors, window["start_mono_ms"], window["end_mono_ms"])
        ]
        lag = csrc_lag_ms(scoped, anchors, vad_start_mono_ms=window["start_mono_ms"])
        if lag is not None:
            lags.append(lag)
    reference = reference_precision(samples, anchors, replay_windows, margin=DOMINANT_MARGIN)
    source_sets = source_sets_by_speaker(samples, anchors, replay_windows, margin=DOMINANT_MARGIN)
    markers: dict[str, dict[str, Any]] = {}
    for candidate in ("es310d", "knownClass"):
        correct = 0
        denominator = 0
        for window in replay_windows:
            if window["kind"] != "solo":
                continue
            scoped = _snapshots_in_window(
                snapshots, anchors, window["start_mono_ms"], window["end_mono_ms"]
            )
            if not scoped:
                continue
            denominator += 1
            lit: set[str] = set()
            for snapshot in scoped:
                lit |= _lit_names(snapshot, candidate)
            if lit == {str(window["speaker"])}:
                correct += 1
        markers[candidate] = {
            "accuracy": (correct / denominator) if denominator else None,
            "denominator": denominator,
        }
    keeps = 0
    unique = 0
    mute_denominator = 0
    for window in replay_windows:
        if window["kind"] != "solo":
            continue
        scoped = _snapshots_in_window(
            snapshots, anchors, window["start_mono_ms"], window["end_mono_ms"]
        )
        if not scoped:
            continue
        mute_denominator += 1
        speaker = str(window["speaker"])
        if any(speaker in _unmuted_names(snapshot) for snapshot in scoped):
            keeps += 1
        if all(_unmuted_names(snapshot) == {speaker} for snapshot in scoped):
            unique += 1
    stability = _label_stability(samples, anchors, replay_windows)
    precision = reference["precision"]
    overlap_rate = reference["overlap_unknown_rate"]
    return {
        "turns": len(turns),
        "aligned": len(aligned),
        "skipped_lines": {"probe": skipped_probe, "record": skipped_record},
        "lag_ms": {
            "median": _median(lags),
            "p95": _percentile(lags, 0.95),
            "samples": len(lags),
        },
        "source_sets": {speaker: list(sources) for speaker, sources in source_sets.items()},
        "label_stability": stability,
        "reference": reference,
        "markers": markers,
        "mute": {
            "keeps_speaker": (keeps / mute_denominator) if mute_denominator else None,
            "unique": (unique / mute_denominator) if mute_denominator else None,
            "denominator": mute_denominator,
        },
        "gates": {
            "name_precision": (precision is not None and precision >= NAME_PRECISION_GATE),
            "overlap_unknown": (
                None if overlap_rate is None else overlap_rate >= OVERLAP_UNKNOWN_GATE
            ),
            "label_stability": (None if stability is None else stability >= LABEL_STABILITY_GATE),
        },
    }


def _in_scope(
    sample: Mapping[str, Any],
    anchors: Sequence[tuple[float, float]],
    start_mono_ms: float,
    end_mono_ms: float,
) -> bool:
    try:
        page_ms = float(sample["page_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    at_mono = page_to_mono(page_ms, anchors)
    return start_mono_ms - CSRC_LEAD_WINDOW_MS <= at_mono <= end_mono_ms


def _label_stability(
    samples: Sequence[Mapping[str, Any]],
    anchors: Sequence[tuple[float, float]],
    windows: Sequence[Mapping[str, Any]],
    margin: float = DOMINANT_MARGIN,
) -> float | None:
    solo = [window for window in windows if window.get("kind", "solo") == "solo"]
    dominant: dict[str, int] = {}
    tallies: dict[str, dict[int, int]] = {}
    for window in solo:
        active = dominant_sources_in_window(
            samples,
            anchors,
            float(window["start_mono_ms"]),
            float(window["end_mono_ms"]),
            SOURCE_ACTIVE_THRESHOLD,
            margin,
        )
        if len(active) != 1:
            continue
        source = next(iter(active))
        speaker = str(window.get("speaker", ""))
        tally = tallies.setdefault(speaker, {})
        tally[source] = tally.get(source, 0) + 1
    for speaker, tally in tallies.items():
        dominant[speaker] = max(tally.items(), key=lambda item: (item[1], item[0]))[0]
    stable = 0
    denominator = 0
    for window in solo:
        active = dominant_sources_in_window(
            samples,
            anchors,
            float(window["start_mono_ms"]),
            float(window["end_mono_ms"]),
            SOURCE_ACTIVE_THRESHOLD,
            margin,
        )
        if not active:
            continue
        denominator += 1
        if active == {dominant.get(str(window.get("speaker", "")))}:
            stable += 1
    return (stable / denominator) if denominator else None


def _load_rows(path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, Mapping):
            rows.append(row)
    return rows


def voice_fixture_from_probe(
    probe_path: Path,
    record_path: Path,
    truth_path: Path,
    *,
    twin_name: str,
    aliases: Mapping[str, str],
    persons: Mapping[str, str],
) -> dict[str, Any]:
    probe_rows = _load_rows(probe_path)
    pages = [row for row in probe_rows if row.get("kind") == "page"]
    wall_offset = _median(
        [
            float(row["mono_ms"]) - wall.timestamp() * 1000.0
            for row in pages
            if (wall := _parse_wall(row.get("wall"))) is not None
        ]
    )
    devices: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for row in probe_rows:
        if row.get("kind") == "vad":
            events.append(
                {"kind": "vad", "mono_ms": float(row["mono_ms"]), "active": bool(row["active"])}
            )
            continue
        if row.get("kind") != "page":
            continue
        batch: dict[str, Any] = {
            "instance": "probe",
            "page_ms": float(row["page_ms"]),
            "levels": [
                [entry["page_ms"], source["source"], round(source["audioLevel"], 5)]
                for entry in merge_levels(row.get("levels") or [])
                for source in entry["sources"]
                if source["audioLevel"] >= 0.001
            ],
        }
        roster = row.get("roster")
        if isinstance(roster, list) and roster and isinstance(roster[-1], Mapping):
            tiles = roster[-1].get("tiles")
            batch["participants"] = [
                {
                    "id": devices.setdefault(str(tile["id"]), f"devices/{len(devices) + 1}"),
                    "name": aliases.get(str(tile.get("name", "")), "Participant"),
                }
                for tile in tiles or []
                if isinstance(tile, Mapping) and tile.get("id")
            ]
        events.append({"kind": "page", "mono_ms": float(row["mono_ms"]), "batch": batch})
    if wall_offset is not None:
        for row in _load_rows(record_path):
            at = _parse_wall(row.get("at"))
            if row.get("kind") == "transcript" and at is not None:
                events.append(
                    {
                        "kind": "transcript",
                        "mono_ms": at.timestamp() * 1000.0 + wall_offset,
                        "text": str(row.get("text", "")),
                    }
                )
    events.sort(key=lambda event: event["mono_ms"])
    truth = [
        {
            **turn,
            "speaker": aliases.get(str(turn.get("speaker", "")), "Participant"),
            "person": persons.get(aliases.get(str(turn.get("speaker", "")), ""), "unknown"),
        }
        for turn in json.loads(truth_path.read_text(encoding="utf-8"))
    ]
    return {"twin_name": twin_name, "events": events, "truth": truth}


def replay_voice_labels(fixture: Mapping[str, Any]) -> dict[str, Any]:
    clock_ms = [0.0]
    tracker = VoiceActivityTracker(
        twin_name=str(fixture.get("twin_name", "")), monotonic=lambda: clock_ms[0] / 1000.0
    )
    attributions: list[VoiceAttribution] = []
    transcripts: list[dict[str, Any]] = []
    for event in fixture["events"]:
        clock_ms[0] = float(event["mono_ms"])
        if event["kind"] == "page":
            tracker.observe_batch(event["batch"], sent_ms=clock_ms[0], received_ms=clock_ms[0])
        elif event["kind"] == "vad":
            tracker.observe_vad(bool(event["active"]))
        elif event["kind"] == "transcript":
            transcripts.append({"text": event["text"], "wall": len(attributions)})
            attributions.append(tracker.attribute())
    truth = list(fixture["truth"])
    persons = {turn.get("turn"): turn.get("person") for turn in truth}
    aligned = align_turns(transcripts, truth)
    solo = [(entry, attributions[entry["wall"]]) for entry in aligned if entry["kind"] == "solo"]
    overlap = [attributions[entry["wall"]] for entry in aligned if entry["kind"] == "overlap"]
    labeled = [(entry, voice) for entry, voice in solo if voice.participant_id != "unknown"]
    by_join: dict[str, Counter[str]] = {}
    by_label: dict[str, Counter[str]] = {}
    by_person: dict[str, set[str]] = {}
    for entry, voice in labeled:
        join = str(entry["speaker"])
        by_join.setdefault(join, Counter())[voice.participant_id] += 1
        by_label.setdefault(voice.participant_id, Counter())[join] += 1
        by_person.setdefault(str(persons.get(entry["turn"])), set()).add(voice.participant_id)
    stable = sum(counts.most_common(1)[0][1] for counts in by_join.values())
    pure = sum(counts.most_common(1)[0][1] for counts in by_label.values())
    stability = stable / len(labeled) if labeled else None
    overlap_rate = (
        sum(voice.participant_id == "unknown" for voice in overlap) / len(overlap)
        if overlap
        else None
    )
    return {
        "transcripts": len(transcripts),
        "aligned": len(aligned),
        "solo_turns": len(solo),
        "labeled": len(labeled),
        "coverage": len(labeled) / len(solo) if solo else None,
        "label_stability": stability,
        "purity": pure / len(labeled) if labeled else None,
        "overlap_unknown_rate": overlap_rate,
        "join_labels": {join: counts.most_common(1)[0][0] for join, counts in by_join.items()},
        "person_labels": {person: sorted(labels) for person, labels in by_person.items()},
        "reasons": dict(Counter(voice.reason for _, voice in solo)),
        "misses": [
            {"turn": entry["turn"], "speaker": entry["speaker"], "reason": voice.reason}
            for entry, voice in solo
            if voice.participant_id == "unknown"
            or voice.participant_id != by_join[str(entry["speaker"])].most_common(1)[0][0]
        ],
        "gates": {
            "label_stability": None if stability is None else stability >= LABEL_STABILITY_GATE,
            "overlap_unknown": (
                None if overlap_rate is None else overlap_rate >= OVERLAP_UNKNOWN_GATE
            ),
        },
    }
