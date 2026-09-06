"use client";

import { OVERLAYS, type OverlayId, type OverlayState } from "@/lib/overlay";
import type { VolScale } from "@/lib/smilemodel";

/**
 * The chart's own controls: which comparison overlays are on, and which scale the
 * volatility axis is read on.
 *
 * On their own row above the plot rather than in the header: they change how the chart
 * is drawn and nothing about what the figures say. The overlay buttons carry their own
 * swatch, so the control *is* the key — a legend under the plot would be the same
 * information twice, in the place a reader looks last.
 */
export default function PlotControls({
  overlayOn,
  onToggleOverlay,
  scale,
  onPickScale,
}: {
  overlayOn: OverlayState;
  onToggleOverlay: (id: OverlayId) => void;
  scale: VolScale;
  onPickScale: (next: VolScale) => void;
}) {
  return (
    <div className="plot-controls">
      <span className="stat-label">Compare</span>
      <div className="overlay-toggle" role="group" aria-label="Comparison overlays">
        {OVERLAYS.map((spec) => (
          <button
            key={spec.id}
            type="button"
            className="overlay-option"
            data-overlay={spec.id}
            aria-pressed={overlayOn[spec.id]}
            title={spec.title}
            onClick={() => onToggleOverlay(spec.id)}
          >
            <span className="overlay-swatch" aria-hidden="true" />
            {spec.label}
          </button>
        ))}
      </div>

      <span className="plot-controls-gap" />

      <span className="stat-label">Vol axis</span>
      <div className="scale-toggle" role="group" aria-label="Volatility axis scale">
        {(["linear", "log"] as const).map((option) => (
          <button
            key={option}
            type="button"
            className="scale-option"
            aria-pressed={scale === option}
            onClick={() => onPickScale(option)}
          >
            {option === "linear" ? "LIN" : "LOG"}
          </button>
        ))}
      </div>
    </div>
  );
}
