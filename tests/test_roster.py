from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.orchestration.join import share_roster
from fakeyou.orchestration.kernel_port import KernelPort
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
        ]
    )
    return roster


def test_people_joining_are_announced_once() -> None:
    roster = MeetRoster()

    first = roster.apply_users([user(AYU, "Ayu")])
    again = roster.apply_users([user(AYU, "Ayu")])

    assert first == [RosterChange("joined", AYU, "Ayu")]
    assert again == []


def test_people_leave_only_when_their_status_says_so() -> None:
    roster = roster_with_people()

    quiet = roster.apply_users([])
    partial = roster.apply_users([user(AYU, "Ayu")])
    left = roster.apply_users([user(BUDI, "Budi", present=False)])

    assert (quiet, partial) == ([], [])
    assert left == [RosterChange("left", BUDI, "Budi")]


def test_the_twin_itself_is_never_announced() -> None:
    roster = MeetRoster()

    changes = roster.apply_users(
        [MeetUser(device_id=TWIN, name="Rani Twin", present=True, current_user=True)]
    )

    assert changes == []
    assert roster.own_device_ids == frozenset({TWIN})


def test_a_screen_device_means_its_parent_is_presenting() -> None:
    roster = roster_with_people()

    started = roster.apply_users([user(SCREEN, "", parent=AYU)])
    stopped = roster.apply_users([user(SCREEN, "", parent=AYU, present=False)])

    assert started == [RosterChange("presenting_started", AYU, "Ayu")]
    assert stopped == [RosterChange("presenting_stopped", AYU, "Ayu")]


def test_video_streams_are_assigned_to_screens_and_cameras() -> None:
    roster = roster_with_people()
    roster.apply_users([user(SCREEN, "", parent=AYU)])
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

    roster.apply_users([user(AYU, "")])

    assert roster.name_of(AYU) == "Ayu"


def audio(device_id: str, stream_id: str) -> DeviceOutput:
    return DeviceOutput(device_id=device_id, kind="audio", stream_id=stream_id, disabled=False)


def test_people_are_present_humans_with_their_audio_sources() -> None:
    roster = roster_with_people()
    roster.apply_users([user(SCREEN, "", parent=AYU), user("devices/gone", "Gone", present=False)])
    roster.apply_outputs(
        [audio(AYU, "3553701704"), audio(TWIN, "42"), audio(SCREEN, "77"), audio(BUDI, "mixed")]
    )

    assert roster.people() == {AYU: "Ayu", BUDI: "Budi"}
    assert roster.audio_sources() == {3553701704: AYU}


def test_the_speaker_registry_names_voices_from_the_meet_roster() -> None:
    roster = roster_with_people()
    roster.apply_outputs([audio(AYU, "111"), audio(BUDI, "222")])
    registry = SpeakerRegistry()
    kernel = KernelPort.for_meeting("meeting-9")

    share_roster(registry, kernel, roster)
    kernel.drain()
    ayu = registry.resolve_csrcs(frozenset({111}))
    budi = registry.resolve_csrcs(frozenset({222}))

    assert ayu is not None
    assert budi is not None
    assert (ayu.device, ayu.display_claim, ayu.status) == (AYU, "Ayu", "confirmed")
    assert (budi.device, budi.display_claim) == (BUDI, "Budi")
    assert ayu.label != budi.label
    assert kernel.state.world.streams == {"csrc:111": AYU, "csrc:222": BUDI}
    assert set(kernel.state.world.endpoints) >= {AYU, BUDI}


def test_meeting_state_reports_people_presenter_and_cameras() -> None:
    roster = roster_with_people()
    roster.apply_users([user(SCREEN, "Ayu screen", parent=AYU)])
    roster.apply_outputs([video(AYU, "7"), video(BUDI, "8", disabled=True)])
    roster.mark_synced()

    state = roster.state()

    assert state.people == {AYU: "Ayu", BUDI: "Budi"}
    assert state.remote_count == 2
    assert state.presenter == "Ayu"
    assert state.cameras_on == frozenset({AYU})
    assert state.synced
