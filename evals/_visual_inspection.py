import hashlib
import string
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fakeyou.perception.inspector import InspectionText, VisualInspection, VisualVerdict

FIXTURE = Path(__file__).parent / "fixtures" / "visual-inspection" / "cases.json"


class EvidenceAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: tuple[int, int, int, int]
    visibility: Literal["visible", "absent", "obscured"]
    facts: tuple[InspectionText, ...]


class VisualCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: InspectionText
    image: InspectionText
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    question: InspectionText
    expected_verdict: VisualVerdict
    expected_answers: tuple[InspectionText, ...] = ()
    annotations: tuple[EvidenceAnnotation, ...] = Field(min_length=1)
    rationale: InspectionText

    @model_validator(mode="after")
    def answers_match_label(self) -> "VisualCase":
        if bool(self.expected_answers) != (self.expected_verdict == "supported"):
            raise ValueError("only supported labels require expected answers")
        return self


class VisualFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label_source: InspectionText
    cases: tuple[VisualCase, ...] = Field(min_length=1)
    image_root: Path = Field(default=FIXTURE.parent, exclude=True)


def load_visual_fixture(path: Path = FIXTURE, *, image_root: Path | None = None) -> VisualFixture:
    fixture = VisualFixture.model_validate_json(path.read_text())
    root = image_root or path.parent
    if len({case.id for case in fixture.cases}) != len(fixture.cases):
        raise ValueError("case IDs must be unique")
    for case in fixture.cases:
        image_path = (root / case.image).resolve()
        if not image_path.is_relative_to(root.resolve()):
            raise ValueError("fixture image must be inside the image root")
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != case.image_sha256:
            raise ValueError(f"image hash mismatch for {case.id}")
        with Image.open(image_path) as image:
            width, height = image.size
        for annotation in case.annotations:
            left, top, right, bottom = annotation.region
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise ValueError(f"annotation outside image for {case.id}")
    return fixture.model_copy(update={"image_root": root})


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).strip(
        string.punctuation
    )


def _score(case: VisualCase, result: VisualInspection) -> dict[str, Any]:
    accepted = {_normalized(answer) for answer in case.expected_answers}
    allowed_facts = {_normalized(fact) for region in case.annotations for fact in region.facts}
    answer_correct = result.answer is not None and _normalized(result.answer) in accepted
    answer_grounded = result.answer is not None and (
        answer_correct or _normalized(result.answer) in allowed_facts
    )
    grounded = int(answer_grounded) + sum(
        _normalized(fact) in allowed_facts for fact in result.observed_facts
    )
    assertions = int(result.answer is not None) + len(result.observed_facts)
    return {
        "case_id": case.id,
        "expected_verdict": case.expected_verdict,
        "verdict": result.verdict,
        "answer": result.answer,
        "observed_facts": list(result.observed_facts),
        "answer_correct": answer_correct,
        "abstained": result.verdict != "supported",
        "grounded_assertions": grounded,
        "assertions": assertions,
        "false_claim": grounded < assertions,
        "error": None,
    }


def _rate(count: int, total: int) -> dict[str, int | float | None]:
    return {"count": count, "of": total, "rate": count / total if total else None}


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row["error"] is None]
    answerable = [row for row in rows if row["expected_verdict"] == "supported"]
    abstentions = [row for row in valid if row["abstained"]]
    correct_abstentions = sum(row["expected_verdict"] != "supported" for row in abstentions)
    return {
        "verdict_accuracy": _rate(
            sum(row["verdict"] == row["expected_verdict"] for row in valid), len(rows)
        ),
        "answer_accuracy": _rate(sum(row["answer_correct"] for row in valid), len(answerable)),
        "grounding_precision": _rate(
            sum(row["grounded_assertions"] for row in valid),
            sum(row["assertions"] for row in valid),
        ),
        "false_claim_rate": _rate(
            sum(row["false_claim"] for row in valid), sum(row["assertions"] > 0 for row in valid)
        ),
        "abstention_precision": _rate(correct_abstentions, len(abstentions)),
        "abstention_recall": _rate(correct_abstentions, len(rows) - len(answerable)),
        "false_abstention_rate": _rate(
            sum(row["expected_verdict"] == "supported" for row in abstentions), len(answerable)
        ),
        "invalid_output_rate": _rate(
            sum(row["error"] == "invalid_output" for row in rows), len(rows)
        ),
        "model_error_rate": _rate(sum(row["error"] == "model_error" for row in rows), len(rows)),
    }


async def evaluate_inspections(
    inspect: Callable[[bytes, str], Awaitable[str]], fixture: VisualFixture
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in fixture.cases:
        data = (fixture.image_root / case.image).read_bytes()
        if hashlib.sha256(data).hexdigest() != case.image_sha256:
            raise ValueError(f"image hash mismatch for {case.id}")
        error: str | None = None
        try:
            raw = await inspect(data, case.question)
        except (RuntimeError, TimeoutError):
            error = "model_error"
        else:
            try:
                result = VisualInspection.model_validate_json(raw)
            except ValueError:
                error = "invalid_output"
        if error is None:
            rows.append(_score(case, result))
        else:
            rows.append(
                {"case_id": case.id, "expected_verdict": case.expected_verdict, "error": error}
            )
    metrics = _metrics(rows)
    return {
        "mode": "offline_closed_set",
        "dataset_sha256": hashlib.sha256(fixture.model_dump_json().encode()).hexdigest(),
        "label_source": fixture.label_source,
        "cases": rows,
        "metrics": metrics,
        "passed": all(
            row["error"] is None
            and row["verdict"] == row["expected_verdict"]
            and not row["false_claim"]
            and (row["expected_verdict"] != "supported" or row["answer_correct"])
            for row in rows
        ),
    }
