from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from fakeyou.conversation.contribution import RecalledFact, SourceRegistry
from fakeyou.perception.view import ScreenFact
from fakeyou.session.facts import Disclosure
from fakeyou.session.speech import BookEntry, EntryKind, FactBook

KEPT_TURNS = 16
KEPT_MEMORY_FACTS = 64
KEPT_SCREENS = 16


@dataclass(slots=True)
class TurnEvidence:
    _observed: OrderedDict[str, list[str]] = field(default_factory=OrderedDict)
    _memory: OrderedDict[str, RecalledFact] = field(default_factory=OrderedDict)
    _screens: OrderedDict[str, ScreenFact] = field(default_factory=OrderedDict)

    def note_observed(self, transcript_id: str, text: str) -> None:
        self._observed.setdefault(transcript_id, []).append(text)
        self._observed.move_to_end(transcript_id)
        _bound(self._observed)

    def note_memory(self, refs: Mapping[str, RecalledFact]) -> None:
        for fact_id, fact in refs.items():
            self._memory[fact_id] = fact
            self._memory.move_to_end(fact_id)
        while len(self._memory) > KEPT_MEMORY_FACTS:
            self._memory.popitem(last=False)

    def observed(self, transcript_id: str) -> str:
        return "\n".join(self._observed.get(transcript_id, ()))

    def memory(self) -> Mapping[str, RecalledFact]:
        return dict(self._memory)

    def note_screens(self, screens: Mapping[str, ScreenFact]) -> None:
        for ref, fact in screens.items():
            self._screens[ref] = fact
            self._screens.move_to_end(ref)
        while len(self._screens) > KEPT_SCREENS:
            self._screens.popitem(last=False)

    def screens(self) -> Mapping[str, ScreenFact]:
        return dict(self._screens)


def _bound[T](items: OrderedDict[str, T]) -> None:
    while len(items) > KEPT_TURNS:
        items.popitem(last=False)


@dataclass(frozen=True, slots=True)
class BookSource:
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    permissions: frozenset[str] = frozenset()
    persona_knowledge: bool = True
    verify: bool = True


def _speakable(permission: str, source: BookSource) -> Disclosure:
    return "room_speakable" if permission in source.permissions else "owner_only"


def _entries(source: BookSource, memory: Mapping[str, RecalledFact]) -> Iterator[BookEntry]:
    registry = source.registry
    facts = _speakable("explain_facts", source)
    questions = _speakable("ask_brief_questions", source)
    objectives: Disclosure = "internal_instruction" if questions == "room_speakable" else questions
    persona: Disclosure = "room_speakable" if source.persona_knowledge else "owner_only"
    for fact_id, fact in memory.items():
        yield BookEntry(
            fact_id,
            fact.text,
            "memory",
            persona,
            owner_spoken=fact.owner_spoken,
            times=fact.times,
        )
    brief: tuple[tuple[Mapping[str, str], Disclosure, EntryKind], ...] = (
        (registry.questions, questions, "question"),
        (registry.facts, facts, "fact"),
        (registry.objectives, objectives, "objective"),
    )
    for texts, disclosure, kind in brief:
        for fact_id, text in texts.items():
            yield BookEntry(fact_id, text, "brief", disclosure, kind=kind)
    for ref, item in registry.owner_model.items():
        owned = item.disclosure if source.persona_knowledge else "owner_only"
        since = (item.since,) if item.since is not None else ()
        yield BookEntry(
            ref,
            item.text,
            "owner_model",
            owned,
            kind=item.kind,
            confidence=item.confidence,
            times=since,
        )


def fact_book(
    source: BookSource,
    *,
    heard: str,
    observed: str,
    memory: Mapping[str, RecalledFact],
    screens: Mapping[str, ScreenFact] | None = None,
) -> FactBook:
    entries = {entry.fact_id: entry for entry in _entries(source, memory)}
    for ref, fact in (screens or {}).items():
        entries[ref] = BookEntry(
            ref, fact.text, "screen", "room_speakable", attributed_to=fact.presenter
        )
    return FactBook(
        entries=entries,
        heard=f"{heard}\n{observed}".strip(),
        internal=tuple(source.registry.objectives.values()),
    )
