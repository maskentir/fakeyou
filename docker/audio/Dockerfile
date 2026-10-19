FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv
FROM cloakhq/cloakbrowser@sha256:aed89b504a57fdba0173d4e4cd81100206c3426c759acfe3786dfc861185eb35 AS base

FROM base AS builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra voiceprint --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --extra voiceprint \
    && mkdir -p /workspace/models
RUN curl -fsSL -o /workspace/models/eres2net-voxceleb-en-16k.onnx \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx \
    && echo "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5  /workspace/models/eres2net-voxceleb-en-16k.onnx" | sha256sum -c -
RUN .venv/bin/python -c "import onnxruntime, os, glob; libs = glob.glob(os.path.join(os.path.dirname(onnxruntime.__file__), 'capi', 'libonnxruntime.so.*')); assert len(libs) == 1, libs; os.makedirs('/opt/ort', exist_ok=True); os.symlink(libs[0], '/opt/ort/libonnxruntime.so')"

FROM base AS runtime
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio pulseaudio-utils libasound2-plugins ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 twin \
    && cp -a /root/.cloakbrowser /home/twin/.cloakbrowser \
    && mkdir -p /home/twin/.cache /home/twin/.config \
    && chown -R twin:twin /home/twin
WORKDIR /workspace
COPY --from=builder /workspace/.venv /workspace/.venv
COPY --from=builder /workspace/src /workspace/src
COPY --from=builder /workspace/models /workspace/models
COPY --from=builder /opt/ort /opt/ort
RUN mkdir -p /workspace/.runtime && chown twin:twin /workspace/.runtime
COPY docker/audio/default.pa /etc/fakeyou/default.pa
COPY docker/audio/daemon.conf /etc/pulse/daemon.conf
COPY docker/audio/asound.conf /etc/asound.conf
COPY --chmod=755 docker/audio/entrypoint.sh /usr/local/bin/fakeyou-entrypoint

# PulseAudio names mirror src/fakeyou/audio.py; keep values in sync.
ENV PATH="/workspace/.venv/bin:$PATH" \
    LD_LIBRARY_PATH="/opt/ort" \
    XDG_RUNTIME_DIR=/tmp/fakeyou-runtime \
    PULSE_SOURCE=meeting_audio.monitor \
    PULSE_SINK=agent_output \
    PYTHONUNBUFFERED=1
USER twin
ENTRYPOINT ["/usr/local/bin/fakeyou-entrypoint"]
CMD ["fakeyou", "check"]

FROM runtime AS test
USER root
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra voiceprint
USER twin
