import os

from tests.conftest import PRESERVE_OPERATOR_ENV, purge_operator_environment


def test_purge_drops_every_fakeyou_variable() -> None:
    environ = {"FAKEYOU_REPLY__API_KEY": "x", "PATH": "/bin"}

    purge_operator_environment(environ, preserve=False)

    assert environ == {"PATH": "/bin"}


def test_purge_drops_unknown_names_too() -> None:
    environ = {"FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL": "http://x", "HOME": "/root"}

    purge_operator_environment(environ, preserve=False)

    assert "FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL" not in environ


def test_purge_leaves_unrelated_variables_alone() -> None:
    environ = {"OPENAI_API_KEY": "x", "EMBEDDING_DIM": "1024"}

    purge_operator_environment(environ, preserve=False)

    assert environ == {"OPENAI_API_KEY": "x", "EMBEDDING_DIM": "1024"}


def test_purge_keeps_everything_when_the_run_opts_in() -> None:
    environ = {"FAKEYOU_REPLY__API_KEY": "x"}

    purge_operator_environment(environ, preserve=True)

    assert environ == {"FAKEYOU_REPLY__API_KEY": "x"}


def test_the_running_suite_carries_no_fakeyou_variable_from_the_operator_dotenv() -> None:
    leaked = sorted(name for name in os.environ if name.startswith("FAKEYOU_"))

    assert leaked == []
    assert not os.environ.get(PRESERVE_OPERATOR_ENV)
