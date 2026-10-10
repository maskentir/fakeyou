import os

from pydantic import ValidationError

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings, export_dotenv


def _export_embedding_dim() -> None:
    try:
        settings = Settings()
    except (FakeYouError, ValidationError):
        return
    os.environ["EMBEDDING_DIM"] = str(settings.memory.embedder.dim)


def main() -> None:
    export_dotenv()
    _export_embedding_dim()
    from fakeyou.cli.main import main as run_cli

    run_cli()
