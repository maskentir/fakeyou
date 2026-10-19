import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefValidationError
from fakeyou.debrief.debrief_sources import chunk_sources
from tests._debrief_sources_support import source
from tests._error_support import message_pattern


def test_chunks_cover_each_source_with_default_overlap() -> None:
    sources = tuple(source(index, "x") for index in range(80))

    chunks = chunk_sources(sources)

    assert [len(chunk) for chunk in chunks] == [40, 40, 8]
    assert chunks[0][-4:] == chunks[1][:4]
    assert chunks[1][-4:] == chunks[2][:4]
    assert {entry.id for chunk in chunks for entry in chunk} == {entry.id for entry in sources}
    assert chunks == chunk_sources(sources)


def test_chunks_obey_character_limit_and_reduce_overlap_to_make_progress() -> None:
    sources = tuple(source(index, "x" * 8000) for index in range(5))

    chunks = chunk_sources(sources)

    assert [tuple(entry.id for entry in chunk) for chunk in chunks] == [
        ("transcript:entry-0", "transcript:entry-1"),
        ("transcript:entry-1", "transcript:entry-2"),
        ("transcript:entry-2", "transcript:entry-3"),
        ("transcript:entry-3", "transcript:entry-4"),
    ]
    assert all(sum(len(entry.text) for entry in chunk) <= 16000 for chunk in chunks)


def test_overlap_drops_when_whole_entry_fills_the_chunk() -> None:
    sources = (source(1, "x" * 16000), source(2, "y" * 16000))

    assert chunk_sources(sources) == ((sources[0],), (sources[1],))


def test_small_entry_limits_cannot_loop_on_overlap() -> None:
    sources = tuple(source(index, "x") for index in range(4))

    assert chunk_sources(sources, max_entries=1) == tuple((entry,) for entry in sources)
    assert chunk_sources(sources, max_entries=2, overlap=0) == (sources[:2], sources[2:])


def test_chunks_reject_oversize_entries() -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK)
    ) as caught:
        chunk_sources((source(text="x" * 16001),))

    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK


def test_twenty_chunks_are_accepted_but_twenty_one_are_rejected() -> None:
    sources = tuple(source(index, "x") for index in range(21))

    assert len(chunk_sources(sources[:20], max_entries=1, overlap=0)) == 20
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_TOO_MANY_CHUNKS)
    ) as caught:
        chunk_sources(sources, max_entries=1, overlap=0)

    assert caught.value.code is ErrorCode.DEBRIEF_TOO_MANY_CHUNKS


def test_chunks_reject_empty_sources() -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NO_SOURCES)
    ) as caught:
        chunk_sources(())

    assert caught.value.code is ErrorCode.DEBRIEF_NO_SOURCES


@pytest.mark.parametrize(
    "limits",
    [
        {"max_entries": 0},
        {"max_entries": 41},
        {"max_characters": 0},
        {"max_characters": 16001},
        {"overlap": -1},
        {"overlap": 5},
    ],
)
def test_chunk_parameters_preserve_pilot_bounds(limits: dict[str, int]) -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_CHUNK_LIMITS_INVALID)
    ) as caught:
        chunk_sources((source(),), **limits)

    assert caught.value.code is ErrorCode.DEBRIEF_CHUNK_LIMITS_INVALID
