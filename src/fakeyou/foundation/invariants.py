from typing import Annotated

from pydantic import Field, StringConstraints

PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
TwinName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
POLL_TIMEOUT_MULTIPLIER = 5.0
LEAVE_BUDGET_SECONDS = 8.0
PCM_CHANNELS = 1
PCM_FORMAT = "s16le"
AGENT_SINK_NAME = "agent_output"
MEETING_SINK_NAME = "meeting_audio"
MEETING_MONITOR_SOURCE_NAME = "meeting_audio.monitor"
AGENT_MIC_SOURCE_NAME = "agent_mic"
