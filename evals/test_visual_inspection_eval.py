import hashlib
import json
from pathlib import Path

import pytest

from evals._visual_cassette import (
    ExperimentBudget,
    cassette_inspector,
    record_visual_cassette,
)
from evals._visual_images import (
    VISUAL_IMAGE_NAMES,
    generate_visual_images,
    image_pixel_digest,
)
from evals._visual_inspection import FIXTURE, evaluate_inspections, load_visual_fixture
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.perception.inspector import VISUAL_INSPECTION_SCHEMA_VERSION
from tests._perception_fakes import vision_model


def test_visual_labels_have_all_verdicts_and_independent_image_annotations() -> None:
    fixture = load_visual_fixture()
    assert len(fixture.cases) == 8
    assert {case.expected_verdict for case in fixture.cases} == {
        "supported",
        "not_visible",
        "unclear",
    }
    assert len({case.id for case in fixture.cases}) == len(fixture.cases)
    assert all(case.annotations and case.rationale for case in fixture.cases)


async def test_a_scripted_model_is_scored_against_independent_labels(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    predictions = json.loads((FIXTURE.parent / "fake-predictions.json").read_text())
    scripts = {(case.image_sha256, case.question): predictions[case.id] for case in fixture.cases}

    async def inspect(image: bytes, question: str) -> str:
        return json.dumps(scripts[hashlib.sha256(image).hexdigest(), question])

    report = await evaluate_inspections(inspect, fixture)
    assert report["passed"] is True
    assert report["metrics"]["verdict_accuracy"] == {"count": 8, "of": 8, "rate": 1.0}
    assert report["metrics"]["answer_accuracy"]["rate"] == 1.0
    assert report["metrics"]["abstention_recall"]["rate"] == 1.0
    assert report["metrics"]["false_claim_rate"]["rate"] == 0.0
    assert report == await evaluate_inspections(inspect, fixture)
    output = tmp_path / "visual-inspection.json"
    output.write_text(json.dumps(report, sort_keys=True))
    assert json.loads(output.read_text()) == report


async def test_supported_verdicts_cannot_certify_wrong_answers_or_facts() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        return json.dumps({"verdict": "supported", "answer": "invented", "observed_facts": []})

    report = await evaluate_inspections(inspect, load_visual_fixture())
    assert report["passed"] is False
    assert report["metrics"]["answer_accuracy"]["rate"] == 0.0
    assert report["metrics"]["grounding_precision"]["rate"] == 0.0
    assert report["metrics"]["false_claim_rate"]["rate"] == 1.0
    assert report["metrics"]["abstention_recall"]["rate"] == 0.0


async def test_correct_answer_does_not_hide_an_unsupported_observed_fact() -> None:
    fixture = load_visual_fixture()
    fixture = fixture.model_copy(update={"cases": (fixture.cases[0],)})

    async def inspect(_image: bytes, _question: str) -> str:
        return json.dumps(
            {"verdict": "supported", "answer": "17 March 2027", "observed_facts": ["red shirt"]}
        )

    report = await evaluate_inspections(inspect, fixture)
    assert report["metrics"]["answer_accuracy"]["rate"] == 1.0
    assert report["metrics"]["grounding_precision"]["rate"] == 0.5
    assert report["metrics"]["false_claim_rate"]["rate"] == 1.0
    assert report["passed"] is False


async def test_an_irrelevant_but_visible_fact_is_wrong_without_becoming_a_false_claim() -> None:
    fixture = load_visual_fixture()
    fixture = fixture.model_copy(update={"cases": (fixture.cases[0],)})

    async def inspect(_image: bytes, _question: str) -> str:
        return '{"verdict":"supported","answer":"Project Orchid"}'

    report = await evaluate_inspections(inspect, fixture)
    assert report["metrics"]["answer_accuracy"]["rate"] == 0.0
    assert report["metrics"]["grounding_precision"]["rate"] == 1.0
    assert report["metrics"]["false_claim_rate"]["rate"] == 0.0
    assert report["passed"] is False


async def test_always_abstaining_cannot_pass_the_benchmark() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        return '{"verdict":"unclear"}'

    report = await evaluate_inspections(inspect, load_visual_fixture())
    assert report["passed"] is False
    assert report["metrics"]["abstention_recall"]["rate"] == 1.0
    assert report["metrics"]["false_abstention_rate"]["rate"] == 1.0
    assert report["metrics"]["grounding_precision"]["rate"] is None
    assert report["metrics"]["false_claim_rate"]["rate"] is None


async def test_invalid_output_is_not_dropped_from_the_denominator() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        return '{"verdict":"supported"}'

    report = await evaluate_inspections(inspect, load_visual_fixture())
    assert report["passed"] is False
    assert report["metrics"]["invalid_output_rate"] == {"count": 8, "of": 8, "rate": 1.0}
    assert report["metrics"]["verdict_accuracy"]["rate"] == 0.0
    assert report["metrics"]["answer_accuracy"]["rate"] == 0.0


async def test_model_failures_are_reported_separately_from_invalid_outputs() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        raise TimeoutError("fake model timed out")

    report = await evaluate_inspections(inspect, load_visual_fixture())
    assert report["metrics"]["model_error_rate"] == {"count": 8, "of": 8, "rate": 1.0}
    assert report["passed"] is False


async def test_a_provider_fault_is_a_case_level_model_error() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        raise ProviderFault("perception", "timeout")

    report = await evaluate_inspections(inspect, load_visual_fixture())
    assert report["metrics"]["model_error_rate"] == {"count": 8, "of": 8, "rate": 1.0}
    assert report["metrics"]["invalid_output_rate"]["rate"] == 0.0
    assert report["passed"] is False


async def test_a_programming_error_stays_visible_as_a_harness_failure() -> None:
    async def inspect(_image: bytes, _question: str) -> str:
        raise ProviderFault(
            "perception", "unknown", evidence=FaultEvidence(detail="internal:TypeError")
        )

    with pytest.raises(ProviderFault):
        await evaluate_inspections(inspect, load_visual_fixture())


async def test_a_recorded_cassette_replays_without_a_network(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    entries = []
    for case in fixture.cases:
        data = (fixture.image_root / case.image).read_bytes()
        entries.append(
            {
                "image_sha256": hashlib.sha256(data).hexdigest(),
                "question": case.question,
                "provider": "google",
                "model": "gemini-3.8-flash",
                "schema_version": VISUAL_INSPECTION_SCHEMA_VERSION,
                "usage": {"input_tokens": 900, "output_tokens": 40},
                "latency_ms": 800,
                "text": json.dumps({"verdict": "supported", "answer": "Project Orchid"}),
            }
        )
    path = tmp_path / "provider-cassette.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    report = await evaluate_inspections(cassette_inspector(path), fixture)

    assert report["metrics"]["invalid_output_rate"]["rate"] == 0.0
    assert report["metrics"]["model_error_rate"]["rate"] == 0.0
    assert report["metrics"]["verdict_accuracy"]["of"] == 8


async def test_an_unrecorded_case_is_never_answered_from_the_model(tmp_path: Path) -> None:
    path = tmp_path / "provider-cassette.jsonl"
    path.write_text("")

    with pytest.raises(LookupError):
        await cassette_inspector(path)(b"image-bytes", "What is the beta date?")


async def test_the_recorder_stops_at_the_request_budget(tmp_path: Path) -> None:
    model = vision_model(answers=["Project Orchid"])
    data = (FIXTURE.parent / "screen.png").read_bytes()
    cases = [("screen.png", "What is the project name?", data)] * 3
    budget = ExperimentBudget(max_requests=2, ledger=tmp_path / "attempts.jsonl")

    recorded = await record_visual_cassette(
        cases,
        model,
        path=tmp_path / "provider-cassette.jsonl",
        budget=budget,
    )

    assert len(recorded) == 2
    assert budget.requests == 2
    assert budget.spent_usd > 0
    assert recorded[0]["provider"] == "google"
    assert recorded[0]["schema_version"] == VISUAL_INSPECTION_SCHEMA_VERSION
    assert recorded[0]["usage"]["total_tokens"] == 1500


async def test_a_reviewed_alias_grounds_a_paraphrase() -> None:
    fixture = load_visual_fixture()
    case = fixture.cases[0]
    fixture = fixture.model_copy(
        update={
            "cases": (
                case.model_copy(
                    update={
                        "annotations": (
                            case.annotations[0].model_copy(
                                update={"fact_aliases": ("the title says project orchid",)}
                            ),
                            *case.annotations[1:],
                        )
                    }
                ),
            )
        }
    )

    async def inspect(_image: bytes, _question: str) -> str:
        return json.dumps(
            {
                "verdict": "supported",
                "answer": "17 March 2027",
                "observed_facts": ["the title says project orchid"],
            }
        )

    report = await evaluate_inspections(inspect, fixture)
    assert report["metrics"]["grounding_precision"]["rate"] == 1.0
    assert report["metrics"]["false_claim_rate"]["rate"] == 0.0


async def test_the_recorder_stops_before_the_provider_call_when_the_request_budget_is_reached(
    tmp_path: Path,
) -> None:
    model = vision_model(answers=["Project Orchid"])
    data = (FIXTURE.parent / "screen.png").read_bytes()
    budget = ExperimentBudget(max_requests=1, ledger=tmp_path / "attempts.jsonl")
    budget.record(
        {"source": "test", "question": "already spent"},
        usage={"input_tokens": 100, "output_tokens": 10},
    )

    recorded = await record_visual_cassette(
        [("screen.png", "What is the project name?", data)],
        model,
        path=tmp_path / "provider-cassette.jsonl",
        budget=budget,
    )

    assert recorded == []
    assert model.structured_seen == []


async def test_the_recorder_stops_before_the_provider_call_when_the_cost_budget_is_reached(
    tmp_path: Path,
) -> None:
    model = vision_model(answers=["Project Orchid"])
    data = (FIXTURE.parent / "screen.png").read_bytes()
    budget = ExperimentBudget(max_requests=12, max_usd=0.0001, ledger=tmp_path / "attempts.jsonl")

    recorded = await record_visual_cassette(
        [("screen.png", "What is the project name?", data)],
        model,
        path=tmp_path / "provider-cassette.jsonl",
        budget=budget,
    )

    assert recorded == []
    assert model.structured_seen == []


def test_the_budget_remembers_the_spent_requests_after_a_restart(tmp_path: Path) -> None:
    ledger = tmp_path / "attempts.jsonl"

    first = ExperimentBudget(max_requests=12, ledger=ledger)
    first.record({"source": "test"}, usage={"input_tokens": 100, "output_tokens": 10})

    second = ExperimentBudget(max_requests=12, ledger=ledger)

    assert second.requests == 1
    assert second.spent_usd > 0


def test_the_dataset_rejects_relabelled_images_with_stale_hashes(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE.read_text())
    raw["cases"][0]["image_sha256"] = "0" * 64
    target = tmp_path / "cases.json"
    target.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="image hash"):
        load_visual_fixture(target, image_root=FIXTURE.parent)


def test_the_controlled_images_are_pinned_by_the_dataset_digests() -> None:
    raw = json.loads(FIXTURE.read_text())
    pinned = {case["image"]: case["image_sha256"] for case in raw["cases"]}
    assert set(pinned) == set(VISUAL_IMAGE_NAMES)
    for image, digest in pinned.items():
        assert hashlib.sha256((FIXTURE.parent / image).read_bytes()).hexdigest() == digest


def test_the_generator_reproduces_the_committed_images(tmp_path: Path) -> None:
    generate_visual_images(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == set(VISUAL_IMAGE_NAMES)
    for image in VISUAL_IMAGE_NAMES:
        assert image_pixel_digest(tmp_path / image) == image_pixel_digest(FIXTURE.parent / image)
