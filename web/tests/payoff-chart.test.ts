/**
 * P5: the chart's geometry, away from a browser.
 *
 * **Every expected value below is worked by hand from the strikes and the premiums**,
 * never by calling the function under test a second time. The worked strategy is one
 * short strangle, and the whole file is derived from these six lines:
 *
 *     Sell 1 × 74,000 put at 800     Sell 1 × 80,000 call at 600
 *     Credit received                  800 + 600 = 1,400
 *     Below 74,000                     1,400 − (74,000 − S)  =  S − 72,600
 *     Between the strikes              +1,400, flat
 *     Above 80,000                     1,400 − (S − 80,000)  =  81,400 − S
 *     Breakevens                       72,600 and 81,400
 *
 * The engine frames that on a window of 70,000–84,000, and `corner_prices` emits the
 * window ends alongside every strike, so the curve arrives as **four** corners — which
 * is exactly the shape the brief's acceptance criterion names.
 *
 * The rule this file exists to pin is the one it lifts from the sibling project's
 * `zoom.ts`: **the window is a suggestion, not a clamp.** The sibling samples 400 points
 * across a fixed span and has nothing outside them, so its window may never leave the
 * data. Corners plus two exact rays have data everywhere, so zooming out past the last
 * corner is not an error state — it is the reason the curve travels as corners at all.
 */
import assert from "node:assert/strict";

import type { Curve } from "@/lib/payoff";
import {
  PLOT_LEFT,
  PLOT_WIDTH,
  VIEWBOX_WIDTH,
  engineSpan,
  priceAtPointer,
  pnlAt,
  polyline,
  projectX,
  projectY,
  verticalSpan,
  visiblePoints,
  wheelFactor,
  zoomAbout,
} from "@/lib/payoffChart";

let failures = 0;

function check(name: string, body: () => void): void {
  try {
    body();
    console.log(`  ok    ${name}`);
  } catch (err) {
    failures++;
    console.error(`  FAIL  ${name}`);
    console.error(`        ${err instanceof Error ? err.message : String(err)}`);
  }
}

/** The short strangle of the header comment, framed 70,000–84,000. */
const STRANGLE: Curve = {
  corners: [
    // 1,400 − (74,000 − 70,000) = −2,600
    { price: 70000, pnl: -2600 },
    { price: 74000, pnl: 1400 },
    { price: 80000, pnl: 1400 },
    // 1,400 − (84,000 − 80,000) = −2,600
    { price: 84000, pnl: -2600 },
  ],
  slope_left: 1,
  slope_right: -1,
  window: { low: 70000, high: 84000 },
};

console.log("payoffChart / pnlAt — the two rays are exact wherever the reader drags");

check("a price on a corner reads that corner's own P&L", () => {
  assert.equal(pnlAt(STRANGLE, 74000), 1400);
  assert.equal(pnlAt(STRANGLE, 80000), 1400);
});

check("a price between two corners is on the straight line between them", () => {
  // Flat between the strikes: the whole credit, wherever it lands in there.
  assert.equal(pnlAt(STRANGLE, 77000), 1400);
  // 1,400 − (74,000 − 72,000) = −600
  assert.equal(pnlAt(STRANGLE, 72000), -600);
});

check("a price left of every corner follows slope_left, not the nearest corner", () => {
  // 1,400 − (74,000 − 69,000) = −3,600. Off the chart the engine framed, and exact.
  assert.equal(pnlAt(STRANGLE, 69000), -3600);
  // 1,400 − (74,000 − 60,000) = −12,600, ten thousand points outside the window.
  assert.equal(pnlAt(STRANGLE, 60000), -12600);
});

check("a price right of every corner follows slope_right", () => {
  // 1,400 − (90,000 − 80,000) = −8,600
  assert.equal(pnlAt(STRANGLE, 90000), -8600);
});

check("the breakevens the engine published are where the line actually crosses zero", () => {
  assert.equal(pnlAt(STRANGLE, 72600), 0);
  assert.equal(pnlAt(STRANGLE, 81400), 0);
});

