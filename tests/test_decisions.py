import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import PhaseUpdate
from fakeyou.debrief.debrief_schema import DebriefValidationError
from fakeyou.debrief.register import MeetingScope, records_from_report, register_page
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir
from tests._debrief_support import AT as RECORD_AT
from tests._debrief_support import report_for
from tests.test_api_memory import MemoryHarness
from tests.test_api_sessions import AT, SESSION_PAYLOAD, STATUS_AT

MEETINGS: dict[str, dict[str, Any]] = {
    meeting["meeting_id"]: meeting
    for meeting in json.loads(
        (Path(__file__).parent / "fixtures" / "decision_register_meetings.json").read_text(
            encoding="utf-8"
        )
    )["meetings"]
}
FIRST, SECOND = "arch-review-1", "arch-review-2"
OWNER_APPROVAL_CLAIM = "The owner approved the extra migration budget."


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[MemoryHarness]:
    harness = MemoryHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


def seed_session(
    api: MemoryHarness, meeting_id: str, *, owner_id: str = "demo_owner", twin_id: str = "demo_twin"
) -> None:
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": meeting_id,
            "owner_id": owner_id,
            "twin_id": twin_id,
            "audio": True,
            "conversation": True,
            "persona": {**SESSION_PAYLOAD["persona"], "owner_id": owner_id, "twin_id": twin_id},
        }
    )
    snapshot = build_snapshot(create, meeting_id=meeting_id, at=AT)
    api.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)


def write_record(api: MemoryHarness, meeting_id: str, fixture: str) -> Path:
    record = MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).record
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "".join(
            json.dumps(
                {
                    "kind": "transcript",
                    "at": RECORD_AT.isoformat(),
                    "seq": index,
                    "id": line["id"],
                    "spoken_at": "12:00:00",
                    "source": "caption",
                    "text": line["text"],
                    "participant_id": line["participant_id"],
                    "speaker_evidence_id": None,
                }
            )
            + "\n"
            for index, line in enumerate(MEETINGS[fixture]["transcript"], start=1)
        ),
        encoding="utf-8",
    )
    return record


def write_debrief(
    api: MemoryHarness,
    meeting_id: str,
    *,
    fixture: str | None = None,
    owner_id: str = "demo_owner",
    twin_id: str = "demo_twin",
    **changes: Any,
) -> Path:
    fixture = fixture or meeting_id
    record = write_record(api, meeting_id, fixture)
    report = report_for(
        record,
        owner_id=owner_id,
        twin_id=twin_id,
        meeting_id=meeting_id,
        **{"items": MEETINGS[fixture]["items"]} | changes,
    )
    artifact = MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).debrief_json
    write_atomically(artifact, report.model_dump_json().encode("utf-8"))
    return record


def seed_meeting(api: MemoryHarness, meeting_id: str, **scope: Any) -> Path:
    seed_session(
        api, meeting_id, **{key: scope[key] for key in ("owner_id", "twin_id") if key in scope}
    )
    return write_debrief(api, meeting_id, **scope)


def seed_two_meetings(api: MemoryHarness) -> None:
    seed_meeting(api, FIRST)
    seed_meeting(api, SECOND)


