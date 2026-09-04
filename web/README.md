# web

Next.js App Router front end for the option chain. Two screens, listed by a rail down
the left edge: the chain ladder at `/`, and the volatility screen at `/volatility`.

The volatility screen is a **shell**. It has a title, a tab strip whose second tab is
present and disabled, and an empty plot region; it holds no series and draws no chart.
It exists so that the change which draws the smile is about drawing a smile and not
about routing as well. `lib/screens.ts` is the one list of screens the rail renders —
adding a screen is an entry there and a directory under `app/`.

It renders `GET /chain` from the engine and **does no arithmetic**. The only number it
touches is IV, which it multiplies by 100 to display as a percentage — the contract
makes that the web app's job. Everything else is printed as it arrives.

The interface is [`docs/chain-contract.md`](../docs/chain-contract.md). That file is the
authority; `lib/contract.ts` mirrors it field for field.

## Install and run

Requires [bun](https://bun.com) (built with 1.3.14). Not npm, not pnpm.

```sh
bun install
bun run dev          # http://localhost:3000
```

Other scripts:

```sh
bun run build        # production build
bun run typecheck    # tsc --noEmit
bun run start        # serve the production build
```

## Pointing it at the engine

The base URL is `NEXT_PUBLIC_ENGINE_URL`, defaulting to `http://localhost:8000`.

```sh
# .env.local
NEXT_PUBLIC_ENGINE_URL=http://localhost:8000
```

It is read at build time, like any `NEXT_PUBLIC_` variable, so restart the dev server
after changing it.

## Running against the fixture

The engine does not have to exist. `lib/fixture.chain.json` holds one complete `/chain`
response and the app uses it in two situations:

1. **Automatically**, when the engine is unreachable — not running, wrong port, CORS.
   The page renders the fixture and says so, in an amber banner naming the URL it tried.
2. **Always**, when you force it:

   ```sh
   # .env.local
   NEXT_PUBLIC_USE_FIXTURE=1
   ```

   Nothing touches the network at all. Useful for working on the table itself.

A header cell always shows whether what you are looking at came from `engine` or
`fixture`, so a fixture is never mistaken for live market data.

An engine that *answers* is never overridden. A 400, 404 or 502 is a real reply, and its
`detail` is shown as an error — only an unreachable engine falls back.

### What the fixture covers, and what it does not

It holds **BTC, expiry 04-09-2026** and nothing else, so the app opens on that expiry when
it falls back. Choosing any other expiry, or switching to ETH, gives an honest "no fixture
for this" message rather than invented numbers. That is deliberate: a fabricated ETH chain
that looked real would be worse than a gap, and it gives the error path something to render.

The expiry dropdown itself is fully populated in fixture mode (eight BTC expiries, five for
ETH), so the control still works.

## The rendering rules

These are the ones that matter, and the ones a rewrite is most likely to get wrong.

**`null` is an empty cell, never `0` and never a dash.** A null bid means nobody is
bidding; printing `0.00` would claim someone bid zero, and a dash sitting in the column
where prices sit reads as one at a glance. All of it lives in `lib/format.ts`. This
follows the sibling chain in `payoff-project`, which renders `""` for a null Greek.

**A zero is a zero.** Open interest of exactly `0` is routine on this venue and is printed
as `0`. The empty cell and the zero must stay visually distinct, because they mean
opposite things. The fixture contains both next to each other on purpose.

**IV is a decimal fraction on the wire and a percentage on screen.** `0.3730` renders as
`37.30%`. The engine never multiplies by 100.

**Nothing calls `parseFloat`.** Every decimal is a JSON number by contract. If the engine
ever sends one as a string, `lib/engine.ts` raises a `ContractViolationError` naming the
offending fields instead of parsing them — the breach gets reported, not worked around.

**Either side of a row may be `null`.** The row still renders, with its strike, and the
absent side is **hatched** — five cells of 45° stripes with no text at all. Absence has
to look deliberate, and it must never look like a price. Rows are never dropped. This is
a different statement from a null field inside a quote, and gets a different mark.

**Data moves only on load and on Refresh.** There is no polling, no websocket, no
revalidation and no auto-refresh anywhere in this app.

## Layout notes

The appearance is **ported from the sibling chain screen** at
`payoff-project/web` — its palette, its density, its column geometry and most of its
stylesheet comments. The two projects are meant to look like one product, so a change
here that is not also a change there is a divergence, not an improvement.

Calls read outward-in from the left, puts inward-out to the right, so the tradeable
prices sit nearest the strike and the position-sized figures sit at the edges:

```
OI  Δ  IV  Bid  Ask  |  STRIKE  |  Ask  Bid  IV  Δ  OI
```

- **IV is per side, and that is the one deliberate difference from the sibling.** The
  sibling has a single shared IV column because it *solves* one volatility per strike.
  Delta publishes `mark_iv` for the call and the put separately and they genuinely
  differ — 28.19% against 27.58% on the at-the-money strike of a live BTC chain — so
  collapsing them would invent a number the venue never sent. `ChainLadder.tsx` says so.
- **Mark price is not a column.** It is in the cell tooltip with all three IVs, because
  a trader deals at the bid and the ask. Deep in-the-money calls report a floored
  `bid_iv` of `0.000005`, which shows there as `0.0005%` — a real value the venue
  publishes, not a rendering fault.
- The ATM row (`atm_strike`) carries the yellow `--atm` wash, an `--atm-line` inset ring
  and a **★** on the strike. The table scrolls it into view on mount, once — not on
  Refresh, which would yank the view out from under someone reading a wing.
- The in-the-money half carries the `--itm` wash, measured against **spot**. The sibling
  measures against its fitted Forward and says so; Delta publishes no forward and the
  contract exposes none, so spot is the only honest reference here — and it is the same
  one `atm_strike` uses, so the star and the wash agree.
- Light is the default. The theme toggle cycles Auto → Light → Dark and stores the choice
  under the same key the sibling uses, so a trader who sets it on one screen finds the
  other already wearing it. **Auto is the absence of `data-theme`**, not
  `data-theme="auto"` — the dark palette hangs off
  `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])`, so any attribute
  left in place would override the system instead of deferring to it.
- The table header stays put while the ladder scrolls: two sticky rows inside `.main`,
  which is the scroll box the shell grid creates.

## Files

```
app/page.tsx             the chain screen: header figures, pickers, the live ladder
app/volatility/page.tsx  the volatility screen, as a shell: title, tabs, empty plot
app/layout.tsx           document shell, the rail, and the anti-flash theme script
app/globals.css          all styling, plain CSS, no framework — ported from the sibling
components/ChainLadder.tsx   the ladder table
components/ThemeToggle.tsx   Auto / Light / Dark, ported from the sibling
components/ScreenRail.tsx    the rail, and which screen is current
lib/contract.ts          types mirroring docs/chain-contract.md and docs/smile-contract.md
lib/engine.ts            the only place that talks to the engine
lib/format.ts            the only place a number becomes text
lib/theme.ts             what a stored theme means; pure, no DOM
lib/screens.ts           which screens exist, and which one a path is on; pure
lib/fixture.ts           fixture loading, and what it covers
lib/fixture.chain.json   one committed /chain response
lib/fixture.smile.json   one committed /smile response, built from the chain above
```

`AGENTS.md` and `CLAUDE.md` are generated by `next dev` and re-added if deleted.