console.log("\npayoffChart / visiblePoints — what is drawn through, at any zoom");

check("a window inside the corners keeps the corners it contains and cuts at its edges", () => {
  const points = visiblePoints(STRANGLE, { min: 71000, max: 76000 });
  assert.deepEqual(points, [
    // 1,400 − (74,000 − 71,000) = −1,600
    { price: 71000, pnl: -1600 },
    { price: 74000, pnl: 1400 },
    { price: 76000, pnl: 1400 },
  ]);
});

check("THE LIFTED CONSTRAINT: a window far outside the data still draws, on the rays", () => {
  const points = visiblePoints(STRANGLE, { min: 60000, max: 100000 });
  assert.deepEqual(points, [
    { price: 60000, pnl: -12600 },
    { price: 70000, pnl: -2600 },
    { price: 74000, pnl: 1400 },
    { price: 80000, pnl: 1400 },
    { price: 84000, pnl: -2600 },
    // 1,400 − (100,000 − 80,000) = −18,600
    { price: 100000, pnl: -18600 },
  ]);
});

check("a window landing exactly on a corner does not draw that point twice", () => {
  const points = visiblePoints(STRANGLE, { min: 74000, max: 80000 });
  assert.deepEqual(points, [
    { price: 74000, pnl: 1400 },
    { price: 80000, pnl: 1400 },
  ]);
});

check("no coordinate is ever non-finite — nothing an Infinity could reach the DOM through", () => {
  for (const span of [
    { min: 0, max: 1e9 },
    { min: 73999.9999, max: 74000.0001 },
  ]) {
    for (const point of visiblePoints(STRANGLE, span)) {
      assert.ok(Number.isFinite(point.price), `price ${point.price}`);
      assert.ok(Number.isFinite(point.pnl), `pnl ${point.pnl}`);
    }
  }
});

console.log("\npayoffChart / verticalSpan — the axis the line is read against");

check("the span reaches every P&L on screen, keeps zero, and adds eight percent of air", () => {
  // The engine's own window: P&L runs −2,600 to +1,400. Zero is already inside.
  // Height 4,000, so the pad is 320 at each end: −2,920 to 1,720.
  const span = verticalSpan(visiblePoints(STRANGLE, { min: 70000, max: 84000 }));
  assert.deepEqual(span, { min: -2920, max: 1720 });
});

check("zero stays in the span even when the whole window is in profit", () => {
  // 74,000–80,000 is flat at +1,400 and never touches zero. Height 1,400, pad 112.
  const span = verticalSpan(visiblePoints(STRANGLE, { min: 74000, max: 80000 }));
  assert.deepEqual(span, { min: -112, max: 1512 });
});

console.log("\npayoffChart / zoomAbout and wheelFactor — in and out, without a clamp");

check("zooming in halves the width and keeps the focus where it was", () => {
  // 70,000–84,000 is 14,000 wide; the focus is dead centre, so half the width is
  // 7,000 and it stays centred: 73,500–80,500.
  assert.deepEqual(zoomAbout({ min: 70000, max: 84000 }, 0.5, 77000), {
    min: 73500,
    max: 80500,
  });
});

check("a focus at the left edge stays at the left edge", () => {
  assert.deepEqual(zoomAbout({ min: 70000, max: 84000 }, 2, 70000), {
    min: 70000,
    max: 98000,
  });
});

check("THE POINT OF THE TICKET: zooming out four times is not clamped to the data", () => {
  // The sibling's `zoom` would slide this back inside the 400 sampled points. Here the
  // rays are exact, so 14,000 wide becomes 56,000 wide about the centre: 49,000–105,000.
  const wide = zoomAbout({ min: 70000, max: 84000 }, 4, 77000);
  assert.deepEqual(wide, { min: 49000, max: 105000 });
  assert.ok(wide.min < 70000 && wide.max > 84000, "the window has left the corners");
});

