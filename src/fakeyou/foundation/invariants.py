from typing import Annotated

from pydantic import Field, StringConstraints

IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
SessionId = Annotated[str, StringConstraints(pattern=rf"^{IDENTIFIER_PATTERN}$")]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
TwinName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
PCM_CHANNELS = 1
PCM_FORMAT = "s16le"
AGENT_SINK_NAME = "agent_output"
MEETING_SINK_NAME = "meeting_audio"
MEETING_MONITOR_SOURCE_NAME = "meeting_audio.monitor"
AGENT_MIC_SOURCE_NAME = "agent_mic"
