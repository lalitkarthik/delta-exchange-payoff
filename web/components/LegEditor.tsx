"use client";

import { useState } from "react";

import {
  type Commit,
  appendLeg,
  commitEntryPrice,
  commitInstrument,
  commitQuantity,
  removeLeg,
  toggleDirection,
  withQuantity,
} from "@/lib/leg-edit";
import type { LegRequest } from "@/lib/payoff";

/**
 * P6: the strategy, editable in place — contract, quantity and entry price.
 *
 * This is the panel `LegsPanel` on the chain screen deliberately is not. Its own header
 * comment says why: the ladder's B/S buttons are that screen's way of changing a leg,
 * and "fully editable — contract, quantity, entry price" is the analyse tab's job. This
 * is that job, and the two panels stay apart rather than one growing a `readOnly` flag.
 *
 * **Editing the entry price is what makes an unquoted leg recoverable.** A leg with
 * nothing on its side and no `entry_price` refuses the whole analysis
 * (`docs/payoff-contract.md` §Refusals, last row) — "a leg nobody is quoting is not
 * disabled, it is asked about" — so this panel is rendered whether or not there is an
 * analysis beside it. A refusal that replaced these inputs would put the question on
 * screen and take away the only way to answer it.
 *
 * **The inputs are uncontrolled, and that is deliberate.** A controlled field whose
 * value is the leg cannot be typed into: `withEntryPrice` reads `"0."` as `0` and would
 * re-render the box as `0`, eating the decimal point on the way to `0.5`, and a cleared
 * quantity box would snap straight back to what it held. The DOM keeps the text, this
 * component keeps only the legs, and `key` — instrument, direction and position — is
 * what re-seeds a row when the leg under it genuinely changes, which is exactly when a
 * leg is removed and the rows below it shift up.
 *
 * **A refused edit does not stay in the field.** `commitQuantity` and its two siblings
 * return the strategy *and* the text the field must now show; when that differs from what
 * was typed, `reseed` bumps a counter in every row's `key` and React remounts the row,
 * which re-reads `defaultValue` from the leg. Without it the rejection is silent: the box
 * keeps `0` while the metrics, the curve and the URL all describe a quantity of 1, which
 * is this feature's characteristic failure — never a wrong number, always a plausible
 * screen. The re-seed is on **blur**, not on every keystroke, so a field being cleared on
 * the way to a new value is not fought halfway through.
 *
 * **Quantity commits as you type; the contract and the price commit on leaving the
 * field or on Enter.** Every commit re-asks `POST /analyse`, and a half-typed strike is
 * a different contract while a half-typed price is a different price — `9`, `90`, `900`
 * would each be sent and two of them are fills nobody got. A quantity has no meaningful
 * half-typed state: every prefix of an integer is an integer.
 *
 * **`direction` is a toggle, and flipping it drops the entry price.** B and S are
 * separate positions rather than one signed quantity, so a flip that kept the price would
 * quietly claim a fill on the other side of the spread; without one the engine re-prices
 * the leg off the book.
 *
 * **The address bar is debounced and the request is not, deliberately.** Typing `1234`
 * into the quantity box commits four times and issues four `POST /analyse`. The 200 ms
 * settle on the URL exists because Firefox and Safari throttle `replaceState` itself —
 * it is a browser-level cost that a burst can actually hit. A request is not that: an
 * analysis measured **3.3 ms** median on a four-leg strategy against the 69-strike
 * 04-09-2026 capture (P6, 2026-09-10), each abandoned subscription is stopped so its
 * answer is dropped rather than racing the new one, and a debounce would put a delay
 * between a keystroke and the metrics it is being typed to see. Cost measured, latency
 * chosen; if the cost ever matters, `docs/superpowers` P6's report names compression as
 * the first move.
 */
export default function LegEditor({
  legs,
  onLegsChange,
}: {
  legs: LegRequest[];
  onLegsChange: (legs: LegRequest[]) => void;
}) {
  /** Bumped whenever a field has to be re-seeded from the leg. It is in every row's
   *  `key`, which is what makes React remount the row and re-read `defaultValue` — the
   *  only lever an uncontrolled input offers. One counter for the whole panel rather than
   *  one per row: a re-seed happens on blur, so no other field is being typed into. */
  const [reseeds, setReseeds] = useState(0);

  /** Apply a commit, and put the value in force back in the box when the typed text was
   *  not what the strategy accepted. */
  const commit = (commited: Commit, typed: string) => {
    onLegsChange(commited.legs);
    if (commited.text !== typed) setReseeds((n) => n + 1);
  };

  return (
    <section className="legs-panel leg-editor" aria-label="Strategy">
      <h2 className="legs-panel-title">
        Legs — {legs.length} {legs.length === 1 ? "leg" : "legs"}
      </h2>

      <ul className="legs-list">
        {legs.map((leg, index) => (
          <li
            key={`${leg.instrument}:${leg.direction}:${index}:${reseeds}`}
            className="leg-row leg-edit-row"
          >
            <button
              type="button"
              className={`leg-direction ${leg.direction === 1 ? "b" : "s"}`}
              aria-label={leg.direction === 1 ? "Bought — click to sell" : "Sold — click to buy"}
              title="Switch this leg between bought and sold. Any entry price is dropped: it was a fill on the other side of the spread."
              onClick={() => onLegsChange(toggleDirection(legs, index))}
            >
              {leg.direction === 1 ? "B" : "S"}
            </button>

            <input
              className="leg-input leg-contract"
              aria-label="Contract"
              title="The canonical instrument string. The engine is the authority on it and names the part that was wrong."
              defaultValue={leg.instrument}
              spellCheck={false}
              onBlur={(event) =>
                commit(commitInstrument(legs, index, event.target.value), event.target.value)
              }
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
            />

            <input
              className="leg-input leg-qty"
              aria-label="Quantity"
              title="How many of this contract. A positive integer; the sign is the B or S beside it."
              type="number"
              min={1}
              step={1}
              defaultValue={leg.quantity}
              onChange={(event) => onLegsChange(withQuantity(legs, index, event.target.value))}
              onBlur={(event) =>
                commit(commitQuantity(legs, index, event.target.value), event.target.value)
              }
            />

            <input
              className="leg-input leg-price"
              aria-label="Entry price"
              title="What you were filled at, USD per one unit of the underlying. Leave it empty and the engine crosses the spread — buy at the ask, sell at the bid."
              type="text"
              inputMode="decimal"
              placeholder="from the book"
              defaultValue={leg.entry_price ?? ""}
              onBlur={(event) =>
                commit(commitEntryPrice(legs, index, event.target.value), event.target.value)
              }
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
            />

            <button
              type="button"
              className="leg-remove"
              aria-label="Remove leg"
              title="Remove this leg from the strategy"
              onClick={() => onLegsChange(removeLeg(legs, index))}
            >
              ×
            </button>
          </li>
        ))}
      </ul>

      <button
        type="button"
        className="leg-add"
        disabled={legs.length === 0}
        title="Copy the last leg at one lot, then edit its contract."
        onClick={() => onLegsChange(appendLeg(legs))}
      >
        + Add leg
      </button>

      <p className="leg-editor-note">
        An empty price is filled from the book by crossing the spread. Everything you
        change here is written into the address bar, so the link stays the strategy on
        screen.
      </p>
    </section>
  );
}
