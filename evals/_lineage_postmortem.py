import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from evals._decision_lineage import (
    FIXTURE,
    LABELS,
    RELATIONSHIPS,
    FixtureMeeting,
    Label,
    LexicalIndex,
    LineageDataset,
    ResolvedPair,
    _tokens,
    dataset_register,
    load_dataset,
    load_split,
    partition_claims,
    partition_pairs,
    resolve_pairs,
    retrieval_text,
)
from evals._lineage_classifier import (
    MAX_CANDIDATES,
    PROMPT_VERSION,
    ClassifierOutput,
    ClassifierRequest,
    Decision,
    PreparedCall,
    ReplayProvider,
    ValidatorVersion,
    claim_view,
    estimate_input_tokens,
    load_plan,
    pilot_scope,
    replay_report,
    spend,
    validate_output,
)
from fakeyou.debrief.lineage import comparison_window
from fakeyou.debrief.register import DecisionRecord, DecisionRegister

PILOT_1 = Path(__file__).parent / "fixtures" / "lineage-pilot-1"
RUNTIME = Path(__file__).resolve().parents[1] / ".runtime" / "lineage-pilot"
REPORT_VERSION = "lineage-pilot-postmortem/v1"
VERSIONS: tuple[ValidatorVersion, ...] = ("v1", "v2")
SEMANTIC_PAIRS = ("pair-001", "pair-051", "pair-053", "pair-060")
COMPONENT_CATEGORIES = ("hard_negative", "ambiguous", "prompt_injection", "owner_approval_claim")
CORRECTED_NOTICE = (
    "Offline rescoring of the responses recorded in pilot #1 under validator v2. It is not a new "
    "measurement of model performance and does not replace the original gate result."
)
_STRICT = ConfigDict(extra="forbid", frozen=True)
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ArtifactRecord(BaseModel):
    model_config = _STRICT

    original: str
    committed: str | None
    sha256: _Sha256


class Manifest(BaseModel):
    model_config = _STRICT

    experiment: str
    status: str
    immutable: str
    execution: dict[str, str | int | float | bool]
    original_gate_failures: tuple[str, ...]
    artifacts: dict[str, ArtifactRecord]


