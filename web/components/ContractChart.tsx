"use client";

import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
} from "lightweight-charts";

import type { ContractBar } from "@/lib/contract";
import {
  candleData,
  currentMinute,
  extendCandle,
  lineData,
  toUnix,
  type CandleMode,
  type OpenCandle,
} from "@/lib/contractChart";

/**
 * The candlestick series on mid or last-trade, bid and ask as lines over it, a
 * crosshair, both axes and zoom — TradingView's own `lightweight-charts`, pinned to
 * `5.2.1` in `package.json`. Candles, crosshairs and axes are solved problems; #46's
 * concept section is explicit that hand-drawing them would teach nothing this project is
 * here to learn, so this component is thin: it wires `lib/contractChart.ts`'s pure data
 * shaping to the library's imperative API and nothing else.
 *
 * **Colours are resolved from the theme's CSS custom properties, not hardcoded and not
 * the library's own defaults.** `lightweight-charts` draws on a `<canvas>`, and a canvas
 * fill or stroke colour does not resolve `var(--token)` the way an SVG or a DOM element's
 * `style` would — so `useChartColors` below reads the *computed* values off
 * `document.documentElement` and re-reads them whenever the theme attribute or the OS
 * preference changes, then pushes them into the chart with `applyOptions` rather than
 * tearing the chart down and rebuilding it on every toggle.
 */

interface ThemeColours {
  bg: string;
  grid: string;
  text: string;
  border: string;
  up: string;
  down: string;
  bid: string;
  ask: string;
}

/** Used only for the very first render before the effect below has read the DOM once —
 * never seen on screen, since the chart itself is not created until that effect runs. */
const FALLBACK: ThemeColours = {
  bg: "#100e0c",
  grid: "#221e1a",
  text: "#8f867a",
  border: "#322c26",
  up: "#4fb87c",
  down: "#e8636b",
  bid: "#6fb3d9",
  ask: "#4f93c4",
};

function readColours(): ThemeColours {
  const style = getComputedStyle(document.documentElement);
  const token = (name: string) => style.getPropertyValue(name).trim();
  return {
    bg: token("--surface"),
    grid: token("--line"),
    text: token("--ink-soft"),
    border: token("--line-strong"),
    up: token("--up"),
    down: token("--down"),
    // The bid/ask lines borrow the realised-vol chart's first two series hues
    // (`IvRvChart.tsx`) rather than `--call`/`--put`: those already mean "which side of
    // the ladder", and reusing them here would make a bid line on a put's chart read as
    // "this is a call".
    bid: token("--series-a"),
    ask: token("--series-b"),
  };
}

/** Tracks the resolved theme colours, updating on a `data-theme` change (the toggle) and
 * on a `prefers-color-scheme` change (the "auto" setting following the OS). Neither
 * `ThemeToggle.tsx` nor `theme.ts` dispatches an event on a change — the toggle only
 * writes a DOM attribute — so a `MutationObserver` is what stands in for one. */
function useChartColours(): ThemeColours {
  const [colours, setColours] = useState<ThemeColours>(FALLBACK);

  useEffect(() => {
    const update = () => setColours(readColours());
    update();

    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);

    return () => {
      observer.disconnect();
      media.removeEventListener("change", update);
    };
  }, []);

  return colours;
}

export interface LiveQuote {
  bid: number | null;
  ask: number | null;
}

/** The chart-wide options that carry a colour, built once from `colours` and used by
 * both the creation effect and the recolour effect below — the two used to write this
 * object out twice, which is exactly how a themed option added to one and not the
 * other would go unnoticed until the next toggle repainted the chart wrong. */
function chartColourOptions(colours: ThemeColours) {
  return {
    layout: {
      background: { type: ColorType.Solid, color: colours.bg },
      textColor: colours.text,
    },
    grid: { vertLines: { color: colours.grid }, horzLines: { color: colours.grid } },
    timeScale: { borderColor: colours.border },
    rightPriceScale: { borderColor: colours.border },
  };
}