check("the wheel composes: two half gestures equal one whole one", () => {
  // The sibling's rule, `exp(delta · k)`, so the exponents add and a pinch delivered as
  // thirty events is the same zoom as one event of the same total delta.
  const halved = wheelFactor(50) * wheelFactor(50);
  assert.ok(Math.abs(halved - wheelFactor(100)) < 1e-12, `${halved}`);
});

check("one mouse notch is the sibling's measured 0.85, and the factor is capped", () => {
  assert.ok(Math.abs(wheelFactor(-100) - 0.85214) < 1e-4, String(wheelFactor(-100)));
  assert.equal(wheelFactor(0), 1);
  assert.equal(wheelFactor(100000), 4);
  assert.equal(wheelFactor(-100000), 0.25);
});

check("reset returns exactly the window the engine sent", () => {
  assert.deepEqual(engineSpan(STRANGLE.window), { min: 70000, max: 84000 });
});

console.log("\npayoffChart / polyline — points into an SVG path");

check("the span's ends land on the frame's ends, and up is up", () => {
  // A 100 × 50 frame over x 70,000–84,000 and y −2,600..1,400. The left corner is at
  // x = 0 and its P&L is the bottom of the span, so y = 50 (SVG counts down).
  const path = polyline(
    [
      { price: 70000, pnl: -2600 },
      { price: 77000, pnl: 1400 },
      { price: 84000, pnl: -2600 },
    ],
    { min: 70000, max: 84000 },
    { min: -2600, max: 1400 },
    100,
    50,
  );
  assert.equal(path, "0,50 50,0 100,50");
});

check("a degenerate span does not divide by zero into a NaN path", () => {
  const path = polyline(
    [{ price: 74000, pnl: 1400 }],
    { min: 74000, max: 74000 },
    { min: 1400, max: 1400 },
    100,
    50,
  );
  assert.doesNotMatch(path, /NaN|Infinity/);
});

console.log("\npayoffChart / projectX and projectY — where a marker stands");

check("the forward and a breakeven land where the arithmetic puts them", () => {
  // A 140-unit frame over 70,000-84,000 is one unit per 100 of price.
  // The forward at 77,609.4 is 7,609.4 above the left edge, so 76.094 units in.
  assert.equal(projectX(77609.4, { min: 70000, max: 84000 }, 140), 76.09);
  // The lower breakeven at 72,600 is 2,600 above the left edge: 26 units in.
  assert.equal(projectX(72600, { min: 70000, max: 84000 }, 140), 26);
});

check("a marker outside the window is not drawn at the frame edge — it is not drawn", () => {
  // Clamping would put the upper breakeven on the right-hand edge, where a reader
  // would take it for a real crossing at 84,000.
  assert.equal(projectX(90000, { min: 70000, max: 84000 }, 140), null);
  assert.equal(projectX(60000, { min: 70000, max: 84000 }, 140), null);
});

check("the zero line sits where zero is, and up is up", () => {
  // A 40-unit frame over -2,600..1,400 is one unit per 100 of P&L. Zero is 1,400
  // below the top, so 14 units down; the top of the span is 0 and the bottom is 40.
  assert.equal(projectY(0, { min: -2600, max: 1400 }, 40), 14);
  assert.equal(projectY(1400, { min: -2600, max: 1400 }, 40), 0);
  assert.equal(projectY(-2600, { min: -2600, max: 1400 }, 40), 40);
});

check("a P&L off the top of the span is not drawn", () => {
  assert.equal(projectY(5000, { min: -2600, max: 1400 }, 40), null);
});