def load_manifest(folder: Path = PILOT_1) -> Manifest:
    return Manifest.model_validate_json((folder / "manifest.json").read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_artifacts(
    manifest: Manifest, folder: Path = PILOT_1, runtime_root: Path = RUNTIME.parents[1]
) -> dict[str, dict[str, str]]:
    status: dict[str, dict[str, str]] = {}
    for name, record in manifest.artifacts.items():
        if record.committed is not None and _digest(folder / record.committed) != record.sha256:
            raise ValueError(f"committed copy of {name} differs from the recorded digest")
        original = runtime_root / record.original
        status[name] = {
            "committed": "verified" if record.committed is not None else "not committed",
            "original": "absent"
            if not original.exists()
            else "verified"
            if _digest(original) == record.sha256
            else "MISMATCH",
        }
    return status


Stage = Literal["broken_evidence", "no_strict_time_order", "retrieval_miss", "presented"]


@dataclass(frozen=True, slots=True)
class PairTrace:
    resolved: ResolvedPair
    stage: Stage
    raw_label: Label | None
    decisions: Mapping[ValidatorVersion, Decision]


@dataclass(frozen=True, slots=True)
class CandidateTrace:
    call: PreparedCall
    ref: str
    raw_label: Label | None
    decisions: Mapping[ValidatorVersion, Decision]


def _raw_labels(raw: str) -> dict[str, Label]:
    try:
        output = ClassifierOutput.model_validate_json(raw)
    except ValidationError:
        return {}
    return {item.candidate: item.label for item in output.comparisons}


def candidate_traces(
    calls: Sequence[PreparedCall], provider: ReplayProvider, model: str
) -> tuple[CandidateTrace, ...]:
    traces: list[CandidateTrace] = []
    for call in calls:
        raw = provider.respond(call.request, model).response
        labels = _raw_labels(raw)
        versions = {version: validate_output(call, raw, version) for version in VERSIONS}
        traces.extend(
            CandidateTrace(
                call,
                view.ref,
                labels.get(view.ref),
                {version: decided[view.ref] for version, decided in versions.items()},
            )
            for view in call.request.candidates
        )
    return tuple(traces)


def pair_traces(
    pairs: Sequence[ResolvedPair], candidates: Sequence[CandidateTrace]
) -> tuple[PairTrace, ...]:
    by_records = {
        (trace.call.record_for(trace.ref).record_id, trace.call.later.record_id): trace
        for trace in candidates
    }
    traces: list[PairTrace] = []
    for resolved in pairs:
        first, second = resolved.first.record, resolved.second.record
        if first is None or second is None:
            traces.append(PairTrace(resolved, "broken_evidence", None, {}))
        elif resolved.pair.temporal_order != "strictly_earlier":
            traces.append(PairTrace(resolved, "no_strict_time_order", None, {}))
        elif (found := by_records.get((first.record_id, second.record_id))) is None:
            traces.append(PairTrace(resolved, "retrieval_miss", None, {}))
        else:
            traces.append(PairTrace(resolved, "presented", found.raw_label, found.decisions))
    return tuple(traces)


def _confusion(rows: Sequence[tuple[Label, Label]]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {gold: dict.fromkeys(LABELS, 0) for gold in LABELS}
    for gold, predicted in rows:
        table[gold][predicted] += 1
    return {gold: row for gold, row in table.items() if any(row.values())}


def _per_class(rows: Sequence[tuple[Label, Label]]) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    for label in LABELS:
        support = sum(gold == label for gold, _ in rows)
        predicted = sum(guess == label for _, guess in rows)
        hits = sum(gold == guess == label for gold, guess in rows)
        report[label] = {
            "support": support,
            "predicted": predicted,
            "correct": hits,
            "precision": hits / predicted if predicted else None,
            "recall": hits / support if support else None,
        }
    return report


def _model_level(
    presented: Sequence[PairTrace], label_of: Callable[[PairTrace], Label]
) -> dict[str, object]:
    rows = [(trace.resolved.pair.label, label_of(trace)) for trace in presented]
    by_language: dict[str, Counter[str]] = {}
    for trace, (gold, guess) in zip(presented, rows, strict=True):
        tally = by_language.setdefault(trace.resolved.pair.language, Counter())
        tally["presented"] += 1
        tally["correct"] += gold == guess
    return {
        "denominator": len(presented),
        "correct": sum(gold == guess for gold, guess in rows),
        "confusion": _confusion(rows),
        "per_class": _per_class(rows),
        "by_language": {name: dict(tally) for name, tally in sorted(by_language.items())},
        "contradiction_as_supersession": sum(
            gold == "potential_contradiction" and guess == "potential_supersession"
            for gold, guess in rows
        ),
        "supersession_as_contradiction": sum(
            gold == "potential_supersession" and guess == "potential_contradiction"
            for gold, guess in rows
        ),
    }


def _validated(version: ValidatorVersion) -> Callable[[PairTrace], Label]:
    return lambda trace: trace.decisions[version].label


def _raw(trace: PairTrace) -> Label:
    return trace.raw_label or "abstain"


@dataclass(frozen=True, slots=True)
class Pilot:
    dataset: LineageDataset
    pairs: tuple[ResolvedPair, ...]
    calls: tuple[PreparedCall, ...]
    candidates: tuple[CandidateTrace, ...]
    traces: tuple[PairTrace, ...]
    provider: ReplayProvider


def load_pilot(folder: Path = PILOT_1, dataset_path: Path = FIXTURE) -> Pilot:
    dataset = load_dataset(dataset_path)
    plan = load_plan()
    pairs, calls = pilot_scope(dataset, plan)
    provider = ReplayProvider.load(folder / "cassette.jsonl")
    candidates = candidate_traces(calls, provider, plan.model)
    return Pilot(dataset, pairs, calls, candidates, pair_traces(pairs, candidates), provider)


def unlabeled_relationships(pilot: Pilot) -> tuple[CandidateTrace, ...]:
    labelled = {
        (resolved.first.record.record_id, resolved.second.record.record_id)
        for resolved in resolve_pairs(pilot.dataset)
        if resolved.first.record is not None and resolved.second.record is not None
    }
    return tuple(
        trace
        for trace in pilot.candidates
        if trace.raw_label in RELATIONSHIPS
        and (trace.call.record_for(trace.ref).record_id, trace.call.later.record_id) not in labelled
    )


def _development_register(dataset: LineageDataset) -> DecisionRegister:
    return dataset_register(dataset, partition_claims(load_split(), "development"))


def retrieval_diagnosis(pilot: Pilot) -> list[dict[str, object]]:
    register = _development_register(pilot.dataset)
    index = LexicalIndex([retrieval_text(record) for record in register.records])
    by_id = {record.record_id: record for record in register.records}
    report: list[dict[str, object]] = []
    for trace in pilot.traces:
        earlier, later = trace.resolved.first.record, trace.resolved.second.record
        if trace.stage != "retrieval_miss" or earlier is None or later is None:
            continue
        window = comparison_window(later.scope, register)
        scores = sorted(
            (
                (index.similarity(retrieval_text(later), retrieval_text(by_id[i])), i)
                for i in window
            ),
            key=lambda item: (-item[0], item[1]),
        )
        rank = next(n for n, (_, i) in enumerate(scores, 1) if i == earlier.record_id)
        shared = _tokens(retrieval_text(later)) & _tokens(retrieval_text(earlier))
        report.append(
            {
                "pair": trace.resolved.pair.id,
                "gold": trace.resolved.pair.label,
                "categories": list(trace.resolved.pair.categories),
                "window_candidates": len(window),
                "rank": rank,
                "similarity": round(scores[rank - 1][0], 4),
                "tenth_similarity": round(scores[MAX_CANDIDATES - 1][0], 4),
                "shared_tokens": sorted(shared),
                "earlier": earlier.text,
                "later": later.text,
            }
        )
    return report


def _decision_reasons(
    candidates: Sequence[CandidateTrace], version: ValidatorVersion
) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                trace.decisions[version].reason or trace.decisions[version].label
                for trace in candidates
            ).items()
        )
    )


