from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.db.engine import open_database
from fakeyou.db.schema import SchemaStatus, stamp, status, upgrade


def db_upgrade(*, settings: Settings) -> None:
    with open_database(settings) as engine:
        upgrade(engine)
        _report(status(engine))


def db_stamp(*, settings: Settings) -> None:
    with open_database(settings) as engine:
        stamp(engine)
        _report(status(engine))


def db_status(*, settings: Settings) -> None:
    with open_database(settings) as engine:
        _report(status(engine))


def _report(state: SchemaStatus) -> None:
    emit_json(
        {
            "current": state.current,
            "head": state.head,
            "differences": list(state.differences),
        }
    )
