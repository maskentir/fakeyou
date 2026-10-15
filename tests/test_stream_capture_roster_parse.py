from fakeyou.audio.stream_capture import (
    parse_roster_names,
)


def test_parse_roster_excludes_twin_self() -> None:
    from fakeyou.audio.stream_capture import parse_roster

    payload = {
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ]
    }
    assert parse_roster(payload, twin_name="Twin") == ("devices/1", "devices/2", "devices/3")
    assert parse_roster(payload) == ("devices/1", "devices/2", "devices/3", "devices/9")


def test_parse_roster_keeps_all_entries_when_twin_name_collides() -> None:
    from fakeyou.audio.stream_capture import parse_roster

    payload = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
    }
    assert parse_roster(payload, twin_name="Twin") == tuple(
        f"devices/{index}" for index in range(5)
    )
    mixed = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(4)]
        + [{"id": "devices/9", "name": "Host"}],
    }
    assert parse_roster(mixed, twin_name="Twin") == (
        tuple(f"devices/{index}" for index in range(4)) + ("devices/9",)
    )


def test_parse_roster_names_keeps_id_name_and_drops_twin() -> None:
    payload = {
        "roster": [
            {"id": "dev-x", "name": "  Harun  "},
            {"id": "dev-t", "name": "Rani Twin"},
            {"id": "dev-blank", "name": "   "},
            {"name": "NoDevice"},
            {"id": "dev-noname"},
            "junk",
        ]
    }
    assert parse_roster_names(payload, twin_name="Rani Twin") == ({"id": "dev-x", "name": "Harun"},)
    assert parse_roster_names({"roster": []}) == ()
    assert parse_roster_names({}) == ()