def _end_to_end(pilot: Pilot, version: ValidatorVersion) -> dict[str, object]:
    report = replay_report(pilot.dataset, load_plan(), pilot.provider, version)
    normalized: dict[str, object] = json.loads(json.dumps(report, sort_keys=True))
    return normalized


def rescore(pilot: Pilot, folder: Path = PILOT_1) -> dict[str, object]:
    manifest = load_manifest(folder)
    original = json.loads((folder / "original-report.json").read_text(encoding="utf-8"))
    v1, v2 = _end_to_end(pilot, "v1"), _end_to_end(pilot, "v2")
    presented = [trace for trace in pilot.traces if trace.stage == "presented"]
    stages = Counter(trace.stage for trace in pilot.traces)
    return {
        "report_version": REPORT_VERSION,
        "experiment": manifest.experiment,
        "artifacts": verify_artifacts(manifest, folder),
        "pilot_pairs": len(pilot.traces),
        "stages": {
            stage: sorted(t.resolved.pair.id for t in pilot.traces if t.stage == stage)
            for stage in ("broken_evidence", "no_strict_time_order", "retrieval_miss", "presented")
        },
        "stage_counts": dict(sorted(stages.items())),
        "raw_outputs": {
            "candidates": len(pilot.candidates),
            "labels": dict(
                sorted(Counter(t.raw_label or "unparsed" for t in pilot.candidates).items())
            ),
            "presented_pairs": {t.resolved.pair.id: t.raw_label for t in presented},
        },
        "model_level": {
            "note": "Denominator is the pairs the model actually saw; untested pairs are excluded.",
            "raw": _model_level(presented, _raw),
            "validated_v1": _model_level(presented, _validated("v1")),
            "validated_v2": _model_level(presented, _validated("v2")),
        },
        "post_validation": {
            "v1": _decision_reasons(pilot.candidates, "v1"),
            "v2": _decision_reasons(pilot.candidates, "v2"),
        },
        "end_to_end": {
            "original_reproduced": {k: v for k, v in v1.items() if k in original} == original,
            "original_gate_failures": list(manifest.original_gate_failures),
            "v1": _summary(v1),
            "v2": {**_summary(v2), "notice": CORRECTED_NOTICE},
        },
        "retrieval_misses": retrieval_diagnosis(pilot),
        "unlabeled_relationships": [
            {
                "earlier": trace.call.record_for(trace.ref).text,
                "later": trace.call.later.text,
                "model_label": trace.raw_label,
                "status": "pending human annotation; not counted as correct or incorrect",
            }
            for trace in unlabeled_relationships(pilot)
        ],
    }


