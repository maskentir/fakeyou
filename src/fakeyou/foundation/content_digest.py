from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock_mapping import MappedInstant

DigestRole = Literal["screen", "camera"]
MatchLevel = Literal["exact", "similar", "distinct", "unknown"]
ENCODED_BYTES_SHA256 = "sha256-encoded-bytes"
DECODED_PIXELS_SHA256 = "sha256-decoded-pixels"
DIGEST_VERSION = 1
ENCODED_PIXEL_FORMAT = "encoded"
HEARTBEAT_WITHOUT_RECORD = "heartbeat_without_record"


@dataclass(frozen=True, slots=True)
class DigestRecord:
    algorithm: str
    version: int
    width: int
    height: int
    pixel_format: str
    role: DigestRole
    stream_id: str
    subject_id: str
    sequence: int
    media_timestamp: float | None
    digest: str | None
    skip_reason: str | None = None

    def comparable_with(self, other: DigestRecord) -> bool:
        return (
            bool(self.digest)
            and bool(other.digest)
            and (self.algorithm, self.version, self.width, self.height, self.pixel_format)
            == (other.algorithm, other.version, other.width, other.height, other.pixel_format)
        )

    def same_content_as(self, other: DigestRecord) -> bool | None:
        if not self.comparable_with(other):
            return None
        return self.digest == other.digest


@dataclass(frozen=True, slots=True)
class ContentSample:
    record: DigestRecord
    captured_at: datetime
    capture: MappedInstant | None
    revision: int
    consistent: bool = True


@dataclass(frozen=True, slots=True)
class SampleSource:
    role: DigestRole
    stream_id: str
    subject_id: str


def encoded_bytes_record(
    sha256: str, source: SampleSource, *, sequence: int, size: tuple[int, int]
) -> DigestRecord:
    width, height = size
    return DigestRecord(
        algorithm=ENCODED_BYTES_SHA256,
        version=DIGEST_VERSION,
        width=width,
        height=height,
        pixel_format=ENCODED_PIXEL_FORMAT,
        role=source.role,
        stream_id=source.stream_id,
        subject_id=source.subject_id,
        sequence=sequence,
        media_timestamp=None,
        digest=sha256,
    )
