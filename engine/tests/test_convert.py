"""The boundary conversion, on its own."""

from __future__ import annotations

import pytest

from deltapayoff.convert import to_int, to_number, to_quote_number


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19448.83273946", 19448.83273946),
        ("97000", 97000.0),
        ("0.100000000000000000", 0.1),
        ("-0.98203055", -0.98203055),
        (1508, 1508.0),
        (18023.0, 18023.0),
        ("0", 0.0),
        ("", None),
        (None, None),
        ("  ", None),
        ("not-a-number", None),
        (True, None),
    ],
)
def test_to_number(raw: object, expected: float | None) -> None:
    assert to_number(raw) == expected


def test_to_number_never_returns_a_string() -> None:
    value = to_number("19448.83273946")
    assert isinstance(value, float)
    assert not isinstance(value, str)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.5", 1234.5),
        ("0.37302185", 0.37302185),
        # Absent, in each of the spellings Delta uses.
        ("0", None),
        ("0.0", None),
        ("0.00000000", None),
        ("", None),
        (None, None),
    ],
)
def test_to_quote_number(raw: object, expected: float | None) -> None:
    assert to_quote_number(raw) == expected


def test_iv_is_not_multiplied_by_a_hundred() -> None:
    assert to_quote_number("0.37302185") == 0.37302185


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(149961, 149961), ("138742", 138742), (None, None), ("", None), ("x", None)],
)
def test_to_int(raw: object, expected: int | None) -> None:
    assert to_int(raw) == expected


def test_product_id_stays_an_int() -> None:
    value = to_int(149961)
    assert isinstance(value, int)
    assert not isinstance(value, float)
