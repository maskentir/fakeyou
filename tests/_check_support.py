import json
import sys

from fakeyou.config.sanitize import sanitized_settings_dump
from fakeyou.config.settings import Settings


def print_admin_check(settings: Settings) -> None:
    sys.stdout.write(json.dumps(sanitized_settings_dump(settings)) + "\n")
