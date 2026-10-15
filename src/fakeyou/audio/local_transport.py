from pipecat.transports.local.audio import LocalAudioTransport


class ManagedLocalAudioTransport(LocalAudioTransport):
    def terminate(self) -> None:
        self._pyaudio.terminate()
