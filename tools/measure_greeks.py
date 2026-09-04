"""What does the choice of forward cost each Greek, and what does it save?

`docs/forward.md` measured the four forwards against each other and stopped at the
forward. It found the forward robust and the discount fragile, and left the obvious next
question unasked: **the forward is not the output.** Nobody trades a forward. It is an
input to an implied volatility and to five Greeks, and those are what reach a screen. A
$22 error on a $77,590 forward is 2.9 basis points and sounds like nothing; whether it is
nothing depends entirely on what it does to delta and to theta.

So this grades **F2, F3 and F4 against F1-all-pairs**, on the numbers that actually get
used. F1-all-pairs is the reference for the reason `docs/forward.md` §4 gives: it is the
only method that assumes nothing and also passes the gate. It is a reference, not a truth
— implied volatility has no ground truth, which is the whole premise of
`docs/implied-vol.md`.

**Two attributions, reported separately.** Each method carries a forward *and* a
discount, and mixing them would let a bad discount be reported as a bad forward:

    end-to-end     the method's own (F, D) — what using it would actually produce
    forward-only   the method's F against the reference D — the forward's effect alone

`docs/forward.md` §4.1 measured the forward spanning $1.23 across window choices while
the implied rate spanned -17.1% to +9.4%. Reporting only end-to-end would attribute that
second number to the first.

**The Greek conventions are `greeks.py`'s, unchanged.** Delta and gamma undiscounted, so
a call's delta is `N(d1)` and D never touches it; vega and rho discounted and per one
percent; theta a one-calendar-day repricing. That asymmetry is why D still reaches three
of the five Greeks even though two are immune to it, and theta reaches it hardest —
`report_greeks` re-discounts a day nearer expiry at `r = -ln(D)/T`, so theta inherits the
discount's fragility rather than the forward's robustness.

Runs on the two committed fixtures, so every number reproduces from the repo, and
optionally against the live chain:

    python tools/measure_greeks.py                # both fixtures
    python tools/measure_greeks.py --live         # adds a live capture
    python tools/measure_greeks.py --runs 500     # timing runs per phase

Output is markdown, so the tables in `docs/greeks.md` are pasted rather than retyped.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))

from deltapayoff.agreement import (  # noqa: E402
    MONEYNESS_BANDS,
    band_for,
    chains_by_expiry,
    days_to_expiry,
    utc,
)
from deltapayoff.chain import build_chain  # noqa: E402
from deltapayoff.forward import (  # noqa: E402
    ForwardResult,
    f1_parity_fit,
    f2_single_strike,
    f3_carry,
    f4_spot,
    year_fraction,
)
from deltapayoff.greeks import report_greeks  # noqa: E402
from deltapayoff.solvers import solve_chain  # noqa: E402
from deltapayoff.timing import Timing, time_it  # noqa: E402

FIXTURES = ROOT / "engine" / "tests" / "fixtures"
REST = "https://api.india.delta.exchange"
USER_AGENT = "delta-exchange-payoff/0.1.0 (+greeks study)"

#: The five, in the order they are reported everywhere else in this project.
GREEKS = ("delta", "gamma", "vega", "theta", "rho")

#: A relative error needs a denominator worth dividing by. Gamma at a far wing is 1e-9,
#: and `|dgamma| / 1e-9` is a percentage of nothing — it reports the wing's smallness,
#: not the method's error. So relative figures are taken only over legs where the
#: reference Greek clears this floor, **and the count excluded is always reported**.
#: The floor is per-Greek because the five do not share a scale.
RELATIVE_FLOOR = {
    "delta": 1e-3,
    "gamma": 1e-8,
    "vega": 1e-3,
    "theta": 1e-3,
    "rho": 1e-6,
}


# --- loading snapshots ----------------------------------------------------------


#: The capture clocks, read off the `time` field inside each fixture rather than off the
#: wall clock. `year_fraction` reads the snapshot, so a fixture's T is fixed forever and
#: these numbers do not rot with the calendar.
CHAIN_TAKEN = datetime(2026, 8, 31, 16, 49, 17, tzinfo=timezone.utc)
MULTI_TAKEN = utc("2026-09-02T08:40:14Z")


def fixture(name: str) -> list[dict[str, Any]]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)["result"]


def single_expiry_chains() -> dict[str, Any]:
    """The 04-09-2026 capture. One expiry, 3.8 days — `docs/forward.md` §4's chain."""
    rows = fixture("tickers-btc-04-09-2026.json")
    return {"04-09-2026": build_chain("BTC", "04-09-2026", rows, fetched_at=CHAIN_TAKEN)}


