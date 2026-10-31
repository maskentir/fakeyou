from collections.abc import Mapping

from fastapi.responses import JSONResponse

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.memory.owner_notes import NOTE_CONFLICT_CODES

CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "meeting.unfinished_session",
        "meeting.already_ended",
        "memory.forget_session_live",
        *(code.value for code in NOTE_CONFLICT_CODES),
    }
)

_API_STATUS: dict[str, int] = {
    str(ErrorCode.API_UNAUTHORIZED): 401,
    str(ErrorCode.API_FORBIDDEN): 403,
    str(ErrorCode.API_NOT_FOUND): 404,
    str(ErrorCode.API_RATE_LIMITED): 429,
}


class DomainConflict(Exception):
    def __init__(self, error: FakeYouError, details: Mapping[str, object]) -> None:
        super().__init__(str(error))
        self.error = error
        self.details = dict(details)


def http_status_for(error: FakeYouError) -> int:
    api_status = _API_STATUS.get(str(error.code))
    if api_status is not None:
        return api_status
    if str(error.code) in CONFLICT_CODES:
        return 409
    if error.exit_code is ExitCode.INPUT_REJECTED:
        return 400
    if error.exit_code is ExitCode.INFRA_FAILURE:
        return 503
    return 500


def error_response(code: str, message: str, *, status: int, details: object = None) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status, content={"error": error})
