import asyncio
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Page

from fakeyou.meet.command_ledger import CommandLedger, CommandRecord
from fakeyou.meet.meet_actions import MeetActionResult
from fakeyou.orchestration import commands as runner_module
from fakeyou.orchestration.commands import CommandRunner
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.session.commands import CommandSpec, command_id

DONE = MeetActionResult("succeeded", "ok")


class FakeMeet:
    def __init__(self, kernel: KernelPort) -> None:
        self.kernel = kernel
        self.calls: list[str] = []
        self.hand = False
        self.reach_hand = True
        self.echo = True
        self.raise_on: set[str] = set()

    async def raise_hand(self, *_args: object) -> MeetActionResult:
        self.calls.append("raise_hand")
        if "raise_hand" in self.raise_on:
            raise RuntimeError("page crashed")
        self.hand = self.reach_hand
        return DONE

    async def observe_hand_raised(self, *_args: object) -> bool:
        return self.hand

    async def send_chat(self, _page: object, text: str, *_args: object) -> MeetActionResult:
        self.calls.append("send_chat")
        if "send_chat" in self.raise_on:
            raise RuntimeError("crashed after enter")
        if self.echo:
            self.kernel.chat(
                message_id=f"m{len(self.calls)}",
                device_id="dev-twin",
                text=text,
                own=True,
                backlog=False,
            )
            self.kernel.drain()
        return DONE

    async def send_reaction(self, *_args: object) -> MeetActionResult:
        self.calls.append("react")
        if "react" in self.raise_on:
            raise TimeoutError("click timed out after the ui took it")
        return DONE

    async def leave_call(self, *_args: object) -> MeetActionResult:
        self.calls.append("leave")
        return DONE


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def record_path(tmp_path: Path) -> Path:
    return tmp_path / "meeting-9" / "record.jsonl"


def in_call(kernel: KernelPort) -> KernelPort:
    kernel.call_state("in_call")
    kernel.drain()
    return kernel


@pytest.fixture
def kernel() -> KernelPort:
    return in_call(KernelPort.for_meeting("meeting-9"))


@pytest.fixture
def meet(kernel: KernelPort, monkeypatch: pytest.MonkeyPatch) -> FakeMeet:
    fake = FakeMeet(kernel)
    for name in ("raise_hand", "observe_hand_raised", "send_chat", "send_reaction", "leave_call"):
        monkeypatch.setattr(runner_module, name, getattr(fake, name))
    return fake


def runner_for(tmp_path: Path, kernel: KernelPort, *, chat_per_minute: int = 5) -> CommandRunner:
    runner = CommandRunner(
        record_path(tmp_path),
        meeting_id="meeting-9",
        kernel=kernel,
        chat_per_minute=chat_per_minute,
    )
    clock = Clock()
    runner.bind_page(
        cast(Page, object()), sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event()
    )
    return runner


async def run(runner: CommandRunner, spec: CommandSpec, plan_id: str = "p1") -> str:
    outcome = await runner.run(spec, plan_id=plan_id, ordinal=0, actor="dev-ayu")
    return outcome.state


