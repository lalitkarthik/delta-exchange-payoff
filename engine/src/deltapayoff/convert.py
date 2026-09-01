"""Boundary conversions: Delta's strings become JSON numbers, exactly once.

Delta sends every decimal as a string to preserve precision. The web app never calls
`parseFloat`, so every decimal that leaves this service is a JSON number or `null`.
"""

from __future__ import annotations

_ABSENT_TEXT = {"", "-", "null", "none", "nan"}


def to_number(value: object) -> float | None:
    """Parse one of Delta's decimal strings into a float.

    `None`, an empty string and unparseable text all become `None`. A real `"0"` stays
    `0.0` here — zero is a true value for a greek, for open interest, for a mark. The
    quote fields that use `"0"` to mean "nobody is quoting" go through
    :func:`to_quote_number` instead.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _ABSENT_TEXT:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def to_quote_number(value: object) -> float | None:
    """Parse a quote field, where an absent quote may arrive as `"0"` or `""`.

    A missing best bid means nobody is bidding. Delta spells that several ways; all of
    them are `null` here, because rendering it as `0.0` would claim someone bid zero.
    An implied vol of exactly zero is likewise not a vol, it is a missing one.
    """
    number = to_number(value)
    if number is None or number == 0.0:
        return None
    return number


def to_int(value: object) -> int | None:
    """Parse an integer field. Delta sends `product_id` unquoted; keep it an int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None
