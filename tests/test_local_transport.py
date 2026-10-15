import pytest


def test_managed_local_transport_terminates_its_pyaudio_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyaudio = pytest.importorskip("pyaudio")
    from pipecat.transports.local.audio import LocalAudioTransportParams

    from fakeyou.audio.local_transport import ManagedLocalAudioTransport

    terminated: list[bool] = []

    class _PyAudio:
        def terminate(self) -> None:
            terminated.append(True)

    monkeypatch.setattr(pyaudio, "PyAudio", _PyAudio)

    ManagedLocalAudioTransport(LocalAudioTransportParams()).terminate()

    assert terminated == [True]