async def register(api: MemoryHarness, query: str = "") -> dict[str, Any]:
    response = await api.get(f"/v1/decisions{query}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def by_text(body: dict[str, Any], text: str) -> dict[str, Any]:
    found: list[dict[str, Any]] = [item for item in body["records"] if item["text"] == text]
    [record] = found
    return record


async def test_register_returns_every_item_of_two_meetings_with_its_evidence(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)

    body = await register(api)

    assert [(item["meeting_id"], item["kind"], item["status"]) for item in body["records"]] == [
        (FIRST, "decision_claim", "stated"),
        (FIRST, "proposal", "proposed"),
        (FIRST, "decision_claim", "stated"),
        (FIRST, "open_question", "stated"),
        (SECOND, "decision_claim", "stated"),
        (SECOND, "action_item", "stated"),
        (SECOND, "owner_decision", "needs_owner"),
    ]
    for record in body["records"]:
        fixture = MEETINGS[record["meeting_id"]]
        [expected] = [item for item in fixture["items"] if item["text"] == record["text"]]
        lines = {f"transcript:{line['id']}": line for line in fixture["transcript"]}
        assert [(item["entry_id"], item["quote"]) for item in record["evidence"]] == [
            (ref["entry_id"], ref["quote"]) for ref in expected["evidence"]
        ]
        for evidence in record["evidence"]:
            line = lines[evidence["entry_id"]]
            assert evidence["quote"] in line["text"]
            assert evidence["participant_id"] == line["participant_id"]
            assert evidence["source_kind"] == "participant"
    assert [
        (item["meeting_id"], item["debrief"], item["record_count"]) for item in body["meetings"]
    ] == [
        (FIRST, "available", 4),
        (SECOND, "available", 3),
    ]
    assert body["next_cursor"] is None
    assert str(api.settings.paths.record_dir) not in json.dumps(body)


async def test_no_record_is_ever_presented_as_owner_approved(api: MemoryHarness) -> None:
    seed_two_meetings(api)

    body = await register(api)

    assert {
        (item["authority"], item["owner_approved"], item["review_status"])
        for item in body["records"]
    } == {("meeting_claim", False, "unreviewed")}


async def test_participant_claim_of_owner_approval_stays_an_attributed_claim(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)

    claim = by_text(await register(api), OWNER_APPROVAL_CLAIM)

    assert (claim["kind"], claim["status"]) == ("decision_claim", "stated")
    assert claim["owner_approved"] is False
    assert [item["participant_id"] for item in claim["evidence"]] == ["budi"]


async def test_contradictory_claims_stay_separate_and_attributed(api: MemoryHarness) -> None:
    seed_two_meetings(api)

    body = await register(api, "?kind=decision_claim")
    postgres = by_text(body, "The billing service moves to Postgres 17 this quarter.")
    mysql = by_text(body, "The billing service stays on MySQL for now.")

    assert (postgres["meeting_id"], postgres["evidence"][0]["participant_id"]) == (FIRST, "rina")
    assert (mysql["meeting_id"], mysql["evidence"][0]["participant_id"]) == (SECOND, "budi")
    assert postgres["record_id"] != mysql["record_id"]
    assert postgres["status"] == mysql["status"] == "stated"
    assert len(body["records"]) == 3


async def test_unresolved_proposal_is_not_an_accepted_decision(api: MemoryHarness) -> None:
    seed_two_meetings(api)

    proposals = await register(api, "?status=proposed")
    decisions = await register(api, "?kind=decision_claim")

    assert [(item["kind"], item["text"]) for item in proposals["records"]] == [
        ("proposal", "Freeze schema changes until the migration is done.")
    ]
    assert "Freeze schema changes until the migration is done." not in {
        item["text"] for item in decisions["records"]
    }


async def test_attribution_is_the_recorded_label_never_a_verified_identity(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)

    body = await register(api)
    proposal = by_text(body, "Freeze schema changes until the migration is done.")
    action = by_text(body, "Dimas writes the rollback plan.")

    assert [(item["participant_id"], item["attribution"]) for item in proposal["evidence"]] == [
        ("unknown", "unattributed")
    ]
    assert [(item["participant_id"], item["attribution"]) for item in action["evidence"]] == [
        ("rina", "recorded_label")
    ]
    assert {
        evidence["attribution"] for record in body["records"] for evidence in record["evidence"]
    } <= {"recorded_label", "unattributed"}
    assert "identified" not in json.dumps(body)
    assert (action["assignee_claim"], action["due_text"]) == ("Dimas", "Friday")
    assert body["meetings"][0]["source_limitations"] == ["Unknown attribution."]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (f"?meeting_id={SECOND}", 3),
        ("?kind=owner_decision", 1),
        ("?status=needs_owner", 1),
        ("?kind=action_item&status=stated", 2),
        ("?kind=proposal&status=needs_owner", 0),
        ("?twin_id=demo_twin", 7),
        ("?twin_id=side_twin", 2),
        ("?meeting_id=never-held", 0),
    ],
)
async def test_register_filters_by_meeting_twin_kind_and_status(
    api: MemoryHarness, query: str, expected: int
) -> None:
    seed_two_meetings(api)
    seed_meeting(
        api,
        "side-sync-1",
        twin_id="side_twin",
        fixture=SECOND,
        items=MEETINGS[SECOND]["items"][:2],
    )

    body = await register(api, query)

    assert len(body["records"]) == expected


