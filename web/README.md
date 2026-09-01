# web

Next.js App Router front end for the option chain. One page, one ladder.

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

**`null` is an em dash `—`, never `0`.** A null bid means nobody is bidding; printing
`0.00` would claim someone bid zero. All the dash logic lives in `lib/format.ts`.

**A zero is a zero.** Open interest of exactly `0` is routine on this venue and is printed
as `0`. The dash and the zero must stay visually distinct, because they mean opposite
things. The fixture contains both next to each other on purpose.

**IV is a decimal fraction on the wire and a percentage on screen.** `0.3730` renders as
`37.30%`. The engine never multiplies by 100.

**Nothing calls `parseFloat`.** Every decimal is a JSON number by contract. If the engine
ever sends one as a string, `lib/engine.ts` raises a `ContractViolationError` naming the
offending fields instead of parsing them — the breach gets reported, not worked around.

**Either side of a row may be `null`.** The row still renders, with its strike, and the
absent side shown as dashes over a faint hatched background. Rows are never dropped.

**Data moves only on load and on Refresh.** There is no polling, no websocket, no
revalidation and no auto-refresh anywhere in this app.

## Layout notes

Calls left, strike centre, puts right. Both sides read in the same column order —
`Bid · Ask · Mark · IV · Δ · OI` — rather than mirroring the call side. Mirroring is
conventional on some terminals, but it makes the two sides hard to compare at a glance,
and everything here is read left to right.

- IV is the **mark** IV. Hovering a cell gives bid, mark and ask IV. Deep in-the-money
  calls report a floored `bid_iv` of `0.000005`, which shows there as `0.0005%` — a real
  value the venue publishes, not a rendering fault.
- The ATM row (`atm_strike`) is tinted, ruled top and bottom, and badged `ATM`.
- In-the-money cells carry a faint tint of their side's colour.
- The table header stays put while the ladder scrolls: `position: sticky` inside the
  `.ladder-scroll` box, which is what the header sticks to.

## Files

```
app/page.tsx             the page: controls, header facts, load and refresh
app/layout.tsx           document shell
app/globals.css          all styling, plain CSS, no framework
components/ChainLadder.tsx   the ladder table
lib/contract.ts          types mirroring docs/chain-contract.md
lib/engine.ts            the only place that talks to the engine
lib/format.ts            the only place a number becomes text
lib/fixture.ts           fixture loading, and what it covers
lib/fixture.chain.json   one committed /chain response
```

`AGENTS.md` and `CLAUDE.md` are generated by `next dev` and re-added if deleted.
