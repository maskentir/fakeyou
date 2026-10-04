import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

EVIDENCE_SCHEMA = "memory_evidence/v1"
OWNER_SPEAKER_IDENTITY = "owner"
UNVERIFIED_SPEAKER_IDENTITY = "unverified: may or may not be the owner or any named person"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    episode_id: str
    kind: Literal["owner_note", "meeting_transcript", "other"]
    label: str
    source_at: datetime
    recorded_at: datetime
    time_granularity: Literal["document", "episode_window"]
    speaker_verified: bool = False
    speaker_identity: str = UNVERIFIED_SPEAKER_IDENTITY

    def __post_init__(self) -> None:
        if not self.episode_id.strip():
            raise ValueError("episode_id must not be blank")
        kind_value: str = self.kind
        if kind_value not in ("owner_note", "meeting_transcript", "other"):
            raise ValueError("kind must be owner_note, meeting_transcript, or other")
        if self.source_at.tzinfo is None or self.source_at.utcoffset() is None:
            raise ValueError("source_at must carry timezone")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must carry timezone")
        granularity_value: str = self.time_granularity
        if granularity_value not in ("document", "episode_window"):
            raise ValueError("time_granularity must be document or episode_window")
        if not self.speaker_identity.strip():
            raise ValueError("speaker_identity must not be blank")


@dataclass(frozen=True, slots=True)
class FactEvidence:
    fact_ids: tuple[str, ...]
    text: str
    sources: tuple[SourceEvidence, ...]

    def __post_init__(self) -> None:
        if not self.fact_ids:
            raise ValueError("fact_ids must not be empty")
        if any(not item.strip() for item in self.fact_ids):
            raise ValueError("fact_ids must hold valid ids")
        if not self.text.strip():
            raise ValueError("text must not be blank")


def render_evidence(
    facts: list[FactEvidence], *, result_limit: int, excluded: dict[str, int]
) -> str:
    if result_limit < 1:
        raise ValueError("result_limit must be at least 1")
    allowed = ("pending", "superseded", "missing_source")
    for key in excluded:
        if key not in allowed:
            raise ValueError("excluded holds unknown key")
    for value in excluded.values():
        if value < 0:
            raise ValueError("excluded counts must be zero or more")
    seen: dict[str, SourceEvidence] = {}
    for fact in facts:
        for source in fact.sources:
            prior = seen.get(source.episode_id)
            if prior is not None and prior != source:
                raise ValueError(f"conflicting metadata for episode {source.episode_id}")
            seen[source.episode_id] = source
    merged_ids: dict[str, set[str]] = {}
    merged_sources: dict[str, dict[str, SourceEvidence]] = {}
    for fact in facts:
        key = " ".join(fact.text.split())
        ids = merged_ids.setdefault(key, set())
        ids.update(fact.fact_ids)
        source_map = merged_sources.setdefault(key, {})
        for source in fact.sources:
            source_map[source.episode_id] = source
    ordered: list[tuple[int, float, str, str, tuple[str, ...], tuple[SourceEvidence, ...]]] = []
    for key, ids in merged_ids.items():
        ranked = tuple(
            sorted(
                merged_sources[key].values(),
                key=lambda source: (
                    source.kind != "owner_note",
                    -source.source_at.timestamp(),
                    source.episode_id,
                ),
            )
        )
        fact_ids = tuple(sorted(ids))
        if ranked:
            owner_rank = 0 if any(item.kind == "owner_note" for item in ranked) else 1
            newest = max(item.source_at for item in ranked).timestamp()
            time_rank = -newest
        else:
            owner_rank = 1
            time_rank = float("inf")
        ordered.append((owner_rank, time_rank, fact_ids[0], key, fact_ids, ranked))
    ordered.sort(key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
    trimmed = ordered[:result_limit]
    rendered_facts: list[dict[str, object]] = []
    for entry in trimmed:
        text = entry[3]
        fact_ids = entry[4]
        ranked = entry[5]
        rendered_sources: list[dict[str, str | bool]] = []
        for source in ranked:
            rendered_sources.append(
                {
                    "episode_id": source.episode_id,
                    "kind": source.kind,
                    "label": source.label,
                    "source_at": source.source_at.astimezone(UTC).isoformat(),
                    "recorded_at": source.recorded_at.astimezone(UTC).isoformat(),
                    "time_granularity": source.time_granularity,
                    "speaker_verified": source.speaker_verified,
                    "speaker_identity": source.speaker_identity,
                }
            )
        rendered_facts.append(
            {"fact_ids": list(fact_ids), "text": text, "sources": rendered_sources}
        )
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "facts": rendered_facts,
        "excluded": {
            "pending": excluded.get("pending", 0),
            "superseded": excluded.get("superseded", 0),
            "missing_source": excluded.get("missing_source", 0),
        },
    }
    return json.dumps(payload, ensure_ascii=False)
