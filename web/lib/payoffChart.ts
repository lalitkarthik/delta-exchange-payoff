/**
 * The payoff chart's geometry: a window, what is visible through it, and where that
 * lands on an SVG frame.
 *
 * All of it pure, and separate from the component for `lib/scale.ts`'s reason — this is
 * the part of a chart worth reasoning about without a browser, and the part whose
 * mistakes draw plausibly rather than throwing.
 *
 * **Nothing here solves anything.** `docs/payoff-contract.md` sends the curve as its
 * corner points plus the two end slopes, which is an exact description of a piecewise
 * linear line everywhere on the real number line. Reading a value off a straight segment
 * between two given points, or off a ray of a given slope from a given point, is
 * rendering — the same rendering the web app's own rule already allows — not the option
 * arithmetic the engine owns.
 *
 * ## Why this is not `convex-hedge-payoff/web/lib/zoom.ts`
 *
 * That file is the prior art and this one deliberately drops its central rule. Its
 * `zoom` and `contain` both slide the window back inside the data, because there the
 * curve is 400 samples across the forward ±6% and a window that escaped would render as
 * empty axis — a broken chart rather than the end of the data. **That constraint is what
 * this ticket exists to lift.** With corners and two exact rays there is a P&L at every
 * price, so a window outside the corners is still a picture of the strategy, and the
 * reader may zoom out as far as they like. What is kept from that file is the part that
 * was about people rather than about sampling: the focus point stays put, which is the
 * whole difference between zooming and scrolling, and the wheel factor is exponential in
 * the delta so a trackpad pinch and a mouse notch compose the same way.
 */
import type { Curve, PayoffPoint, Window } from "./payoff";

/** A range of prices, in USD per one unit of the underlying. */
export interface Span {
  min: number;
  max: number;
}

/**
 * The narrowest and widest a window may get.
 *
 * Neither is a rule about the data — they are float sanity, and they are the only two
 * limits in this file. A window narrower than a cent cannot be told from a point at
 * these five-figure prices, and one wider than `1e12` runs out of the precision that
 * keeps `polyline`'s output finite. Between them the reader is unconstrained, which is
 * the promise the corner-point curve makes.
 */
const MIN_WIDTH = 0.01;
const MAX_WIDTH = 1e12;

/*
 * The frame, in SVG units. The `viewBox` scales these to whatever width the chart is
 * given, so they are proportions rather than pixels.
 *
 * **They live here rather than in the component because they are geometry**, and the one
 * thing that needs them — turning a pointer position into a price — was the last piece of
 * arithmetic left in the component and therefore the only piece with no test.
 * `renderToStaticMarkup` never runs an effect, so a wheel handler cannot be covered from
 * a DOM fingerprint by construction; hand-checking `priceAtPointer` is the only kind of
 * check this environment offers, and it only becomes possible once the layout is a value
 * rather than a constant private to the markup.
 */
export const PLOT_LEFT = 68;
export const PLOT_TOP = 14;
export const PLOT_RIGHT = 16;
export const PLOT_BOTTOM = 26;
export const PLOT_WIDTH = 880;
export const PLOT_HEIGHT = 320;
export const VIEWBOX_WIDTH = PLOT_LEFT + PLOT_WIDTH + PLOT_RIGHT;
export const VIEWBOX_HEIGHT = PLOT_TOP + PLOT_HEIGHT + PLOT_BOTTOM;

/**
 * The price under the pointer — what a wheel event zooms about.
 *
 * Three coordinate systems meet here and getting them confused is silent: the pointer
 * arrives in **client pixels**, the element is measured in **CSS pixels** by
 * `getBoundingClientRect`, and the plot is laid out in **viewBox units** and is inset
 * from the element by `PLOT_LEFT` on one side and `PLOT_RIGHT` on the other. Reading the
 * fraction across the *element* as the fraction across the *plot* displaces the focus
 * right by `PLOT_LEFT / VIEWBOX_WIDTH` — 7.05% of the window — and because each event
 * then zooms about a price to the right of the one being pointed at, **the error
 * compounds across a gesture** rather than staying a fixed offset.
 *
 * That matters more than a small displacement would, because "the price under the
 * pointer stays put" is the entire justification for this chart hand-registering a
 * non-passive wheel listener at all.
 *
 * The gutter where the axis labels live is **clamped to the plot's edges** rather than
 * extrapolated past them: it is not part of the window, and zooming about a price
 * outside the window would move the curve out from under the pointer in the other
 * direction.
 */