async def test_another_owner_never_appears_in_the_register(api: MemoryHarness) -> None:
    seed_meeting(api, FIRST)
    seed_meeting(api, "rival-review", owner_id="other_owner", twin_id="other_twin", fixture=SECOND)

    body = await register(api)
    filtered = await register(api, "?meeting_id=rival-review")

    assert {item["meeting_id"] for item in body["records"]} == {FIRST}
    assert [item["meeting_id"] for item in body["meetings"]] == [FIRST]
    assert filtered["records"] == filtered["meetings"] == []
    assert "rival-review" not in json.dumps(body)


async def test_debrief_written_for_another_owner_is_not_served_under_this_session(
    api: MemoryHarness,
) -> None:
    seed_session(api, FIRST)
    write_debrief(api, FIRST, owner_id="other_owner", twin_id="other_twin")

    body = await register(api)

    assert body["records"] == []
    assert [(item["meeting_id"], item["debrief"]) for item in body["meetings"]] == [
        (FIRST, "invalid")
    ]


async def test_register_requires_the_owner_token(api: MemoryHarness) -> None:
    seed_two_meetings(api)

    assert (await api.get("/v1/decisions", principal=None)).status_code == 401
    assert (await api.get("/v1/decisions", principal="admin")).status_code == 403


async def test_forgotten_meeting_leaves_the_register(api: MemoryHarness) -> None:
    seed_two_meetings(api)
    await api.status.record_phase(
        FIRST, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )

    forgotten = await api.post(f"/v1/sessions/{FIRST}/forget", json={"keep_record": False})
    body = await register(api)

    assert forgotten.status_code == 200
    assert {item["meeting_id"] for item in body["records"]} == {SECOND}
    assert [item["meeting_id"] for item in body["meetings"]] == [SECOND]
    assert OWNER_APPROVAL_CLAIM not in json.dumps(body)


async def test_repeated_reads_and_a_republished_debrief_do_not_duplicate_records(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)
    first = await register(api)

    write_debrief(api, FIRST)
    write_debrief(api, SECOND, items=[*MEETINGS[SECOND]["items"], MEETINGS[SECOND]["items"][0]])
    second = await register(api)

    ids = [item["record_id"] for item in second["records"]]
    assert ids == [item["record_id"] for item in first["records"]]
    assert len(ids) == len(set(ids)) == 7


async def test_meeting_without_a_usable_debrief_is_reported_not_treated_as_empty(
    api: MemoryHarness,
) -> None:
    seed_meeting(api, FIRST)
    seed_session(api, "no-debrief")
    seed_meeting(api, "empty-debrief", fixture=SECOND, items=[])
    stale = seed_meeting(api, "stale-debrief", fixture=SECOND)
    with stale.open("a", encoding="utf-8") as record:
        record.write(
            json.dumps(
                {
                    "kind": "transcript",
                    "at": RECORD_AT.isoformat(),
                    "seq": 9,
                    "id": "e9",
                    "spoken_at": "12:05:00",
                    "source": "caption",
                    "text": "Actually we reversed that decision.",
                    "participant_id": "rina",
                    "speaker_evidence_id": None,
                }
            )
            + "\n"
        )

    body = await register(api)

    assert {item["meeting_id"] for item in body["records"]} == {FIRST}
    assert {
        item["meeting_id"]: (item["debrief"], item["record_count"]) for item in body["meetings"]
    } == {
        FIRST: ("available", 4),
        "no-debrief": ("missing", 0),
        "empty-debrief": ("available", 0),
        "stale-debrief": ("stale", 0),
    }


