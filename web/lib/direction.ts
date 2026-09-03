/**
 * Which way a price moved since the last push, per leg and per side of the book.
 *
 * **This compares pushes, not ticks.** The engine recomputes every 100 ms but pushes
 * once a second, so the browser sees roughly every tenth state. If a bid goes
 * 100 → 105 → 98 → 103 inside one second, this reports **up** — which is true of the
 * endpoints and says nothing about the 105 or the 98 in between.
 *
 * That is fine for a screen and wrong for anything that counts. Measured previously on
 * this project: thirty seconds on the at-the-money call produced 40 distinct quotes and
 * the screen showed 30. Nobody should ever compute an uptick/downtick ratio from what
 * this returns; that question is answered by subscribing to the engine's message bus,
 * where nothing is dropped.
 *
 * Kept as a pure function over two plain snapshots so it can be reasoned about without
 * React. There is no test runner in this workspace, which is exactly why it is this
 * small.
 */

import type { ChainResponse } from "@/lib/contract";

export type Direction = "up" | "down" | null;

/** `symbol|bid` and `symbol|ask` to the last value seen for it. */
export type PriceMemory = Map<string, number>;

/** The two quote fields that carry a direction arrow. Mark and IV deliberately do not. */
const SIDES = ["bid", "ask"] as const;

export type QuoteSide = (typeof SIDES)[number];

function key(symbol: string, side: QuoteSide): string {
  return `${symbol}|${side}`;
}

/**
 * Every price on the chain, flattened to the keys `directionOf` reads.
 *
 * Absent quotes are omitted rather than stored as zero: a strike that stops being quoted
 * has not moved to zero, and remembering it as such would flash a red arrow the moment
 * it comes back.
 */
export function priceMemoryOf(chain: ChainResponse): PriceMemory {
  const memory: PriceMemory = new Map();
  for (const row of chain.rows) {
    for (const leg of [row.call, row.put]) {
      if (leg === null) continue;
      for (const side of SIDES) {
        const value = leg[side];
        if (value !== null) memory.set(key(leg.symbol, side), value);
      }
    }
  }
  return memory;
}

/**
 * `"up"`, `"down"`, or `null` when it did not move or was not previously seen.
 *
 * A first sighting is `null` rather than `"up"`. The first render would otherwise paint
 * the whole ladder green, which says nothing and looks like a market event.
 */
export function directionOf(
  previous: PriceMemory | null,
  symbol: string,
  side: QuoteSide,
  value: number | null,
): Direction {
  if (previous === null || value === null) return null;
  const before = previous.get(key(symbol, side));
  if (before === undefined || before === value) return null;
  return value > before ? "up" : "down";
}