export function priceAtPointer(
  span: Span,
  clientX: number,
  boxLeft: number,
  boxWidth: number,
): number {
  // Before layout, or on a hidden element. The centre is the only honest answer, and it
  // is what the +/- buttons already zoom about.
  if (!(boxWidth > 0)) return span.min + (span.max - span.min) / 2;
  const units = ((clientX - boxLeft) / boxWidth) * VIEWBOX_WIDTH;
  const plotX = Math.min(PLOT_WIDTH, Math.max(0, units - PLOT_LEFT));
  return span.min + (plotX / PLOT_WIDTH) * (span.max - span.min);
}

/** The window the engine suggested opening on, which is what Reset returns to. */
export function engineSpan(window: Window): Span {
  return { min: window.low, max: window.high };
}

/**
 * The strategy's P&L if the underlying finishes at `price` — exact at every price, not
 * only inside the frame the engine happened to suggest.
 *
 * Three cases, and the two rays are the interesting ones: outside the first and last
 * corner the line continues at `slope_left` and `slope_right`, which is what makes
 * zooming out free rather than a guess. A corner list is never empty
 * (`docs/payoff-contract.md`: "at least one corner"); the guard is there so a breaching
 * response cannot take the whole screen down with it.
 */
export function pnlAt(curve: Curve, price: number): number {
  const corners = curve.corners;
  const first = corners[0];
  const last = corners[corners.length - 1];
  if (first === undefined || last === undefined) return 0;

  if (price <= first.price) return first.pnl + curve.slope_left * (price - first.price);
  if (price >= last.price) return last.pnl + curve.slope_right * (price - last.price);

  for (let i = 1; i < corners.length; i++) {
    const right = corners[i]!;
    if (price > right.price) continue;
    const left = corners[i - 1]!;
    const width = right.price - left.price;
    // Two corners at one price would be a breach of "strictly ascending"; take the
    // right-hand one rather than dividing by zero.
    if (width <= 0) return right.pnl;
    return left.pnl + ((right.pnl - left.pnl) * (price - left.price)) / width;
  }
  return last.pnl;
}

/**
 * The points the line is drawn through for one window: both edges, and every corner
 * between them.
 *
 * That is the whole curve inside the window and nothing outside it — exact, because
 * every piece between two of these points is straight by construction. A corner sitting
 * exactly on an edge is not repeated: the edge point already carries it.
 */
export function visiblePoints(curve: Curve, span: Span): PayoffPoint[] {
  if (!(span.max > span.min)) return [{ price: span.min, pnl: pnlAt(curve, span.min) }];
  const inside = curve.corners.filter(
    (corner) => corner.price > span.min && corner.price < span.max,
  );
  return [
    { price: span.min, pnl: pnlAt(curve, span.min) },
    ...inside,
    { price: span.max, pnl: pnlAt(curve, span.max) },
  ];
}

/** Eight percent of the height at each end, so the line does not touch the frame. */
const AIR = 0.08;

/**
 * The vertical extent of whatever the window contains, with a little air.
 *
 * **Zero is always inside it**, and that is not decoration: zero is the line every
 * figure on this screen is quoted against, and a window that excluded it would show a
 * profit curve with no reference for what "profit" is measured from. The cost is real —
 * zoom into a region entirely in profit and the axis still reaches down to zero — and it
 * is the right trade on a payoff chart. The sibling's `fit` makes the same call, there
 * for an additional reason this chart does not have (its area fills baseline to zero).
 */
export function verticalSpan(points: PayoffPoint[]): Span {
  const values = points.map((point) => point.pnl).filter((pnl) => Number.isFinite(pnl));
  const low = Math.min(0, ...values);
  const high = Math.max(0, ...values);
  // A flat curve at exactly zero has no height to take a percentage of.
  const pad = Math.max((high - low) * AIR, 1);
  return { min: low - pad, max: high + pad };
}