def _summary(report: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "confusion",
        "classes",
        "gate_failures",
        "abstention_recall",
        "answered_coverage",
        "relationship_false_positive_rate",
        "evidence_validity",
        "selection_pairs",
        "evidence_selection_sufficiency",
        "evidence_selection_precision",
        "temporal_violations",
        "approval_resolutions",
        "end_to_end",
    )
    return {key: report[key] for key in keys}


class PacketQuote(BaseModel):
    model_config = _STRICT

    entry_id: str
    quote: str
    source_line: str
    speaker: Literal["recorded_label", "unattributed"]


class PacketClaim(BaseModel):
    model_config = _STRICT

    meeting_time: str
    kind: str
    status: str
    text: str
    quotes: tuple[PacketQuote, ...]


class PacketItem(BaseModel):
    model_config = _STRICT

    item: str
    earlier: PacketClaim
    later: PacketClaim


class AdjudicationPacket(BaseModel):
    model_config = _STRICT

    version: Literal["lineage-adjudication/v1"]
    instructions: str
    labels: tuple[str, ...]
    items: tuple[PacketItem, ...]


class KeyEntry(BaseModel):
    model_config = _STRICT

    item: str
    source: Literal["benchmark_pair", "unlabeled_candidate"]
    pair_id: str | None
    earlier_record_id: str
    later_record_id: str
    gold: Label | None
    classifier_raw: Label | None
    classifier_v2: Label


class AdjudicationKey(BaseModel):
    model_config = _STRICT

    version: Literal["lineage-adjudication/v1"]
    entries: tuple[KeyEntry, ...]


class AdjudicationResponse(BaseModel):
    model_config = _STRICT

    item: str
    adjudicator: Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
    label: Label
    revision_evidence: tuple[str, ...]
    rationale: Annotated[str, StringConstraints(min_length=40, strip_whitespace=True)]


ADJUDICATION_INSTRUCTIONS = (
    "Each item shows an earlier and a later claim recorded in engineering meetings, with the exact "
    "quotes and the transcript line each quote comes from. Label the relationship of the later "
    "claim to the earlier one.\n"
    "potential_supersession needs explicit evidence of revision in the later claim: it says the "
    "earlier claim was changed, replaced, cancelled, postponed or reassigned (for example "
    "'instead', 'no longer', 'after all', or the same in Indonesian). A later meeting time or a "
    "different value is not enough on its own.\n"
    "potential_contradiction: same subject, both cannot hold, and the later claim does not present "
    "itself as a revision.\n"
    "related_discussion: same subject, compatible. no_established_relationship: different "
    "subjects. abstain: you cannot judge, for example a claim with no identifiable subject.\n"
    "For supersession, list in revision_evidence the later-side entry ids that show the revision. "
    "Write a rationale of at least 40 characters. Do not consult the benchmark or any model output."
)


def _packet_claim(record: DecisionRecord, meeting: FixtureMeeting) -> PacketClaim:
    lines = {source.id: source.text for source in meeting.sources}
    view = claim_view("x", record)
    return PacketClaim(
        meeting_time=view.meeting_time,
        kind=view.kind,
        status=view.status,
        text=view.text,
        quotes=tuple(
            PacketQuote(
                entry_id=quote.entry_id,
                quote=quote.quote,
                source_line=lines[quote.entry_id],
                speaker=quote.attribution,
            )
            for quote in view.quotes
        ),
    )


