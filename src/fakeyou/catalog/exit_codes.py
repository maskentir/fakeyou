from collections.abc import Mapping
from enum import IntEnum

from fakeyou.foundation.taxonomy import TerminalReason


class ExitCode(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    INPUT_REJECTED = 2
    INFRA_FAILURE = 3
    EVAL_GATE_FAILED = 4
    CALIBRATION_REJECTED = 5


TERMINAL_EXIT_CODES: Mapping[TerminalReason, ExitCode] = {
    "infra_failure": ExitCode.INFRA_FAILURE,
    "browser_failure": ExitCode.FAILURE,
    "audio_failure": ExitCode.FAILURE,
    "conversation_failure": ExitCode.FAILURE,
    "stopped_by_owner": ExitCode.SUCCESS,
    "left_by_owner": ExitCode.SUCCESS,
    "denied_by_host": ExitCode.SUCCESS,
    "removed_by_host": ExitCode.SUCCESS,
    "ended_by_host": ExitCode.SUCCESS,
    "blocked_by_platform": ExitCode.SUCCESS,
    "lobby_timeout_reached": ExitCode.SUCCESS,
    "duration_limit_reached": ExitCode.SUCCESS,
    "alone_limit_reached": ExitCode.SUCCESS,
    "left_by_participant": ExitCode.SUCCESS,
}
