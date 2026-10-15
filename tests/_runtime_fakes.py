from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import fields, is_dataclass
from typing import Any

from fakeyou.conversation.session import SessionResult


class SessionRun(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.effect: Callable[..., Awaitable[Any]] | None = None
        self.result: dict[str, Any] = {
            "phase": "ended",
            "terminal_reason": "stopped_by_owner",
            "detail": "ok",
        }

    async def __call__(self, config: Any, wiring: Any = None, **kwargs: Any) -> Any:
        self["config"] = config
        if is_dataclass(wiring):
            unpacked = {field.name: getattr(wiring, field.name) for field in fields(wiring)}
            unpacked.update(kwargs)
            kwargs = unpacked
        self["kwargs"] = kwargs
        self.update(kwargs)
        if self.effect is not None:
            return await self.effect(config, **kwargs)
        return SessionResult(**self.result)


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.outputs: dict[str, str] = {}
        self.sequences: dict[str, list[str]] = {}
        self.failures: dict[str, tuple[int, str]] = {}
        self.blowups: dict[str, Exception] = {}
        self.default_output = ""

    def expect(self, needle: str, output: str) -> None:
        self.outputs[needle] = output

    def expect_sequence(self, needle: str, outputs: list[str]) -> None:
        self.sequences[needle] = list(outputs)

    def fail_with(self, needle: str, returncode: int, stderr: str) -> None:
        self.failures[needle] = (returncode, stderr)

    def blow_up(self, needle: str, error: Exception) -> None:
        self.blowups[needle] = error

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in args[0]]
        self.commands.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        text = " ".join(command)
        for needle, error in self.blowups.items():
            if needle in text:
                raise error
        for needle, outputs in self.sequences.items():
            if needle in text and outputs:
                return subprocess.CompletedProcess(command, 0, outputs.pop(0), "")
        for needle, (returncode, stderr) in self.failures.items():
            if needle in text:
                return subprocess.CompletedProcess(command, returncode, "", stderr)
        for needle, output in self.outputs.items():
            if needle in text:
                return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, self.default_output, "")