def adjudication_packet(pilot: Pilot) -> tuple[AdjudicationPacket, AdjudicationKey]:
    meetings = {meeting.meeting_id: meeting for meeting in pilot.dataset.meetings}
    chosen: list[tuple[DecisionRecord, DecisionRecord, KeyEntry]] = []
    for trace in pilot.traces:
        earlier, later = trace.resolved.first.record, trace.resolved.second.record
        if trace.resolved.pair.id in SEMANTIC_PAIRS and earlier and later:
            chosen.append(
                (
                    earlier,
                    later,
                    KeyEntry(
                        item="",
                        source="benchmark_pair",
                        pair_id=trace.resolved.pair.id,
                        earlier_record_id=earlier.record_id,
                        later_record_id=later.record_id,
                        gold=trace.resolved.pair.label,
                        classifier_raw=trace.raw_label,
                        classifier_v2=trace.decisions["v2"].label,
                    ),
                )
            )
    for candidate in unlabeled_relationships(pilot):
        earlier = candidate.call.record_for(candidate.ref)
        chosen.append(
            (
                earlier,
                candidate.call.later,
                KeyEntry(
                    item="",
                    source="unlabeled_candidate",
                    pair_id=None,
                    earlier_record_id=earlier.record_id,
                    later_record_id=candidate.call.later.record_id,
                    gold=None,
                    classifier_raw=candidate.raw_label,
                    classifier_v2=candidate.decisions["v2"].label,
                ),
            )
        )
    ordered = sorted(
        chosen,
        key=lambda item: hashlib.sha256(
            (item[0].record_id + item[1].record_id).encode()
        ).hexdigest(),
    )
    items, entries = [], []
    for number, (earlier, later, entry) in enumerate(ordered, 1):
        name = f"item-{number:02d}"
        items.append(
            PacketItem(
                item=name,
                earlier=_packet_claim(earlier, meetings[earlier.scope.meeting_id]),
                later=_packet_claim(later, meetings[later.scope.meeting_id]),
            )
        )
        entries.append(entry.model_copy(update={"item": name}))
    return (
        AdjudicationPacket(
            version="lineage-adjudication/v1",
            instructions=ADJUDICATION_INSTRUCTIONS,
            labels=LABELS,
            items=tuple(items),
        ),
        AdjudicationKey(version="lineage-adjudication/v1", entries=tuple(entries)),
    )


def packet_leaks(packet: AdjudicationPacket, key: AdjudicationKey) -> list[str]:
    text = packet.model_dump_json()
    leaks = [
        value
        for entry in key.entries
        for value in (entry.pair_id, entry.earlier_record_id, entry.later_record_id)
        if value and value in text
    ]
    leaks += [
        name for name in ("gold", "classifier_raw", "pair_id", 'rationale"') if f'"{name}' in text
    ]
    return leaks


def _check_coverage(
    entries: Mapping[str, KeyEntry], responses: Sequence[AdjudicationResponse]
) -> list[str]:
    if not responses:
        raise ValueError("no responses")
    unknown = sorted({response.item for response in responses} - set(entries))
    if unknown:
        raise ValueError(f"responses for unknown items: {unknown}")
    seen: set[tuple[str, str]] = set()
    for response in responses:
        marker = (response.item, response.adjudicator)
        if marker in seen:
            raise ValueError(
                f"item {response.item} answered more than once by {response.adjudicator}"
            )
        seen.add(marker)
    adjudicators = sorted({response.adjudicator for response in responses})
    for adjudicator in adjudicators:
        answered = {r.item for r in responses if r.adjudicator == adjudicator}
        if answered != set(entries):
            raise ValueError(
                f"adjudicator {adjudicator} did not answer every item: "
                f"missing {sorted(set(entries) - answered)}"
            )
    return adjudicators


def _revision_quotes(
    items: Mapping[str, PacketItem], responses: Sequence[AdjudicationResponse]
) -> dict[tuple[str, str], list[str]]:
    quotes: dict[tuple[str, str], list[str]] = {}
    for response in responses:
        item = items[response.item]
        later_ids = {quote.entry_id for quote in item.later.quotes}
        evidence = response.revision_evidence
        if response.label == "potential_supersession":
            if not evidence:
                raise ValueError(f"{response.item}: supersession requires revision evidence")
            if len(set(evidence)) != len(evidence) or not set(evidence) <= later_ids:
                raise ValueError(
                    f"{response.item}: revision evidence must cite the later claim: "
                    f"{sorted(set(evidence) - later_ids)}"
                )
        elif evidence:
            raise ValueError(
                f"{response.item}: revision evidence is only for potential_supersession"
            )
        by_id = {quote.entry_id: quote.quote for quote in item.later.quotes}
        quotes[(response.item, response.adjudicator)] = [by_id[entry_id] for entry_id in evidence]
    return quotes


def _reject_duplicate_items(
    packet: AdjudicationPacket, key: AdjudicationKey
) -> tuple[dict[str, PacketItem], dict[str, KeyEntry]]:
    for name, count in Counter(item.item for item in packet.items).items():
        if count > 1:
            raise ValueError(f"duplicate item {name} in the packet")
    for name, count in Counter(entry.item for entry in key.entries).items():
        if count > 1:
            raise ValueError(f"duplicate item {name} in the coordinator key")
    return (
        {item.item: item for item in packet.items},
        {entry.item: entry for entry in key.entries},
    )