/**
 * Zoom about a focus point. Below 1 narrows, above 1 widens.
 *
 * **The focus keeps its place across the change** — the price under the pointer is still
 * under the pointer afterwards — which is the difference between zooming and scrolling.
 * Unlike the sibling's, the result is never slid back inside the *data*: see this file's
 * own header for why that rule does not survive the move to corner points.
 *
 * **The one wall is at zero, and it is not a limit on zooming.** A price is a
 * non-negative quantity, so the axis has a domain and a window reaching left of zero
 * would be drawing a region that cannot exist. The arithmetic behind the curve already
 * agrees: the engine's own `_extremes` treats price 0 as a vertex, which is exactly why
 * only the right-hand tail can ever be unbounded, and a chart that disagreed with that
 * would be showing something its own numbers deny.
 *
 * The window is **slid, not truncated** — the width is the zoom level the reader chose,
 * and narrowing it here would zoom them out by an amount they never asked for. So
 * zooming out at the wall keeps widening, to the right; the reader is never stopped.
 * That is the difference between this and the cap this file refuses to have: a reader
 * who has zoomed out past usefulness can zoom back in, and a reader stopped at a cap
 * cannot get past it.
 *
 * The two rules disagree at the wall — a focus close to zero cannot keep its place and
 * also stay right of it — and the wall wins, for the reason the sibling's `zoom` gives
 * for its own precedence: the alternative is drawing axis that means nothing.
 */
export function zoomAbout(span: Span, factor: number, focus: number): Span {
  const width = span.max - span.min;
  const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width * factor));
  const at = width > 0 ? (focus - span.min) / width : 0.5;
  const min = Math.max(0, focus - at * next);
  return { min, max: min + next };
}

/** `exp(-100 · k) = 0.85` — one mouse notch, the step the sibling measured. */
const WHEEL_K = 0.0016;

/**
 * How much to widen or narrow for one wheel or pinch event.
 *
 * **Exponential in the delta, so that it composes**, which is the sibling's own finding
 * and worth restating: a flat step per event is right for a mouse wheel and wrong for a
 * trackpad, which delivers one pinch as thirty events. With `exp(delta · k)` the
 * exponents add, so the same total delta is the same total zoom however it was chopped
 * up. Capped because `deltaMode` is not always pixels and a coarse driver can deliver
 * thousands in one event.
 */
export function wheelFactor(delta: number): number {
  return Math.min(4, Math.max(0.25, Math.exp(delta * WHEEL_K)));
}

/** Two decimals is a tenth of an SVG pixel; the rest is float residue in the markup. */
function round(value: number): number {
  return Number(value.toFixed(2));
}

/**
 * Points into the `x,y x,y` string an SVG `<polyline>` takes.
 *
 * The last step before the DOM, and therefore the one that has to guarantee **no
 * non-finite number is ever written into an attribute**: a `NaN` in a `points` list
 * silently drops the rest of the line, and an `Infinity` is the sentinel this whole
 * feature refuses. A degenerate span maps to the middle of the frame rather than
 * dividing by zero.
 */
export function polyline(
  points: PayoffPoint[],
  xSpan: Span,
  ySpan: Span,
  width: number,
  height: number,
): string {
  const xRange = xSpan.max - xSpan.min;
  const yRange = ySpan.max - ySpan.min;
  return points
    .map((point) => {
      const x = xRange > 0 ? ((point.price - xSpan.min) / xRange) * width : width / 2;
      const y = yRange > 0 ? ((ySpan.max - point.pnl) / yRange) * height : height / 2;
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      return `${round(x)},${round(y)}`;
    })
    .filter((pair): pair is string => pair !== null)
    .join(" ");
}
/** Where one price falls across the frame, in SVG units. `null` when it is off-frame or
 * would not be a finite coordinate — a marker with nothing to stand on is not drawn. */
export function projectX(price: number, xSpan: Span, width: number): number | null {
  const xRange = xSpan.max - xSpan.min;
  if (!(xRange > 0)) return null;
  const x = ((price - xSpan.min) / xRange) * width;
  if (!Number.isFinite(x) || x < 0 || x > width) return null;
  return round(x);
}

/** Where one P&L falls across the frame, in SVG units. `null` off-frame, as `projectX`. */
export function projectY(pnl: number, ySpan: Span, height: number): number | null {
  const yRange = ySpan.max - ySpan.min;
  if (!(yRange > 0)) return null;
  const y = ((ySpan.max - pnl) / yRange) * height;
  if (!Number.isFinite(y) || y < 0 || y > height) return null;
  return round(y);
}
