import math
from collections.abc import Sequence


def real_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def finite_float(value: object) -> float | None:
    number = real_number(value)
    if number is None or not math.isfinite(number):
        return None
    return number


def rms(samples: Sequence[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))