def _match_canonical_content(
    items: Mapping[str, PacketItem], entries: Mapping[str, KeyEntry]
) -> None:
    canonical_packet, canonical_key = adjudication_packet(load_pilot())
    canonical_items = {item.item: item for item in canonical_packet.items}
    canonical_entries = {entry.item: entry for entry in canonical_key.entries}
    if set(items) != set(canonical_items) or set(entries) != set(canonical_entries):
        raise ValueError("packet and key must contain every canonical item")
    for name, item in items.items():
        if canonical_items[name] != item:
            raise ValueError(f"packet item {name} does not match the canonical packet")
    for name, entry in entries.items():
        if canonical_entries[name] != entry:
            raise ValueError(f"key entry {name} does not match the canonical key")


def _match_canonical_header(packet: AdjudicationPacket) -> None:
    canonical_packet, _ = adjudication_packet(load_pilot())
    if packet.instructions != canonical_packet.instructions:
        raise ValueError("packet instructions do not match the canonical instructions")
    if tuple(packet.labels) != tuple(canonical_packet.labels):
        raise ValueError("packet labels do not match the canonical labels")


def _check_pairing(
    packet: AdjudicationPacket,
    key: AdjudicationKey,
) -> tuple[dict[str, PacketItem], dict[str, KeyEntry]]:
    items, entries = _reject_duplicate_items(packet, key)
    _match_canonical_content(items, entries)
    _match_canonical_header(packet)
    return items, entries


def _normalize_responses(
    responses: Sequence[AdjudicationResponse],
) -> list[AdjudicationResponse]:
    normalized = []
    for response in responses:
        adjudicator = response.adjudicator.strip()
        rationale = response.rationale.strip()
        if not adjudicator:
            raise ValueError(f"item {response.item} has a blank adjudicator")
        if len(rationale) < 40:
            raise ValueError(f"item {response.item} rationale is shorter than 40 characters")
        if (adjudicator, rationale) == (response.adjudicator, response.rationale):
            normalized.append(response)
        else:
            normalized.append(
                response.model_copy(update={"adjudicator": adjudicator, "rationale": rationale})
            )
    return normalized


def _validate_responses(
    packet: AdjudicationPacket,
    key: AdjudicationKey,
    responses: Sequence[AdjudicationResponse],
) -> dict[str, Any]:
    items, entries = _check_pairing(packet, key)
    cleaned = _normalize_responses(responses)
    adjudicators = _check_coverage(entries, cleaned)
    quotes = _revision_quotes(items, cleaned)
    return {
        "entries": entries,
        "adjudicators": adjudicators,
        "quotes": quotes,
        "responses": cleaned,
    }


def _inter_annotator_agreement(
    responses: Sequence[AdjudicationResponse], adjudicators: Sequence[str]
) -> dict[str, object]:
    by_item: dict[str, dict[str, Label]] = {}
    for response in responses:
        by_item.setdefault(response.item, {})[response.adjudicator] = response.label
    overlapping = sorted(item for item, labels in by_item.items() if len(labels) >= 2)
    pairs = []
    for index, first in enumerate(adjudicators):
        for second in adjudicators[index + 1 :]:
            shared = [
                item for item in overlapping if first in by_item[item] and second in by_item[item]
            ]
            matches = sum(by_item[item][first] == by_item[item][second] for item in shared)
            pairs.append(
                {
                    "annotators": [first, second],
                    "items": len(shared),
                    "matches": matches,
                    "rate": matches / len(shared) if shared else None,
                }
            )
    return {
        "status": "available" if pairs else "unavailable",
        "overlapping_items": overlapping,
        "pairs": pairs,
    }


