import type { Metadata } from "next";

import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Volatility",
  description: "Implied volatility across strikes for one expiry — the smile.",
};

/**
 * The volatility screen, as a shell.
 *
 * There is no series on it and no chart in it. This route exists so that the change
 * that draws the smile is a change about drawing a smile, rather than one about routing,
 * navigation and a tab strip as well.
 *
 * **The second tab is present and disabled rather than absent.** IV vs RV is a real
 * screen that is deliberately not being built yet — it needs a realised-volatility
 * estimator and a daily bar series, and the store holds under ten hours of history. A
 * tab strip that gains an entry later shifts under a reader who has learned where the
 * first one sits; one that shows its final shape now does not. It carries a SOON badge
 * and a dotted rule rather than only a dimmer grey, because "disabled" and "broken"
 * look identical when the only difference is opacity — and because a grey faint enough
 * to read as unavailable at a glance is a grey nobody can read at all.
 *
 * A server component. Nothing here has state: the selected tab is the only tab that can
 * be selected, so there is no handler to write and no reason to ship the JavaScript for
 * one. The tabs become interactive in the change that gives the second one somewhere to
 * go.
 */
export default function VolatilityPage() {
  return (
    <div className="shell">
      <header className="header">
        <div className="brand">DELTA</div>
        <h1 className="screen-title">Volatility</h1>
        <ThemeToggle />
      </header>

      <main className="main">
        <div className="tabs" role="tablist" aria-label="Volatility views">
          <button type="button" role="tab" className="tab" aria-selected="true">
            Smile
          </button>

          {/* Native `disabled`, so "clicking it does nothing" is the browser's promise
              and not a handler that could be forgotten. `aria-disabled` alongside it for
              the screen readers that announce a tab's state from the ARIA attribute
              rather than from the DOM property. */}
          <button
            type="button"
            role="tab"
            className="tab"
            aria-selected="false"
            aria-disabled="true"
            disabled
          >
            IV vs RV <span className="tab-soon">soon</span>
          </button>
        </div>

        <section className="plot-empty" aria-label="Smile plot">
          <p>
            No plot yet.
            <br />
            This route is the screen shell — the implied-volatility smile is drawn in a
            later change.
          </p>
        </section>

        <p className="note">
          The smile plots one implied volatility per strike for a single expiry at a
          single minute, labelled by strike along the bottom and by offset from the
          forward along the top. Nothing on this screen is fitted or interpolated, and
          nothing is on it yet.
        </p>
      </main>
    </div>
  );
}
