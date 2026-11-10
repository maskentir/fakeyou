from typing import Any

from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr

from fakeyou.config.settings import Settings
from fakeyou.db.jobs import build_job_queue, create_job_queue

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}


async def test_a_deferred_ingest_job_round_trips_with_its_arguments() -> None:
    queue = build_job_queue(InMemoryConnector())

    job_id = await queue.defer_ingest(meeting_id="m1", kind="correct")
    fetched = await queue.fetch(job_id)

    assert fetched is not None
    assert (fetched.task, fetched.status) == ("ingest_meeting", "todo")
    assert fetched.args == {"meeting_id": "m1", "kind": "correct"}
    await queue.close()


async def test_a_deferred_reembed_job_round_trips_with_its_arguments() -> None:
    queue = build_job_queue(InMemoryConnector())

    job_id = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")
    fetched = await queue.fetch(job_id)

    assert fetched is not None
    assert (fetched.task, fetched.status) == ("reembed_twin", "todo")
    assert fetched.args == {"owner_id": "owner-a", "twin_id": "twin-a"}
    assert await queue.fetch(job_id + 100) is None
    await queue.close()


async def test_the_queue_defers_into_a_real_postgres(migrated_database_url: str) -> None:
    settings = Settings(**NO_ENV_FILE)
    settings.database.url = SecretStr(migrated_database_url)
    queue = create_job_queue(settings)
    try:
        job_id = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")
        fetched = await queue.fetch(job_id)
        debrief_id = await queue.defer_debrief(meeting_id="m1", force=True)
        deferred = await queue.fetch(debrief_id)
    finally:
        await queue.close()

    assert fetched is not None
    assert fetched.args == {"owner_id": "owner-a", "twin_id": "twin-a"}
    assert deferred is not None
    assert deferred.args == {"meeting_id": "m1", "force": True}
