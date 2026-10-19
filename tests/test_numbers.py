import math

import pytest

from fakeyou.foundation.numbers import finite_float, real_number, rms


@pytest.mark.parametrize(("value", "expected"), [(3, 3.0), (2.5, 2.5), (0, 0.0), (-4, -4.0)])
def test_finite_float_keeps_finite_numbers(value: object, expected: float) -> None:
    assert finite_float(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, "3", None, [1], float("nan"), float("inf"), float("-inf")]
)
def test_finite_float_rejects_booleans_text_and_non_finite_values(value: object) -> None:
    assert finite_float(value) is None


def test_real_number_keeps_non_finite_numbers() -> None:
    assert real_number(3) == 3.0
    assert real_number(float("inf")) == math.inf
    nan = real_number(float("nan"))
    assert nan is not None
    assert math.isnan(nan)


@pytest.mark.parametrize("value", [True, "3", None])
def test_real_number_rejects_booleans_and_non_numbers(value: object) -> None:
    assert real_number(value) is None


def test_rms_of_an_empty_signal_is_zero() -> None:
    assert rms([]) == 0.0


def test_rms_of_float_samples() -> None:
    assert rms([3.0, 4.0]) == math.sqrt(12.5)


def test_rms_of_integer_samples() -> None:
    assert rms([1, -1]) == 1.0
