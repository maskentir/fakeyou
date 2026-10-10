import json
import sys

from fakeyou.catalog.errors import FakeYouError


def emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def emit_text(line: str) -> None:
    print(line)


def emit_progress(line: str) -> None:
    print(line, file=sys.stderr)


def emit_error(error: FakeYouError) -> None:
    print(f"fakeyou: error: {error}", file=sys.stderr)