function candleColourOptions(colours: ThemeColours) {
  return {
    upColor: colours.up,
    downColor: colours.down,
    borderUpColor: colours.up,
    borderDownColor: colours.down,
    wickUpColor: colours.up,
    wickDownColor: colours.down,
  };
}

export default function ContractChart({
  bars,
  mode,
  live,
}: {
  bars: ContractBar[];
  mode: CandleMode;
  /** The selected leg's current bid/ask off the live chain, or `null` while not
   * standing on the live edge — see `ContractPanel.tsx`. Only `mode === "mid"` is
   * followed live: there is no live last-traded-price field on `Leg`, only `mark`, and
   * drawing a live last-trade candle from a number that is not the last-traded price
   * would be exactly the "plausible and wrong" failure this project refuses elsewhere. */
  live: LiveQuote | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const bidRef = useRef<ISeriesApi<"Line"> | null>(null);
  const askRef = useRef<ISeriesApi<"Line"> | null>(null);
  const openRef = useRef<OpenCandle | null>(null);

  const colours = useChartColours();

  // Created once. Recoloured in place by the effect below, not rebuilt — rebuilding on
  // every theme toggle would drop the zoom and scroll position a reader had set.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      ...chartColourOptions(colours),
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: {
        ...chartColourOptions(colours).timeScale,
        timeVisible: true,
        secondsVisible: false,
      },
      autoSize: true,
    });

    const candle = chart.addSeries(CandlestickSeries, candleColourOptions(colours));
    const bid = chart.addSeries(LineSeries, {
      color: colours.bid,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    const ask = chart.addSeries(LineSeries, {
      color: colours.ask,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });

    chartRef.current = chart;
    candleRef.current = candle;
    bidRef.current = bid;
    askRef.current = ask;

    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      bidRef.current = null;
      askRef.current = null;
    };
    // `colours` deliberately excluded: the recolour effect below applies a changed
    // palette to the chart this effect already created, rather than this effect
    // recreating the chart every time the theme does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Recolour without rebuilding, from the same builders the creation effect used.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions(chartColourOptions(colours));
    candleRef.current?.applyOptions(candleColourOptions(colours));
    bidRef.current?.applyOptions({ color: colours.bid });
    askRef.current?.applyOptions({ color: colours.ask });
  }, [colours]);

  // The stored day, or a toggle between mid and last-trade. A fresh `setData` replaces
  // the whole series, so the live open candle being built on top of it is stale the
  // instant either changes — cleared here, rebuilt from the next live push.
  useEffect(() => {
    openRef.current = null;
    candleRef.current?.setData(candleData(bars, mode));
    bidRef.current?.setData(lineData(bars, "bid"));
    askRef.current?.setData(lineData(bars, "ask"));
    // Explicit rather than relied-upon: a contract whose book went quiet before the
    // stored day ends — a same-day expiry settling at a fixed time short of midnight,
    // observed against real recorded data while building this panel — leaves the last
    // series `setData` receiving mostly whitespace, and `lightweight-charts`' own
    // auto-fit is documented against the whole chart's content, not against whichever
    // series happened to call `setData` last. Fitting explicitly, every time the data
    // changes, is what keeps a toggle between two series of different length from
    // leaving the view zoomed into whichever one rendered first.
    chartRef.current?.timeScale().fitContent();
  }, [bars, mode]);

  // The live edge: the ladder's current quote for this leg folded into the two line
  // series always, and into the open candle only in mid mode — see `live`'s own doc.
  useEffect(() => {
    if (!live) return;
    const time = toUnix(currentMinute());
    if (live.bid !== null) bidRef.current?.update({ time, value: live.bid });
    if (live.ask !== null) askRef.current?.update({ time, value: live.ask });
    if (mode === "mid" && live.bid !== null && live.ask !== null) {
      const next = extendCandle(openRef.current, time, (live.bid + live.ask) / 2);
      openRef.current = next;
      candleRef.current?.update(next);
    }
  }, [live, mode]);

  return <div ref={containerRef} className="contract-chart" />;
}
