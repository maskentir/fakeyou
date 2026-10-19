from collections.abc import Callable, Sequence
from dataclasses import astuple, dataclass, fields
from xml.sax.saxutils import escape

from fakeyou.foundation.provenance import MemoryEvidence

DEFAULT_EVIDENCE_CHAR_BUDGET = 1200
EVIDENCE_SECTION = "memory_evidence"

RenderEvidence = Callable[[Sequence[MemoryEvidence], int], str]


@dataclass(frozen=True, slots=True)
class PromptSections:
    role: str
    authority: str
    persona: str
    brief: str
    memory_rules: str
    tools: str
    output_rules: str


SECTION_ORDER: tuple[str, ...] = tuple(section.name for section in fields(PromptSections))


def escape_xml_data(text: str) -> str:
    return escape(text)


def _section(name: str, body: str) -> str:
    return f"<{name}>{escape_xml_data(body)}</{name}>"


def build_system_prompt(
    sections: PromptSections,
    evidence: Sequence[MemoryEvidence],
    render_evidence: RenderEvidence,
    *,
    evidence_char_budget: int = DEFAULT_EVIDENCE_CHAR_BUDGET,
) -> str:
    rendered = [
        _section(name, body) for name, body in zip(SECTION_ORDER, astuple(sections), strict=True)
    ]
    rendered.append(_section(EVIDENCE_SECTION, render_evidence(evidence, evidence_char_budget)))
    return "\n\n".join(rendered)