def compare(
    packet: AdjudicationPacket,
    key: AdjudicationKey,
    responses: Sequence[AdjudicationResponse],
) -> dict[str, object]:
    validated = _validate_responses(packet, key, responses)
    entries = validated["entries"]
    adjudicators = validated["adjudicators"]
    quotes = validated["quotes"]
    responses = validated["responses"]
    rows = []
    for response in sorted(responses, key=lambda item: (item.item, item.adjudicator)):
        entry = entries[response.item]
        rows.append(
            {
                "item": response.item,
                "source": entry.source,
                "pair_id": entry.pair_id,
                "adjudicator": response.adjudicator,
                "human": response.label,
                "gold": entry.gold,
                "classifier_raw": entry.classifier_raw,
                "agrees_with_gold": None if entry.gold is None else response.label == entry.gold,
                "agrees_with_model": response.label == entry.classifier_raw,
                "revision_evidence": list(response.revision_evidence),
                "revision_evidence_quotes": quotes[(response.item, response.adjudicator)],
                "rationale": response.rationale,
            }
        )
    labeled = [entry for entry in entries.values() if entry.gold is not None]
    gold_agreement = {
        "labeled_items": len(labeled),
        "by_adjudicator": {
            name: {
                "matched": sum(
                    response.label == entries[response.item].gold
                    for response in responses
                    if response.adjudicator == name and entries[response.item].gold is not None
                ),
                "total": len(labeled),
            }
            for name in adjudicators
        },
    }
    first_annotations = [
        {
            "item": row["item"],
            "earlier_record_id": entries[row["item"]].earlier_record_id,
            "later_record_id": entries[row["item"]].later_record_id,
            "adjudicator": row["adjudicator"],
            "human": row["human"],
            "classifier_raw": row["classifier_raw"],
            "revision_evidence": row["revision_evidence"],
            "revision_evidence_quotes": row["revision_evidence_quotes"],
            "rationale": row["rationale"],
        }
        for row in rows
        if row["source"] == "unlabeled_candidate"
    ]
    agreement = _inter_annotator_agreement(responses, adjudicators)
    return {
        "adjudicators": adjudicators,
        "annotator_coverage": {
            name: {
                "answered": len(
                    {response.item for response in responses if response.adjudicator == name}
                ),
                "complete": True,
            }
            for name in adjudicators
        },
        "items_answered": len({response.item for response in responses}),
        "items_total": len(entries),
        "gold_agreement": gold_agreement,
        "disagreements_with_gold": [row for row in rows if row["agrees_with_gold"] is False],
        "first_annotations": first_annotations,
        "inter_annotator_agreement": agreement,
        "rows": rows,
        "gold_changes_applied": False,
        "second_independent_annotator": ("present" if agreement["pairs"] else "pending"),
    }


def component_pairs(dataset: LineageDataset, pilot: Pilot | None) -> tuple[ResolvedPair, ...]:
    development = partition_pairs(load_split(), resolve_pairs(dataset))["development"]
    missed = {
        trace.resolved.pair.id
        for trace in (pilot.traces if pilot else ())
        if trace.stage == "retrieval_miss"
    }
    return tuple(
        resolved
        for resolved in development
        if resolved.first.record is not None
        and resolved.second.record is not None
        and resolved.pair.temporal_order == "strictly_earlier"
        and (
            set(resolved.pair.categories) & set(COMPONENT_CATEGORIES) or resolved.pair.id in missed
        )
    )


def component_calls(
    dataset: LineageDataset, pairs: Sequence[ResolvedPair]
) -> tuple[PreparedCall, ...]:
    register = _development_register(dataset)
    index = LexicalIndex([retrieval_text(record) for record in register.records])
    by_id = {record.record_id: record for record in register.records}
    forced: dict[str, set[str]] = {}
    for resolved in pairs:
        if resolved.first.record is not None and resolved.second.record is not None:
            forced.setdefault(resolved.second.record.record_id, set()).add(
                resolved.first.record.record_id
            )
    calls = []
    for later_id in sorted(forced):
        later = by_id[later_id]
        window = comparison_window(later.scope, register)
        ranked = sorted(
            window - forced[later_id],
            key=lambda i: (-index.similarity(retrieval_text(later), retrieval_text(by_id[i])), i),
        )
        chosen = sorted(forced[later_id] | set(ranked[: MAX_CANDIDATES - len(forced[later_id])]))
        candidates = tuple(by_id[i] for i in chosen)
        request = ClassifierRequest(
            prompt_version=PROMPT_VERSION,
            later=claim_view("later", later),
            candidates=tuple(
                claim_view(f"c{position}", record) for position, record in enumerate(candidates, 1)
            ),
        )
        calls.append(PreparedCall(later, candidates, request))
    return tuple(calls)