async def test_evidence_that_does_not_match_the_record_yields_no_record(
    api: MemoryHarness,
) -> None:
    seed_meeting(api, SECOND)
    seed_session(api, FIRST)
    write_debrief(
        api,
        FIRST,
        items=[
            {
                "kind": "decision_claim",
                "status": "stated",
                "text": "The billing service is retired.",
                "evidence": [{"entry_id": "transcript:e1", "quote": "retire the billing service"}],
            }
        ],
    )

    body = await register(api)

    assert {item["meeting_id"] for item in body["records"]} == {SECOND}
    assert {item["meeting_id"]: item["debrief"] for item in body["meetings"]} == {
        FIRST: "invalid",
        SECOND: "available",
    }


def test_report_evidence_must_resolve_inside_the_report_itself(tmp_path: Path) -> None:
    record = tmp_path / FIRST / "record.jsonl"
    record.parent.mkdir()
    record.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": RECORD_AT.isoformat(),
                "seq": 1,
                "id": "e1",
                "spoken_at": "12:00:00",
                "source": "caption",
                "text": MEETINGS[FIRST]["transcript"][0]["text"],
                "participant_id": "rina",
                "speaker_evidence_id": None,
            }
        )
        + "\n"
    )
    scope = MeetingScope("demo_owner", "demo_twin", FIRST, AT)
    items = MEETINGS[FIRST]["items"][:1]
    complete = report_for(
        record, owner_id="demo_owner", twin_id="demo_twin", meeting_id=FIRST, items=items
    )
    unsourced = complete.model_copy(update={"evidence_sources": ()})

    assert len(records_from_report(scope, complete)) == 1
    with pytest.raises(DebriefValidationError, match="canonical debrief"):
        records_from_report(scope, unsourced)
    with pytest.raises(DebriefValidationError, match="canonical debrief"):
        records_from_report(MeetingScope("demo_owner", "side_twin", FIRST, AT), complete)