/*
 * THE BUTTERFLY, and the hand-drawn expectation the brief asks to attach.
 *
 *     Buy  1 x 76,000 call at 1,600
 *     Sell 2 x 77,000 calls at 1,000
 *     Buy  1 x 78,000 call at 600
 *     Net debit    1,600 - 2,000 + 600 = 200 paid
 *
 *     At 76,000 and below   -200          (every call expires worthless)
 *     At 77,000             1,000 - 200 = 800
 *     At 78,000             2,000 - 2,000 - 200 = -200
 *     At 78,000 and above   -200          (the wings cap it: both slopes are zero)
 *     Breakevens            76,200 and 77,800
 *
 * On a 600 x 100 frame over 74,000-80,000 with the P&L axis pinned to -200..800, that
 * is a flat line at the bottom, one spike to the very top at the middle, and flat again:
 *
 *     y=0     . . . . . . . . . . . /\ . . . . . . . . . . .        800
 *                                  /  \
 *     y=100   ____________________/    \____________________       -200
 *             ^0                ^300                     ^600
 */
const BUTTERFLY: Curve = {
  corners: [
    { price: 74000, pnl: -200 },
    { price: 76000, pnl: -200 },
    { price: 77000, pnl: 800 },
    { price: 78000, pnl: -200 },
    { price: 80000, pnl: -200 },
  ],
  slope_left: 0,
  slope_right: 0,
  window: { low: 74000, high: 80000 },
};

console.log("\npayoffChart / a known butterfly, against a hand-drawn expectation");

check("at the engine's window the drawing is the one sketched above, point for point", () => {
  const path = polyline(
    visiblePoints(BUTTERFLY, engineSpan(BUTTERFLY.window)),
    engineSpan(BUTTERFLY.window),
    { min: -200, max: 800 },
    600,
    100,
  );
  assert.equal(path, "0,100 200,100 300,0 400,100 600,100");
});

check("zoomed out four times it is the same picture with a narrower spike", () => {
  // 6,000 wide about 77,000 becomes 24,000 wide: 65,000-89,000. The two rays are flat,
  // so both new edge points sit at -200 — the bottom of the frame — and the peak stays
  // dead centre. Worked by hand: 74,000 is 9,000 of 24,000 in, which is 225 of 600.
  const wide = zoomAbout(engineSpan(BUTTERFLY.window), 4, 77000);
  assert.deepEqual(wide, { min: 65000, max: 89000 });
  const path = polyline(
    visiblePoints(BUTTERFLY, wide),
    wide,
    { min: -200, max: 800 },
    600,
    100,
  );
  assert.equal(path, "0,100 225,100 275,100 300,0 325,100 375,100 600,100");
});

check("both breakevens are prices the drawn line really crosses zero at", () => {
  assert.equal(pnlAt(BUTTERFLY, 76200), 0);
  assert.equal(pnlAt(BUTTERFLY, 77800), 0);
  // And a capped structure's rays are flat: the loss never deepens however far out.
  assert.equal(pnlAt(BUTTERFLY, 40000), -200);
  assert.equal(pnlAt(BUTTERFLY, 200000), -200);
});

console.log("\npayoffChart / the wall at zero — a price is a non-negative quantity");

check("a zoom that would reach below zero slides to zero instead of crossing it", () => {
  // 14,000 wide about the centre, twentyfold, is 280,000 wide: centred on 77,000 the
  // left edge would be 77,000 - 140,000 = -63,000, which is not a price anything can
  // finish at. Slid, not truncated: the width is the zoom the reader asked for.
  const wide = zoomAbout({ min: 70000, max: 84000 }, 20, 77000);
  assert.deepEqual(wide, { min: 0, max: 280000 });
  assert.equal(wide.max - wide.min, 280000, "the chosen width survives the wall");
});

check("THE WALL IS NOT A CAP: zooming out again from the wall still widens", () => {
  // The sibling cannot zoom out at all and this feature exists so that it can. The
  // wall bounds the axis's domain, not the reader's gesture — the window keeps growing,
  // it simply grows to the right.
  const once = zoomAbout({ min: 70000, max: 84000 }, 20, 77000);
  const twice = zoomAbout(once, 2, 140000);
  assert.deepEqual(twice, { min: 0, max: 560000 });
  assert.ok(twice.max - twice.min > once.max - once.min, "still widening");
});

