from fakeyou.perception.meet_protocol import DeviceOutput, MeetUser
from fakeyou.perception.roster import MeetRoster, RosterChange, StreamAssignment

AYU = "devices/ayu"
BUDI = "devices/budi"
TWIN = "devices/twin"
SCREEN = "devices/ayu-screen"


def user(device_id: str, name: str, *, present: bool = True, parent: str | None = None) -> MeetUser:
    return MeetUser(device_id=device_id, name=name, present=present, parent_device_id=parent)


def video(device_id: str, stream_id: str, *, disabled: bool = False) -> DeviceOutput:
    return DeviceOutput(device_id=device_id, kind="video", stream_id=stream_id, disabled=disabled)


def roster_with_people() -> MeetRoster:
    roster = MeetRoster()
    roster.apply_users(
        [
            user(AYU, "Ayu"),
            user(BUDI, "Budi"),
            MeetUser(device_id=TWIN, name="Rani Twin", present=True, current_user=True),
        ],
        complete=True,
    )
    return roster


def test_people_joining_are_announced_once() -> None:
    roster = MeetRoster()

    first = roster.apply_users([user(AYU, "Ayu")], complete=False)
    again = roster.apply_users([user(AYU, "Ayu")], complete=False)

    assert first == [RosterChange("joined", AYU, "Ayu")]
    assert again == []


def test_a_complete_sync_reveals_who_left() -> None:
    roster = roster_with_people()

    changes = roster.apply_users(
        [
            user(AYU, "Ayu"),
            MeetUser(device_id=TWIN, name="Rani Twin", present=True, current_user=True),
        ],
        complete=True,
    )

    assert changes == [RosterChange("left", BUDI, "Budi")]


def test_the_twin_itself_is_never_announced() -> None:
    roster = MeetRoster()

    changes = roster.apply_users(
        [MeetUser(device_id=TWIN, name="Rani Twin", present=True, current_user=True)],
        complete=False,
    )

    assert changes == []
    assert roster.own_device_ids == frozenset({TWIN})


def test_a_screen_device_means_its_parent_is_presenting() -> None:
    roster = roster_with_people()

    started = roster.apply_users([user(SCREEN, "", parent=AYU)], complete=False)
    stopped = roster.apply_users([user(SCREEN, "", parent=AYU, present=False)], complete=False)

    assert started == [RosterChange("presenting_started", AYU, "Ayu")]
    assert stopped == [RosterChange("presenting_stopped", AYU, "Ayu")]


def test_video_streams_are_assigned_to_screens_and_cameras() -> None:
    roster = roster_with_people()
    roster.apply_users([user(SCREEN, "", parent=AYU)], complete=False)
    roster.apply_outputs(
        [
            video(SCREEN, "stream-screen"),
            video(BUDI, "stream-budi"),
            video(TWIN, "stream-twin"),
            video(AYU, "stream-ayu", disabled=True),
            DeviceOutput(device_id=AYU, kind="audio", stream_id="audio-ayu", disabled=False),
        ]
    )

    assert sorted(roster.assignments(), key=lambda item: item.stream_id) == [
        StreamAssignment("stream-budi", "camera", BUDI),
        StreamAssignment("stream-screen", "screen", AYU),
    ]
    assert roster.assignment_for("stream-screen") == StreamAssignment(
        "stream-screen", "screen", AYU
    )
    assert roster.assignment_for("stream-twin") is None


def test_cameras_switching_on_and_off_are_signals() -> None:
    roster = roster_with_people()

    on = roster.apply_outputs([video(BUDI, "stream-budi")])
    same = roster.apply_outputs([video(BUDI, "stream-budi")])
    off = roster.apply_outputs([video(BUDI, "stream-budi", disabled=True)])
    own = roster.apply_outputs([video(TWIN, "stream-twin")])

    assert on == [RosterChange("camera_on", BUDI, "Budi")]
    assert same == []
    assert off == [RosterChange("camera_off", BUDI, "Budi")]
    assert own == []


def test_an_update_without_a_name_keeps_the_known_name() -> None:
    roster = roster_with_people()

    roster.apply_users([user(AYU, "")], complete=False)

    assert roster.name_of(AYU) == "Ayu"
