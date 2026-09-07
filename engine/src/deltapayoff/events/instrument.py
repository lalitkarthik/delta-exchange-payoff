"""The one spelling of a contract that every part of the system agrees on.

**A venue's symbol is the venue's business.** Delta writes `C-BTC-60000-270626`; NSE
writes `NIFTY-20260908-25500CE`, in rupees and in lots, off a different calendar. Today
that spelling is parsed in the wire decoder, keyed in the chain cache, stored in the bar
tables and read back by the screens, which is four places that would each need a second
case the day a second venue arrives.

So the adapter parses the venue's string exactly once and hands everything downstream a
typed record. The canonical string here is **derived and never stored as truth**: it
exists so that a log line, a cache key and a URL agree, and it is reconstructed from the
fields rather than the fields being reconstructed from it.

`venue_symbol` rides along verbatim so that a request back to the venue needs no reverse
lookup. It is deliberately *not* in the canonical string — the string names a contract,
and two venues listing the same contract should produce the same string.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


class InstrumentParseError(ValueError):
    """A canonical string that could not be read back into an instrument.

    Raised rather than returning `None` or a partly-filled record: `underlying` becomes
    a partition directory name downstream, and a wrong guess files quotes under an asset
    they did not happen in. `bars.py` counts unparseable venue symbols for the same
    reason; here the string is ours, so a bad one is a bug and not a statistic.
    """


class Right(str, Enum):
    """Call or put.

    The values are `C` and `P` because that is what the store's `option_type` column
    already holds and what the canonical string prints. **One spelling** — `side` below is
    a rendering of it, not a second one.
    """

    CALL = "C"
    PUT = "P"

    @property
    def side(self) -> str:
        """`"call"` or `"put"` — the spelling `docs/chain-contract.md` fixes for the
        browser and `models.ChainRow` uses for its two slots.

        The ladder has to name a side somewhere, since a `Leg` does not carry one, and
        one property here is the alternative to that mapping appearing at every fold.
        """
        return "call" if self is Right.CALL else "put"


def format_strike(strike: Decimal) -> str:
    """A strike without trailing zeros, and never in exponent form.

    `Decimal.normalize()` alone turns `60000` into `6E+4`, which would put an exponent
    into cache keys and log lines. Formatting the normalised value as fixed point undoes
    that while keeping a genuine fraction: `60000.00` prints `60000`, `1234.50` prints
    `1234.5`.
    """
    return format(strike.normalize(), "f")


def strike_as_json_number(strike: Decimal) -> int | float:
    """A strike as a **JSON number, never a string.**

    Pydantic serialises a `Decimal` to a quoted string by default, which breaks the rule
    this project holds everywhere: every decimal is a JSON number or `null`, converted
    once at the boundary. An integral strike goes out as an `int` so that `60000` stays
    `60000` and does not acquire the trailing zero the `Decimal` was chosen to avoid;
    a fractional one goes out as a `float`. Both read back as the same `Decimal`.
    """
    return int(strike) if strike == strike.to_integral_value() else float(strike)


class Instrument(BaseModel):
    """One option contract, venue-independent.

    Frozen for the same reason events are: a consumer holding one must not be surprised
    by another.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The venue's short name, upper case by convention — `DELTA`, later `NSE`.
    venue: str
    #: The underlying asset — `BTC`, `ETH`. A partition directory name downstream.
    underlying: str
    #: The expiry as a **calendar date**, not the venue's `DDMMYY` or `04-09-2026`
    #: string. The venue's own spelling stays in `venue_symbol`.
    expiry: date
    #: A `Decimal`, so that `60000` is `60000` and not `60000.0`. A float strike prints
    #: a trailing zero into every key derived from it and compares unequal to the
    #: venue's own integer.
    strike: Decimal
    right: Right
    #: The venue's string, verbatim. `None` on an instrument parsed back from a
    #: canonical string, which does not carry it.
    venue_symbol: str | None = None

    @field_validator("venue", "underlying")
    @classmethod
    def _no_separator_inside_a_part(cls, value: str) -> str:
        """The canonical string joins on `-`, so a part containing one would make it
        ambiguous and `from_canonical` would reject a string `canonical` had just
        produced. Refused where the record is built rather than where it is read."""
        if not value:
            raise ValueError("may not be empty")
        if "-" in value:
            raise ValueError(f"may not contain '-'; got {value!r}")
        return value

    @field_serializer("strike", when_used="json")
    def _serialise_strike(self, strike: Decimal) -> int | float:
        return strike_as_json_number(strike)

    def canonical(self) -> str:
        """`VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P`, e.g. `DELTA-BTC-20260627-60000-C`."""
        return "-".join(
            (
                self.venue,
                self.underlying,
                self.expiry.strftime("%Y%m%d"),
                format_strike(self.strike),
                self.right.value,
            )
        )

    @classmethod
    def from_canonical(
        cls, text: str, *, venue_symbol: str | None = None
    ) -> Instrument:
        """Invert `canonical`. `venue_symbol` is supplied because the string omits it.

        Split on the separator rather than matched with one regular expression, so that
        the failure says which of the five parts was wrong when a caller reads the
        traceback.
        """
        parts = (text or "").split("-")
        if len(parts) != 5:
            raise InstrumentParseError(
                f"expected VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P; got {text!r}"
            )
        venue, underlying, expiry_text, strike_text, right_text = parts
        if not venue or not underlying:
            raise InstrumentParseError(f"venue and underlying may not be empty: {text!r}")
        # `strptime` is lenient about digit counts and reads `2026627` as 2026-06-27,
        # which would file a contract under a date nobody wrote. The length and the
        # digits are checked first so only a true `YYYYMMDD` gets through.
        if len(expiry_text) != 8 or not expiry_text.isdigit():
            raise InstrumentParseError(
                f"expiry {expiry_text!r} is not eight digits in {text!r}"
            )
        try:
            expiry = datetime.strptime(expiry_text, "%Y%m%d").date()
        except ValueError as error:
            raise InstrumentParseError(
                f"expiry {expiry_text!r} is not YYYYMMDD in {text!r}"
            ) from error
        try:
            strike = Decimal(strike_text)
        except InvalidOperation as error:
            raise InstrumentParseError(
                f"strike {strike_text!r} is not a decimal in {text!r}"
            ) from error
        # `Decimal("NaN")` and `Decimal("Infinity")` parse without raising and are not
        # strikes. Refused here rather than surviving into a partition path.
        if not strike.is_finite():
            raise InstrumentParseError(
                f"strike {strike_text!r} is not finite in {text!r}"
            )
        try:
            right = Right(right_text)
        except ValueError as error:
            raise InstrumentParseError(
                f"right {right_text!r} is not C or P in {text!r}"
            ) from error
        return cls(
            venue=venue,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            venue_symbol=venue_symbol,
        )
