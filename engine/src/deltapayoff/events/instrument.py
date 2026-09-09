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
    #: ISO 4217, upper case — `"USD"`. What a price on this contract is **quoted**
    #: in, and the canonical string's last token, #57/#60 (I1). Optional with this
    #: default so an instrument built exactly as every caller built one before I1
    #: still gets a currency rather than `None` — `docs/design/events.md` §Versioning
    #: permits adding an optional field with a default without a schema bump, and
    #: `"USD"` is the only currency any venue here has used so far.
    quote_currency: str = "USD"
    #: ISO 4217, upper case. What this contract **settles** in. Equal to
    #: `quote_currency` on every venue this project has met — Delta is USD for both,
    #: `docs/settlement.md` §3.1 — but kept as a second field rather than one shared
    #: value because a venue where the two differ is a venue this type must not have
    #: to be redesigned for.
    settlement_currency: str = "USD"

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

    @field_validator("quote_currency", "settlement_currency")
    @classmethod
    def _currency_is_iso4217_shaped(cls, value: str) -> str:
        """Three upper-case letters — `"USD"`, `"INR"`. Not a lookup against the real
        ISO 4217 table, which this project has no need to carry; the shape is what
        `canonical()` needs to keep joining cleanly on `-`, and what a reader expects
        a currency code to look like on sight."""
        if len(value) != 3 or not value.isalpha() or value != value.upper():
            raise ValueError(
                "currency must be three upper-case letters, ISO 4217 style; "
                f"got {value!r}"
            )
        return value

    @field_serializer("strike", when_used="json")
    def _serialise_strike(self, strike: Decimal) -> int | float:
        return strike_as_json_number(strike)

    def canonical(self) -> str:
        """`VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY`, e.g.
        `DELTA-BTC-20260627-60000-C-USD`. The last token is `quote_currency` — a price
        on screen next to this contract is quoted in it. `settlement_currency` does
        not travel here; #57 fixed the six-part form with one currency in it, the one
        a reader needs to make sense of a price, and a string naming two currencies
        with no marker for which is which would be a worse ambiguity than the one this
        ticket closes."""
        return "-".join(
            (
                self.venue,
                self.underlying,
                self.expiry.strftime("%Y%m%d"),
                format_strike(self.strike),
                self.right.value,
                self.quote_currency,
            )
        )

    @classmethod
    def from_canonical(
        cls, text: str, *, venue_symbol: str | None = None
    ) -> Instrument:
        """Invert `canonical`. `venue_symbol` is supplied because the string omits it.

        Split on the separator rather than matched with one regular expression, so that
        the failure says which of the six parts was wrong when a caller reads the
        traceback.

        **A five-part string — the pre-I1 shape with no currency — is rejected loudly,
        not defaulted.** Guessing `"USD"` for a string that predates the suffix would
        make a caller who never updated their address book fail silently the day a
        second, non-USD venue arrives; refusing it here instead surfaces every stale
        cache key, log line and fixture at once, which is #60's whole point.
        `settlement_currency` is not recoverable from this string at all — it comes
        back at the class default, `"USD"`, exactly as an instrument built with no
        currency arguments would.
        """
        parts = (text or "").split("-")
        if len(parts) != 6:
            raise InstrumentParseError(
                f"expected VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY; got {text!r}"
            )
        venue, underlying, expiry_text, strike_text, right_text, currency_text = parts
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
        # Checked here, with the same explicit message style as every part above it,
        # rather than left to the field validator: a currency-shaped failure from
        # `cls(...)` below would raise `pydantic.ValidationError`, not
        # `InstrumentParseError`, and this parser's whole contract is that a bad
        # string raises the one exception type a caller can catch for all of them.
        if (
            len(currency_text) != 3
            or not currency_text.isalpha()
            or currency_text != currency_text.upper()
        ):
            raise InstrumentParseError(
                f"currency {currency_text!r} is not three upper-case letters in {text!r}"
            )
        return cls(
            venue=venue,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            venue_symbol=venue_symbol,
            quote_currency=currency_text,
        )