def multi_expiry_chains() -> dict[str, Any]:
    """588 contracts, eight expiries, half a day to 85 days out. The T sweep."""
    return chains_by_expiry("BTC", fixture("tickers-btc-multi-expiry.json"), MULTI_TAKEN)


def live_chains() -> dict[str, Any]:
    """Every listed BTC expiry, now. Not reproducible — labelled as such in the doc."""
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        rows = json.load(response)["result"]
    taken = datetime.now(timezone.utc).replace(microsecond=0)
    return chains_by_expiry("BTC", rows, taken)


# --- the numbers under one forward ----------------------------------------------


@dataclass(frozen=True)
class Leg:
    """One priced leg: which strike, which side, and which moneyness band it sits in."""

    strike: float
    is_call: bool
    band: str


def solved_ivs(chain, forward: ForwardResult) -> dict[float, float]:
    """Strike to implied volatility, over the strikes that actually solved.

    `solve_chain` returns refusals too, deliberately — a strike it declined is a fact
    about the chain. They are dropped here and **counted by the caller**, because a
    refusal is not a disagreement and scoring it as one would flatter the method that
    refuses most.
    """
    return {
        strike: result.sigma
        for strike, result in solve_chain(chain, forward).items()
        if result.converged and result.sigma is not None
    }


def greeks_under(chain, forward: ForwardResult, ivs: dict[float, float]):
    """Every listed leg's five Greeks under one forward and one volatility per strike.

    One volatility per strike written to both legs, which is `compute.enrich`'s rule and
    the contract's: IV is a property of the strike, not of the leg. The Greeks then
    differ between the two legs because `is_call` does, which is the point.
    """
    years = year_fraction(chain)
    out: dict[Leg, dict[str, float]] = {}
    for row in chain.rows:
        sigma = ivs.get(row.strike)
        if sigma is None or forward.forward is None or forward.discount is None:
            continue
        band = band_for(row.strike / forward.forward, MONEYNESS_BANDS)
        for is_call, leg in ((True, row.call), (False, row.put)):
            if leg is None:
                continue
            try:
                greeks = report_greeks(
                    forward=forward.forward,
                    strike=row.strike,
                    years=years,
                    sigma=sigma,
                    discount=forward.discount,
                    is_call=is_call,
                )
            except ValueError:
                # At or past expiry, or a non-positive forward. `report_greeks` refuses
                # rather than returning zeros; so do we, and the leg is simply absent
                # from both sides of the comparison.
                continue
            out[Leg(row.strike, is_call, band)] = greeks.model_dump()
    return out


# --- comparing two runs ----------------------------------------------------------


@dataclass
class Deviation:
    """How far one method's figure landed from the reference's, over one slice."""

    n: int
    median: float
    p95: float
    worst: float
    #: Relative figures, as a percentage, over the subset clearing `RELATIVE_FLOOR`.
    n_relative: int
    median_relative: float
    p95_relative: float


def _p95(ordered: list[float]) -> float:
    """Nearest rank, matching `timing.summarise` rather than interpolating."""
    return ordered[max(int(0.95 * len(ordered)) - 1, 0)]


def deviation(
    reference: dict[Any, float], other: dict[Any, float], floor: float
) -> Deviation | None:
    """Absolute and relative deviation over the keys **both** runs produced.

    Restricting to the common set is `agreement.compare`'s rule and is kept for the same
    reason: a leg one method declined has not disagreed about it.
    """
    common = sorted(set(reference) & set(other), key=repr)
    if not common:
        return None

    gaps = sorted(abs(reference[key] - other[key]) for key in common)
    relative = sorted(
        abs(reference[key] - other[key]) / abs(reference[key]) * 100.0
        for key in common
        if abs(reference[key]) >= floor
    )
    return Deviation(
        n=len(gaps),
        median=statistics.median(gaps),
        p95=_p95(gaps),
        worst=gaps[-1],
        n_relative=len(relative),
        median_relative=statistics.median(relative) if relative else float("nan"),
        p95_relative=_p95(relative) if relative else float("nan"),
    )


def column(greeks: dict[Leg, dict[str, float]], name: str) -> dict[Leg, float]:
    return {leg: values[name] for leg, values in greeks.items()}


# --- one snapshot, every method --------------------------------------------------


@dataclass
class MethodRun:
    """One method under one attribution, with its forward, its IVs and its Greeks."""

    label: str
    mode: str
    forward: ForwardResult
    ivs: dict[float, float]
    greeks: dict[Leg, dict[str, float]]
    timings: dict[str, Timing]