check("no sequence of zooms puts a negative price on the axis", () => {
  let span = { min: 70000, max: 84000 };
  for (const factor of [4, 4, 4, 0.5, 8, 2]) {
    span = zoomAbout(span, factor, span.min);
    assert.ok(span.min >= 0, `left edge ${span.min}`);
    assert.ok(span.max > span.min, "the window still has width");
  }
});

check("a window nowhere near zero is untouched — the focus still stays put", () => {
  assert.deepEqual(zoomAbout({ min: 70000, max: 84000 }, 0.5, 77000), {
    min: 73500,
    max: 80500,
  });
});

console.log("\npayoffChart / priceAtPointer — what the wheel is actually zooming about");

/*
 * The mapping the component used to do by hand, and got wrong.
 *
 * The `<svg>` element is `VIEWBOX_WIDTH` units wide; the plot inside it is translated
 * right by `PLOT_LEFT` and is only `PLOT_WIDTH` wide. Treating the fraction across the
 * *element* as the fraction across the *plot* displaces the focus right by
 * `PLOT_LEFT / VIEWBOX_WIDTH` of the window — 7.05% — and the error compounds across a
 * gesture, because each event zooms about a price further right than the one under the
 * pointer. The whole reason the wheel listener is registered by hand with
 * `{ passive: false }` is that the price under the pointer stays put, so this is the one
 * piece of arithmetic on which that justification rests.
 *
 * `renderToStaticMarkup` never runs an effect, so the wheel path has no DOM coverage by
 * construction. Moving the mapping here is what makes it checkable at all.
 */
check("the plot's left edge is the window's left edge, not seven percent inside it", () => {
  // The element rendered at 1 CSS pixel per viewBox unit, flush against the viewport.
  // SVG x = 68 is where the plot starts, so it is 70,000 — the old arithmetic called it
  // 70,000 + (68/964)·14,000 = 70,987.
  assert.equal(priceAtPointer({ min: 70000, max: 84000 }, PLOT_LEFT, 0, VIEWBOX_WIDTH), 70000);
});

check("the plot's right edge is the window's right edge", () => {
  assert.equal(
    priceAtPointer({ min: 70000, max: 84000 }, PLOT_LEFT + PLOT_WIDTH, 0, VIEWBOX_WIDTH),
    84000,
  );
});

check("the middle of the plot is the middle of the window", () => {
  // 68 + 440 = 508 in viewBox units.
  assert.equal(
    priceAtPointer({ min: 70000, max: 84000 }, PLOT_LEFT + PLOT_WIDTH / 2, 0, VIEWBOX_WIDTH),
    77000,
  );
});

check("it holds at any rendered width, because the viewBox scales and the pointer does not", () => {
  // Half size: 482 CSS pixels for 964 units, so the plot's left edge is at 34.
  assert.equal(priceAtPointer({ min: 70000, max: 84000 }, 34, 0, VIEWBOX_WIDTH / 2), 70000);
  // And offset from the viewport's left edge, which is what `getBoundingClientRect` gives.
  assert.equal(
    priceAtPointer({ min: 70000, max: 84000 }, 200 + PLOT_LEFT, 200, VIEWBOX_WIDTH),
    70000,
  );
});

check("a pointer in the axis gutter or the right margin clamps to the plot's own edges", () => {
  // The label gutter is not part of the window; zooming about a price outside it would
  // move the curve out from under the pointer in the other direction.
  assert.equal(priceAtPointer({ min: 70000, max: 84000 }, 10, 0, VIEWBOX_WIDTH), 70000);
  assert.equal(priceAtPointer({ min: 70000, max: 84000 }, 962, 0, VIEWBOX_WIDTH), 84000);
});

check("an element with no width yet zooms about the centre rather than dividing by zero", () => {
  assert.equal(priceAtPointer({ min: 70000, max: 84000 }, 0, 0, 0), 77000);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
