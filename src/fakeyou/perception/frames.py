import hashlib
import io
from datetime import datetime
from typing import Literal

import numpy as np
from PIL import Image, ImageFilter

from fakeyou.perception.percepts import Keyframe

SIGNATURE_SIZE = (128, 72)
SIGNATURE_BLUR_RADIUS = 1.0
SIGNATURE_BLOCK = 8
BLOCK_CHANGE_THRESHOLD = 4.0
MINOR_CHANGE_BLOCKS = 2
ScreenComparison = Literal["same", "minor", "distinct"]


def signature_of(image: Image.Image) -> bytes:
    small = image.convert("L").resize(SIGNATURE_SIZE, Image.Resampling.BOX)
    return small.filter(ImageFilter.GaussianBlur(SIGNATURE_BLUR_RADIUS)).tobytes()


def keyframe_from_jpeg(data: bytes, *, captured_at: datetime, path: str | None = None) -> Keyframe:
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        signature = signature_of(image)
    return Keyframe(
        sha256=hashlib.sha256(data).hexdigest(),
        signature=signature,
        width=width,
        height=height,
        captured_at=captured_at,
        path=path,
    )


def _pixels(keyframe: Keyframe) -> np.ndarray[tuple[int, int], np.dtype[np.int16]]:
    width, height = SIGNATURE_SIZE
    return np.frombuffer(keyframe.signature, dtype=np.uint8).astype(np.int16).reshape(height, width)


def changed_blocks(left: Keyframe, right: Keyframe) -> int:
    if left.sha256 == right.sha256:
        return 0
    difference = np.abs(_pixels(left) - _pixels(right)).astype(np.float64)
    rows, columns = difference.shape
    block = SIGNATURE_BLOCK
    means = difference.reshape(rows // block, block, columns // block, block).mean(axis=(1, 3))
    return int((means > BLOCK_CHANGE_THRESHOLD).sum())


def compare_screens(left: Keyframe, right: Keyframe) -> ScreenComparison:
    changed = changed_blocks(left, right)
    if changed == 0:
        return "same"
    return "minor" if changed <= MINOR_CHANGE_BLOCKS else "distinct"