FITTERS: dict[str, Callable[[Any], ForwardResult]] = {
    "F1 all pairs": lambda chain: f1_parity_fit(chain, width=None),
    "F2": f2_single_strike,
    "F3": f3_carry,
    "F4": f4_spot,
}


def run_method(chain, label: str, forward: ForwardResult, mode: str, runs: int):
    """Solve and price one snapshot under one forward, timing all three phases.

    The three are timed apart because they do not cost alike and a total would hide it:
    F4's forward is free and its volatility solve is not, so a single figure would make
    the cheap half pay for the expensive one.
    """
    _, fit_timing = time_it(lambda: FITTERS[label](chain), runs=runs)
    ivs, solve_timing = time_it(lambda: solved_ivs(chain, forward), runs=runs)
    greeks, greek_timing = time_it(lambda: greeks_under(chain, forward, ivs), runs=runs)
    return MethodRun(
        label=label,
        mode=mode,
        forward=forward,
        ivs=ivs,
        greeks=greeks,
        timings={"forward": fit_timing, "iv": solve_timing, "greeks": greek_timing},
    )


def study(chain, runs: int) -> tuple[MethodRun, list[MethodRun]]:
    """The reference run, and every (method, attribution) run graded against it."""
    fits = {label: fit(chain) for label, fit in FITTERS.items()}
    reference = run_method(chain, "F1 all pairs", fits["F1 all pairs"], "reference", runs)

    others: list[MethodRun] = []
    for label in ("F2", "F3", "F4"):
        fit = fits[label]
        if not fit.trusted or fit.forward is None:
            continue
        others.append(run_method(chain, label, fit, "end-to-end", runs))
        if reference.forward.discount is not None:
            swapped = fit.model_copy(
                update={"discount": reference.forward.discount, "implied_rate": None}
            )
            others.append(run_method(chain, label, swapped, "forward-only", runs))
    return reference, others


# --- reporting -------------------------------------------------------------------


def fmt(value: float, places: int = 6) -> str:
    if value != value:  # NaN
        return "n/a"
    return f"{value:,.{places}f}"


def report_forwards(name: str, reference: MethodRun, others: list[MethodRun]) -> None:
    print(f"\n### {name} — the forwards\n")
    if not reference.forward.trusted:
        # The benchmark failing its own gate is the one result that invalidates every
        # row beneath it, so it is said before the table rather than in a footnote.
        # Near expiry the true discount is within parts per hundred thousand of 1, so
        # its implied rate is quote noise — `docs/forward.md` §4 measured exactly this.
        print(
            f"> **The reference itself failed the gate here** — implied rate "
            f"{(reference.forward.implied_rate or 0.0) * 100:.3f}%, outside the 0-30% "
            "band. Every deviation below is measured against an untrustworthy "
            "benchmark and states the reference's noise, not the method's error.\n"
        )
    print("| method | forward | vs ref | D | implied r | trusted | n pairs | solved |")
    print("|---|---|---|---|---|---|---|---|")
    ref_f = reference.forward.forward or 0.0
    for run in [reference] + [r for r in others if r.mode != "forward-only"]:
        fwd = run.forward
        rate = "—" if fwd.implied_rate is None else f"{fwd.implied_rate * 100:.3f}%"
        gap = "ref" if run.mode == "reference" else f"{(fwd.forward or 0.0) - ref_f:+.2f}"
        print(
            f"| {run.label} | {fmt(fwd.forward or 0.0, 2)} | {gap} | "
            f"{fmt(fwd.discount or 0.0)} | {rate} | "
            f"{'yes' if fwd.trusted else '**no**'} | {fwd.n_pairs} | {len(run.ivs)} |"
        )


def report_iv(name: str, reference: MethodRun, others: list[MethodRun]) -> None:
    print(f"\n### {name} — implied volatility, in vol points\n")
    print("| method | attribution | n | median | p95 | worst | median rel | p95 rel |")
    print("|---|---|---|---|---|---|---|---|")
    for run in others:
        dev = deviation(reference.ivs, run.ivs, floor=1e-4)
        if dev is None:
            continue
        print(
            f"| {run.label} | {run.mode} | {dev.n} | {dev.median * 100:.4f} | "
            f"{dev.p95 * 100:.4f} | {dev.worst * 100:.4f} | "
            f"{dev.median_relative:.3f}% | {dev.p95_relative:.3f}% |"
        )