def component_suite(pilot: Pilot) -> dict[str, object]:
    pairs = component_pairs(pilot.dataset, pilot)
    calls = component_calls(pilot.dataset, pairs)
    plan = load_plan()
    tokens = sum(estimate_input_tokens(call.request) for call in calls)
    output = len(calls) * plan.caps.max_output_tokens_per_call
    categories: Counter[str] = Counter(
        category
        for resolved in pairs
        for category in resolved.pair.categories
        if category in COMPONENT_CATEGORIES
    )
    return {
        "suite": "classifier_component",
        "scope": (
            "Forces each listed pair's earlier claim into the candidate list. It tests the "
            "classifier alone and is never mixed with natural retrieval or end-to-end results."
        ),
        "partition": "development",
        "pairs": sorted(resolved.pair.id for resolved in pairs),
        "labels": dict(sorted(Counter(resolved.pair.label for resolved in pairs).items())),
        "categories": {name: categories[name] for name in COMPONENT_CATEGORIES},
        "gaps": [
            "owner_approval_claim pairs exist only in frozen_evaluation; the suite has none "
            "until reviewed development cases are curated"
        ]
        if not categories["owner_approval_claim"]
        else [],
        "calls": len(calls),
        "offline_estimate": {
            "input_tokens": tokens,
            "worst_case_output_tokens": output,
            "worst_case_spend_usd": round(spend(plan.pricing, tokens, output), 4),
        },
        "provider_calls_authorized": False,
    }


def _write(report: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _report_command(
    build: Callable[[], Mapping[str, object]],
) -> Callable[[argparse.Namespace], int]:
    def run(_: argparse.Namespace) -> int:
        _write(build())
        return 0

    return run


def key_inside_package(package: Path, key: Path) -> bool:
    return key.resolve().is_relative_to(package.resolve())


def _packet_command(arguments: argparse.Namespace) -> int:
    if key_inside_package(arguments.out, arguments.key_out):
        _write({"refused": "the coordinator key folder is the package folder or inside it"})
        return 2
    packet, key = adjudication_packet(load_pilot())
    leaks = packet_leaks(packet, key)
    if leaks:
        _write({"refused": "the packet would reveal the key", "leaks": leaks})
        return 2
    arguments.out.mkdir(parents=True, exist_ok=True)
    arguments.key_out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "packet.json").write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    (arguments.key_out / "key.json").write_text(key.model_dump_json(indent=2), encoding="utf-8")
    _write(
        {"items": len(packet.items), "packet": str(arguments.out), "key": str(arguments.key_out)}
    )
    return 0


def _compare_command(arguments: argparse.Namespace) -> int:
    try:
        packet = AdjudicationPacket.model_validate_json(
            arguments.packet.read_text(encoding="utf-8")
        )
        key = AdjudicationKey.model_validate_json(arguments.key.read_text(encoding="utf-8"))
        raw = json.loads(arguments.responses.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise TypeError("responses must be a JSON array")
        responses = [AdjudicationResponse.model_validate(item) for item in raw]
        report = compare(packet, key, responses)
    except (OSError, ValueError, ValidationError, TypeError) as error:
        _write({"refused": str(error)})
        return 2
    _write(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals._lineage_postmortem",
        description="Offline postmortem of DM-2b pilot #1; never calls a provider",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    reports: dict[str, tuple[str, Callable[[], Mapping[str, object]]]] = {
        "verify": (
            "Check the recorded artifact digests",
            lambda: verify_artifacts(load_manifest()),
        ),
        "rescore": ("Layered diagnostic rescoring", lambda: rescore(load_pilot())),
        "component": ("Offline classifier-component suite", lambda: component_suite(load_pilot())),
    }
    for name, (summary, build) in reports.items():
        commands.add_parser(name, help=summary).set_defaults(run=_report_command(build))
    packet = commands.add_parser("packet", help="Blind human adjudication packet")
    packet.add_argument("--out", type=Path, required=True)
    packet.add_argument("--key-out", type=Path, required=True)
    packet.set_defaults(run=_packet_command)
    comparing = commands.add_parser("compare", help="Record disagreements; never edits gold")
    comparing.add_argument("--packet", type=Path, required=True)
    comparing.add_argument("--key", type=Path, required=True)
    comparing.add_argument("--responses", type=Path, required=True)
    comparing.set_defaults(run=_compare_command)
    arguments = parser.parse_args(argv)
    command: Callable[[argparse.Namespace], int] = arguments.run
    return command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