async def test_pages_are_stable_complete_and_do_not_overlap(api: MemoryHarness) -> None:
    seed_two_meetings(api)
    everything = [item["record_id"] for item in (await register(api))["records"]]

    seen: list[str] = []
    cursor: str | None = None
    sizes: list[int] = []
    while True:
        query = "?limit=3" + (f"&cursor={cursor}" if cursor else "")
        body = await register(api, query)
        sizes.append(len(body["records"]))
        seen.extend(item["record_id"] for item in body["records"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sizes == [3, 3, 1]
    assert seen == everything
    assert (await register(api, "?limit=7"))["next_cursor"] is None


async def rejected_reason(api: MemoryHarness, query: str) -> str:
    response = await api.get(f"/v1/decisions{query}")
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "input.invalid"
    reason: str = error["message"]
    return reason


async def test_cursor_from_another_filter_is_refused(api: MemoryHarness) -> None:
    seed_two_meetings(api)
    cursor = (await register(api, "?limit=2"))["next_cursor"]

    for query in ("?kind=decision_claim", f"?meeting_id={FIRST}", "?status=stated"):
        assert "cursor_query_mismatch" in await rejected_reason(
            api, f"{query}&limit=2&cursor={cursor}"
        )
    assert len((await register(api, f"?limit=50&cursor={cursor}"))["records"]) == 5


async def test_cursor_after_a_changed_debrief_is_stale(api: MemoryHarness) -> None:
    seed_two_meetings(api)
    cursor = (await register(api, "?limit=2"))["next_cursor"]

    write_debrief(api, FIRST, items=MEETINGS[FIRST]["items"][:3])

    assert "cursor_stale" in await rejected_reason(api, f"?limit=2&cursor={cursor}")


async def test_cursor_survives_a_debrief_regenerated_with_the_same_items(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)
    everything = [item["record_id"] for item in (await register(api))["records"]]
    first = await register(api, "?limit=2")

    write_debrief(api, FIRST)
    rest = await register(api, f"?limit=50&cursor={first['next_cursor']}")

    assert [item["record_id"] for item in [*first["records"], *rest["records"]]] == everything


async def test_cursor_after_a_forgotten_meeting_is_stale(api: MemoryHarness) -> None:
    seed_two_meetings(api)
    cursor = (await register(api, "?limit=2"))["next_cursor"]
    await api.status.record_phase(
        FIRST, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )
    await api.post(f"/v1/sessions/{FIRST}/forget", json={"keep_record": False})

    assert "cursor_stale" in await rejected_reason(api, f"?limit=2&cursor={cursor}")


async def test_forget_keeping_the_record_removes_derived_memory_only(
    api: MemoryHarness,
) -> None:
    seed_two_meetings(api)
    before = await register(api, f"?meeting_id={FIRST}")
    await api.status.record_phase(
        FIRST, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )

    forgotten = await api.post(f"/v1/sessions/{FIRST}/forget", json={"keep_record": True})
    after = await register(api, f"?meeting_id={FIRST}")

    assert forgotten.status_code == 200
    stages = {stage["stage"]: stage["status"] for stage in forgotten.json()["stages"]}
    assert stages.pop("graph") != "kept"
    assert stages == {
        "record": "kept",
        "journal": "kept",
        "browser_profile": "kept",
        "status": "kept",
    }
    assert MeetingDir(api.settings.paths.record_dir, SessionId(FIRST)).record.exists()
    assert after == before
    assert after["meetings"][0]["debrief"] == "available"


@pytest.mark.parametrize(
    "query", ["?cursor=not-a-cursor", "?limit=0", "?limit=201", "?kind=verdict"]
)
async def test_unreadable_query_is_rejected(api: MemoryHarness, query: str) -> None:
    seed_two_meetings(api)

    response = await api.get(f"/v1/decisions{query}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"


def test_cursor_that_is_not_a_register_position_is_refused() -> None:
    with pytest.raises(FakeYouError, match="cursor_invalid"):
        register_page((), cursor="bm90LWpzb24=", limit=10, query="0" * 32)


def same_line_item(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "action_item",
        "status": "stated",
        "text": "Dimas writes the rollback plan.",
        "assignee_claim": "Dimas",
        "due_text": "Friday",
        "evidence": [
            {"entry_id": "transcript:e2", "quote": "Dimas will write the rollback plan by Friday"}
        ],
    } | changes


async def test_items_differing_only_in_quotes_are_separate_records(api: MemoryHarness) -> None:
    claim = {
        "kind": "decision_claim",
        "status": "stated",
        "text": "The billing service moves to Postgres 17 this quarter.",
    }
    whole = claim | {
        "evidence": [
            {"entry_id": "transcript:e1", "quote": "move the billing service to Postgres 17"}
        ]
    }
    narrower = claim | {
        "evidence": [{"entry_id": "transcript:e1", "quote": "Postgres 17 this quarter"}]
    }
    seed_meeting(api, FIRST, items=[whole, narrower, whole])

    body = await register(api)

    assert [record["evidence"][0]["quote"] for record in body["records"]] == [
        "move the billing service to Postgres 17",
        "Postgres 17 this quarter",
    ]
    assert len({record["record_id"] for record in body["records"]}) == 2


async def test_items_differing_only_in_assignee_or_due_are_separate_records(
    api: MemoryHarness,
) -> None:
    items = [
        same_line_item(),
        same_line_item(due_text=None),
        same_line_item(assignee_claim=None, due_text=None),
        same_line_item(),
    ]
    seed_meeting(api, SECOND, items=items)

    body = await register(api)

    assert [(item["assignee_claim"], item["due_text"]) for item in body["records"]] == [
        ("Dimas", "Friday"),
        ("Dimas", None),
        (None, None),
    ]
    assert len({record["record_id"] for record in body["records"]}) == 3


async def test_the_same_item_in_another_meeting_is_another_record(api: MemoryHarness) -> None:
    seed_meeting(api, SECOND, items=[same_line_item()])
    seed_meeting(api, "arch-review-3", fixture=SECOND, items=[same_line_item()])

    body = await register(api)

    assert [item["meeting_id"] for item in body["records"]] == [SECOND, "arch-review-3"]
    assert len({record["record_id"] for record in body["records"]}) == 2