def report_greek_table(name: str, reference: MethodRun, others: list[MethodRun]) -> None:
    print(f"\n### {name} — the five Greeks\n")
    print("| method | attribution | greek | n | median | p95 | worst "
          "| median rel | p95 rel |")
    print("|---|---|---|---|---|---|---|---|---|")
    for run in others:
        for greek in GREEKS:
            dev = deviation(
                column(reference.greeks, greek),
                column(run.greeks, greek),
                floor=RELATIVE_FLOOR[greek],
            )
            if dev is None:
                continue
            places = 9 if greek == "gamma" else 6
            print(
                f"| {run.label} | {run.mode} | {greek} | {dev.n} | "
                f"{fmt(dev.median, places)} | {fmt(dev.p95, places)} | "
                f"{fmt(dev.worst, places)} | {dev.median_relative:.3f}% | "
                f"{dev.p95_relative:.3f}% |"
            )


def report_timing(name: str, reference: MethodRun, others: list[MethodRun]) -> None:
    print(f"\n### {name} — time taken, milliseconds\n")
    print("| method | forward median | forward p95 | IV median | IV p95 | "
          "greeks median | greeks p95 | total median |")
    print("|---|---|---|---|---|---|---|---|")
    for run in [reference] + [r for r in others if r.mode != "forward-only"]:
        t = run.timings
        total = t["forward"].median_ms + t["iv"].median_ms + t["greeks"].median_ms
        print(
            f"| {run.label} | {t['forward'].median_ms:.4f} | {t['forward'].p95_ms:.4f} | "
            f"{t['iv'].median_ms:.4f} | {t['iv'].p95_ms:.4f} | "
            f"{t['greeks'].median_ms:.4f} | {t['greeks'].p95_ms:.4f} | {total:.4f} |"
        )


def report_bands(name: str, reference: MethodRun, others: list[MethodRun]) -> None:
    """Delta error by moneyness. A wing-concentrated error is a different fact."""
    print(f"\n### {name} — delta error by moneyness band, end-to-end\n")
    header = "| method | " + " | ".join(b[0] for b in MONEYNESS_BANDS) + " |"
    print(header)
    print("|---" * (len(MONEYNESS_BANDS) + 1) + "|")
    for run in others:
        if run.mode != "end-to-end":
            continue
        cells = []
        for band_name, _, _ in MONEYNESS_BANDS:
            ref = {
                leg: v["delta"]
                for leg, v in reference.greeks.items()
                if leg.band == band_name
            }
            got = {
                leg: v["delta"] for leg, v in run.greeks.items() if leg.band == band_name
            }
            dev = deviation(ref, got, floor=RELATIVE_FLOOR["delta"])
            cells.append("—" if dev is None else f"{dev.p95:.6f} (n={dev.n})")
        print(f"| {run.label} | " + " | ".join(cells) + " |")


def report_snapshot(name: str, chain, runs: int) -> None:
    days = days_to_expiry(chain)
    paired = sum(1 for row in chain.rows if row.call and row.put)
    print(f"\n\n## {name}")
    print(
        f"\n{len(chain.rows)} strikes, {paired} quoting both sides, "
        f"spot {chain.spot}, T = {days:.3f} days, snapshot {chain.fetched_at}."
    )
    reference, others = study(chain, runs)
    if not others:
        print("\nNo method other than the reference produced a trusted forward here.")
        return
    report_forwards(name, reference, others)
    report_iv(name, reference, others)
    report_greek_table(name, reference, others)
    report_bands(name, reference, others)
    report_timing(name, reference, others)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=500, help="timing runs per phase")
    parser.add_argument("--live", action="store_true", help="also measure the live chain")
    parser.add_argument("--expiries", type=int, default=0, help="cap expiries per source")
    args = parser.parse_args()

    sources = [
        ("fixture: 04-09-2026 chain", single_expiry_chains()),
        ("fixture: multi-expiry", multi_expiry_chains()),
    ]
    if args.live:
        # The fixtures are the study; the live capture is a corroboration. Delta's edge
        # is reachable intermittently from some networks, and losing eight expiries of
        # reproducible measurement to one TLS timeout would be the wrong trade. The
        # failure is printed rather than swallowed, so a missing live section is always
        # visible as a refusal and never as an omission.
        try:
            sources.append(("live", live_chains()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"\n> **Live capture skipped**: {exc}. Fixture results follow.")

    for source_name, chains in sources:
        items = list(chains.items())
        if args.expiries:
            items = items[: args.expiries]
        for expiry, chain in items:
            report_snapshot(f"{source_name} — {expiry}", chain, args.runs)


if __name__ == "__main__":
    main()
