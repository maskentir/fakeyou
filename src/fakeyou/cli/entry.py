import os
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    for key, value in dotenv_values(Path(".env")).items():
        if value is not None:
            os.environ.setdefault(key, value)
    from fakeyou.cli.main import main as run_cli

    run_cli()
