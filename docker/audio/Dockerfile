FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM cloakhq/cloakbrowser@sha256:aed89b504a57fdba0173d4e4cd81100206c3426c759acfe3786dfc861185eb35 AS runtime

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio pulseaudio-utils libasound2-plugins portaudio19-dev build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 twin \
    && cp -a /root/.cloakbrowser /home/twin/.cloakbrowser \
    && chown -R twin:twin /home/twin

WORKDIR /workspace
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra audio --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --extra audio \
    && mkdir -p /workspace/.runtime \
    && chown -R twin:twin /workspace/.runtime
COPY docker/audio/default.pa /etc/fakeyou/default.pa
COPY docker/audio/daemon.conf /etc/pulse/daemon.conf
COPY docker/audio/asound.conf /etc/asound.conf
COPY --chmod=755 docker/audio/entrypoint.sh /usr/local/bin/fakeyou-entrypoint

# PulseAudio names mirror src/fakeyou/audio.py; keep values in sync.
ENV PATH="/workspace/.venv/bin:$PATH" \
    XDG_RUNTIME_DIR=/tmp/fakeyou-runtime \
    PULSE_SOURCE=meeting_audio.monitor \
    PULSE_SINK=agent_output \
    PYTHONUNBUFFERED=1
USER twin
ENTRYPOINT ["/usr/local/bin/fakeyou-entrypoint"]
CMD ["fakeyou", "check"]

FROM runtime AS test
USER root
RUN uv sync --frozen --extra audio
USER twin
