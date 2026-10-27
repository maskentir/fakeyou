from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from fakeyou.conversation.contribution import SourceRegistry
from fakeyou.session.facts import Disclosure, Namespace
from fakeyou.session.speech import BookEntry, FactBook

KEPT_TURNS = 16
KEPT_MEMORY_FACTS = 64


@dataclass(slots=True)
class TurnEvidence:
    _observed: OrderedDict[str, list[str]] = field(default_factory=OrderedDict)
    _memory: OrderedDict[str, str] = field(default_factory=OrderedDict)

    def note_observed(self, transcript_id: str, text: str) -> None:
        self._observed.setdefault(transcript_id, []).append(text)
        self._observed.move_to_end(transcript_id)
        _bound(self._observed)

    def note_memory(self, refs: Mapping[str, str]) -> None:
        for fact_id, text in refs.items():
            self._memory[fact_id] = text
            self._memory.move_to_end(fact_id)
        while len(self._memory) > KEPT_MEMORY_FACTS:
            self._memory.popitem(last=False)

    def observed(self, transcript_id: str) -> str:
        return "\n".join(self._observed.get(transcript_id, ()))

    def memory(self) -> Mapping[str, str]:
        return dict(self._memory)


def _bound[T](items: OrderedDict[str, T]) -> None:
    while len(items) > KEPT_TURNS:
        items.popitem(last=False)


@dataclass(frozen=True, slots=True)
class BookSource:
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    permissions: frozenset[str] = frozenset()
    persona_knowledge: bool = True


def _speakable(permission: str, source: BookSource) -> Disclosure:
    return "room_speakable" if permission in source.permissions else "owner_only"


def _entries(source: BookSource, memory: Mapping[str, str]) -> Iterator[BookEntry]:
    registry = source.registry
    facts = _speakable("explain_facts", source)
    questions = _speakable("ask_brief_questions", source)
    persona: Disclosure = "room_speakable" if source.persona_knowledge else "owner_only"
    namespaces: tuple[tuple[Mapping[str, str], Namespace, Disclosure, bool], ...] = (
        (memory, "memory", persona, False),
        (registry.profile, "persona", persona, False),
        (registry.questions, "brief", questions, True),
        (registry.facts, "brief", facts, False),
    )
    for texts, namespace, disclosure, question in namespaces:
        for fact_id, text in texts.items():
            yield BookEntry(fact_id, text, namespace, disclosure, question=question)


def fact_book(
    source: BookSource, *, heard: str, observed: str, memory: Mapping[str, str]
) -> FactBook:
    return FactBook(
        entries={entry.fact_id: entry for entry in _entries(source, memory)},
        heard=f"{heard}\n{observed}".strip(),
        internal=tuple(source.registry.objectives.values()),
    )