async def test_a_toggle_already_in_place_is_observed_without_a_click(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    meet.hand = True

    assert await run(runner_for(tmp_path, kernel), CommandSpec("raise_hand")) == "effect_observed"
    assert meet.calls == []


async def test_a_toggle_is_observed_by_readback(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    assert await run(runner_for(tmp_path, kernel), CommandSpec("raise_hand")) == "effect_observed"
    assert meet.calls == ["raise_hand"]


async def test_a_toggle_retries_only_while_readback_proves_it_did_not_happen(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    meet.reach_hand = False

    state = await run(runner_for(tmp_path, kernel), CommandSpec("raise_hand"))

    assert state == "failed_after_boundary"
    assert meet.calls == ["raise_hand"] * 3


async def test_a_crashed_toggle_click_is_settled_by_readback(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    meet.raise_on.add("raise_hand")
    meet.hand = False

    state = await run(runner_for(tmp_path, kernel), CommandSpec("raise_hand"))

    assert state == "failed_after_boundary"
    assert len(meet.calls) == 3


async def test_a_chat_is_confirmed_by_its_protocol_echo(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    state = await run(runner_for(tmp_path, kernel), CommandSpec("send_chat", "Deck is up."))

    assert state == "effect_observed"
    assert meet.calls == ["send_chat"]


async def test_a_chat_without_an_echo_is_unknown_and_never_resent(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    meet.echo = False
    runner = runner_for(tmp_path, kernel)
    spec = CommandSpec("send_chat", "Deck is up.")

    first = await run(runner, spec)
    second = await run(runner, spec)

    assert (first, second) == ("unknown", "unknown")
    assert meet.calls == ["send_chat"]


async def test_a_crash_after_enter_is_unknown_and_never_resent(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    meet.raise_on.add("send_chat")
    runner = runner_for(tmp_path, kernel)
    spec = CommandSpec("send_chat", "Deck is up.")

    assert await run(runner, spec) == "unknown"
    assert await run(runner, spec) == "unknown"
    assert meet.calls == ["send_chat"]


async def test_a_reaction_click_is_acted_but_never_claimed_seen(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    runner = runner_for(tmp_path, kernel)

    assert await run(runner, CommandSpec("react", "\U0001f44d")) == "effect_unobserved"
    meet.raise_on.add("react")
    assert await run(runner, CommandSpec("react", "\U0001f44d"), plan_id="p2") == "unknown"


@pytest.mark.usefixtures("meet")
async def test_leave_marks_the_runner_left_and_notifies(tmp_path: Path, kernel: KernelPort) -> None:
    runner = runner_for(tmp_path, kernel)
    left: list[bool] = []
    runner.on_left = lambda: left.append(True)

    assert await run(runner, CommandSpec("leave")) == "effect_unobserved"
    assert runner.left is True
    assert left == [True]
    assert await run(runner, CommandSpec("raise_hand"), plan_id="p2") == (
        "cancelled_before_boundary"
    )


async def test_a_new_browser_epoch_cancels_before_the_boundary(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    runner = runner_for(tmp_path, kernel)
    kernel.call_state("lobby")
    kernel.call_state("in_call", new_epoch=True)
    kernel.drain()
    spec = CommandSpec("send_chat", "Deck is up.")
    stale = CommandRecord(
        command_id=command_id(kernel.attempt_id, "p1", 0, spec),
        kind="send_chat",
        attempt_id=kernel.attempt_id,
        plan_id="p1",
        actor="dev-ayu",
        browser_epoch=1,
        verification="protocol_echo",
        text=spec.text,
    )
    CommandLedger(record_path(tmp_path), "meeting-9").accept(stale)

    assert await run(runner, spec) == "cancelled_before_boundary"
    assert meet.calls == []


async def test_nothing_is_dispatched_outside_the_call(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    runner = runner_for(tmp_path, kernel)
    kernel.call_state("lobby")
    kernel.drain()

    assert await run(runner, CommandSpec("raise_hand")) == "cancelled_before_boundary"
    assert runner.available is False
    assert meet.calls == []


async def test_chat_limits_refuse_before_the_boundary(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    runner = runner_for(tmp_path, kernel, chat_per_minute=1)

    long_text = await run(runner, CommandSpec("send_chat", "x" * 501))
    first = await run(runner, CommandSpec("send_chat", "one"), plan_id="p2")
    limited = await run(runner, CommandSpec("send_chat", "two"), plan_id="p3")

    assert (long_text, first, limited) == ("refused", "effect_observed", "refused")
    assert meet.calls == ["send_chat"]


async def test_a_restart_closes_a_dispatched_command_as_unknown(
    tmp_path: Path, kernel: KernelPort, meet: FakeMeet
) -> None:
    ledger = CommandLedger(record_path(tmp_path), "meeting-9")
    for plan_id, stop_at in (("p-sent", "dispatching"), ("p-ready", "ready")):
        spec = CommandSpec("send_chat", plan_id)
        ledger.accept(
            CommandRecord(
                command_id=command_id(kernel.attempt_id, plan_id, 0, spec),
                kind="send_chat",
                attempt_id=kernel.attempt_id,
                plan_id=plan_id,
                actor="dev-ayu",
                browser_epoch=1,
                verification="protocol_echo",
                text=spec.text,
            )
        )
        cid = command_id(kernel.attempt_id, plan_id, 0, spec)
        ledger.move(cid, "ready")
        if stop_at == "dispatching":
            ledger.move(cid, "dispatching")
    restarted = in_call(KernelPort.for_meeting("meeting-9"))

    closed = runner_for(tmp_path, restarted).recover()

    assert {record.plan_id: record.state for record in closed} == {
        "p-sent": "unknown",
        "p-ready": "cancelled_before_boundary",
    }
    assert meet.calls == []
    assert runner_for(tmp_path, restarted).recover() == ()
