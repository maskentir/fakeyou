from fakeyou.endpointing import SemanticEndpoint


def test_endpoint_fires_once_per_stable_completion() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("are you coming") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today?") is False
    assert endpoint.observe("are you coming today?") is True
    assert endpoint.observe("are you coming today?") is False


def test_endpoint_ignores_short_and_unstable_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("ok?") is False
    assert endpoint.observe("ok?") is False
    endpoint.reset()
    assert endpoint.observe("please open the door.") is False
    assert endpoint.observe("please open the hatch.") is False
    assert endpoint.observe("please open the hatch.") is True


def test_endpoint_fires_on_unpunctuated_stable_run() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is True


def test_endpoint_fires_once_per_unpunctuated_run() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is True
    assert endpoint.observe("are you coming today") is False


def test_endpoint_different_words_break_run() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("please open the hatch") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today") is True


def test_endpoint_ignores_short_stable_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("see you soon") is False
    assert endpoint.observe("see you soon") is False
    assert endpoint.observe("see you soon") is False
    assert endpoint.observe("see you soon") is False
    assert endpoint.observe("see you soon") is False


def test_endpoint_punctuated_fires_fast_on_second() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("please open the hatch.") is False
    assert endpoint.observe("please open the hatch.") is True


def test_observe_stop_fires_on_long_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe_stop("can you help me today") is True


def test_observe_stop_ignores_short_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe_stop("see you") is False


def test_observe_stop_ignores_empty_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe_stop("") is False
    assert endpoint.observe_stop("   ") is False


def test_observe_stop_accepts_min_words_override() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe_stop("see you", min_words=1) is True
    assert endpoint.observe_stop("see you") is False
    assert endpoint.observe_stop("", min_words=1) is False
