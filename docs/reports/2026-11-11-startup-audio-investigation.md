# Startup audio investigation

Status: interruption reproduced locally and narrowed to the microphone capture path; production fix and Meet validation remain open.

## Result

Two fresh Chromium runs reproduced an interrupted first tone while simultaneous native recordings of `agent_output.monitor` and `agent_mic` remained continuous. A later tone in the same capture was continuous. A third run with two additional seconds before writing the first tone captured both tones continuously.

This places the reproduced interruption between native `agent_mic` recording and the browser AudioWorklet tap. Opus and Meet are not required to reproduce this particular interruption. This does not prove that the reported live symptom has only this cause.

## Method

Used the existing idle `fakeyou-worker-sessions-1` container and production `PulseWriter`, with a disposable browser profile and a locally intercepted HTTPS page. No Meet room was joined, no provider API was called, and no production configuration was changed.

Injected two continuous two-second tones, 440 Hz then 660 Hz, with a half-second scheduled pause before each tone. Recorded native sink and microphone PCM at 24 kHz and browser capture through an AudioWorklet at 48 kHz. Browser input used `agent_mic`, with echo cancellation, noise suppression and automatic gain control disabled. Analysis used 10 ms RMS windows and an absolute threshold of 500 PCM units; tone RMS was approximately 7,140.

| Run | First tone: sink / native mic | First tone: browser active spans | Second tone: browser |
| --- | --- | --- | --- |
| Fresh capture A | 2.01 / 2.01 s continuous | 70 ms, 60 ms below threshold, 1,750 ms | 2.01 s continuous |
| Fresh capture B | 2.01 / 2.01 s continuous | 70 ms, 60 ms below threshold, 1,230 ms | 2.01 s continuous |
| Capture with two extra seconds before playback | 2.00 / 2.01 s continuous | 2.01 s continuous | 2.01 s continuous |

AudioWorklet frame indices had no discontinuities in all three runs. The browser observation includes shortened first-tone duration as well as the internal gap. The native tone durations remain intact. Start times from separate files are not synchronized well enough to claim end-to-end latency; do not subtract their independent file offsets.

An initial browser run also captured continuous tones after waiting for a local peer connection. That connection never established: state remained `new`, receiver stats were empty, and its decoded track was silent. Its Opus result is invalid, not a failed audio-quality measurement. The later capture-only runs omit peer negotiation.

## Earlier 120-second measurement

The current `/private/tmp/record_twin_audio.py` selects `meeting_audio.monitor`, but its `.bak` selects `agent_output.monitor`. Their modification times differ. Therefore the current script alone cannot establish which source was used for the earlier measurement.

Both versions retain samples only in memory and print an RMS summary. The summary excludes silent runs of three seconds or longer and excludes the leading and trailing silence from its reported internal-gap list. It cannot establish phoneme integrity or correlate an interruption with the first live utterance. This is a limitation of the available evidence, not proof that the previous source was incorrect.

## Artifacts

Local PCM, metadata and analysis are retained under:

- `.runtime/audio/diagnostics/startup-audio-cold/`
- `.runtime/audio/diagnostics/startup-audio-cold-repeat/`
- `.runtime/audio/diagnostics/startup-audio-warm2/`
- `.runtime/audio/diagnostics/startup-audio-capture/` for the earlier warmed capture and unsuccessful peer experiment.

The cold and warm2 directories include their probe source. Artifacts remain local and ignored by git. `decoded.wav` is not evidence of valid Opus output.

## Next investigation boundary

The supported hypothesis is startup convergence of the browser microphone/capture path. The experiment does not distinguish the browser PulseAudio client, capture resampling and AudioContext input timing. Concurrent native recorders also change the audio server's client mix, so production timing still needs confirmation.

Do not ship the experimental two-second sleep as a fix. First repeat with the actual Meet microphone track and its constraints, capturing its first utterance alongside native output from before admission. Separate capture readiness from call admission. Then compare sender and receiver WebRTC statistics and receiver PCM if capture is continuous but the participant still hears interruptions. Require the first utterance to remain intact before claiming the issue fixed.
