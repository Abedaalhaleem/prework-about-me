/**
 * Lightweight canvas chart (no chart library).
 *
 * Every line is drawn from splitSeries(): null samples, reported gaps and
 * implausibly large steps END the line. Gap intervals are shaded with a
 * hatch so "no data" is visible, never smoothed over. A segment with a single
 * sample is drawn as a dot.
 */

import { useEffect, useRef } from 'react';
import type { GapInterval } from '../api/types';
import {
  clipGaps,
  extentOf,
  formatTick,
  linearScale,
  niceTicks,
  paddedExtent,
  splitSeries,
  tickStep,
  type Segment,
} from '../lib/plotting';

export interface PlotLine {
  label: string;
  color: string;
  x: readonly number[];
  y: readonly (number | null)[];
}

export interface PlotHLine {
  y: number;
  label: string;
  color: string;
}

export interface PlotBand {
  start: number;
  end: number;
  color: string;
}

export interface SignalPlotProps {
  title: string;
  caption?: string;
  xLabel: string;
  yLabel: string;
  xDomain: [number, number];
  lines: PlotLine[];
  gaps?: readonly GapInterval[];
  hLines?: PlotHLine[];
  bands?: PlotBand[];
  yDomain?: [number, number];
  /** Extra split threshold in x units (see autoMaxStep). */
  maxStep?: number;
  height?: number;
  emptyText?: string;
  legend?: boolean;
}

const THEME = {
  bg: '#0f151c',
  grid: 'rgba(148, 163, 184, 0.14)',
  axis: 'rgba(148, 163, 184, 0.55)',
  text: '#aab4c3',
  gapFill: 'rgba(148, 163, 184, 0.10)',
  gapHatch: 'rgba(148, 163, 184, 0.28)',
};

const PAD = { left: 56, right: 14, top: 10, bottom: 38 };
const BAND_H = 8;

function draw(canvas: HTMLCanvasElement, width: number, props: SignalPlotProps): { segments: number; points: number } {
  const height = props.height ?? 170;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  if (!ctx) return { segments: 0, points: 0 };
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = THEME.bg;
  ctx.fillRect(0, 0, width, height);

  const bandSpace = props.bands ? BAND_H + 4 : 0;
  const plotL = PAD.left;
  const plotR = width - PAD.right;
  const plotT = PAD.top;
  const plotB = height - PAD.bottom - bandSpace;
  if (plotR - plotL < 20 || plotB - plotT < 20) return { segments: 0, points: 0 };

  const [x0, x1] = props.xDomain;
  const inDomain = (s: Segment): Segment => s.filter((p) => p.x >= x0 && p.x <= x1);
  const all: { line: PlotLine; segs: Segment[] }[] = props.lines.map((line) => ({
    line,
    segs: splitSeries(line.x, line.y, { gaps: props.gaps, maxStep: props.maxStep })
      .map(inDomain)
      .filter((s) => s.length > 0),
  }));
  const segCount = all.reduce((n, l) => n + l.segs.length, 0);
  const pointCount = all.reduce((n, l) => n + l.segs.reduce((m, s) => m + s.length, 0), 0);

  const ext = props.yDomain ?? (() => {
    const e = extentOf(all.flatMap((l) => l.segs), (props.hLines ?? []).map((h) => h.y));
    return e ? paddedExtent(e, 0.08, 1) : ([0, 1] as [number, number]);
  })();
  const sx = linearScale(x0, x1, plotL, plotR);
  const sy = linearScale(ext[0], ext[1], plotB, plotT);

  // Gaps: shaded + hatched so missing data is visibly missing.
  const gaps = clipGaps(props.gaps ?? [], x0, x1);
  for (const g of gaps) {
    const gx0 = sx(g.start);
    const gx1 = Math.max(gx0 + 1, sx(g.end));
    ctx.fillStyle = THEME.gapFill;
    ctx.fillRect(gx0, plotT, gx1 - gx0, plotB - plotT);
    ctx.save();
    ctx.beginPath();
    ctx.rect(gx0, plotT, gx1 - gx0, plotB - plotT);
    ctx.clip();
    ctx.strokeStyle = THEME.gapHatch;
    ctx.lineWidth = 1;
    for (let hx = gx0 - (plotB - plotT); hx < gx1; hx += 7) {
      ctx.beginPath();
      ctx.moveTo(hx, plotB);
      ctx.lineTo(hx + (plotB - plotT), plotT);
      ctx.stroke();
    }
    ctx.restore();
  }

  // Grid + ticks
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
  ctx.fillStyle = THEME.text;
  ctx.strokeStyle = THEME.grid;
  ctx.lineWidth = 1;
  const yt = niceTicks(ext[0], ext[1], 4);
  const ys = tickStep(yt);
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const v of yt) {
    const y = Math.round(sy(v)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(plotL, y);
    ctx.lineTo(plotR, y);
    ctx.stroke();
    ctx.fillText(formatTick(v, ys), plotL - 6, y);
  }
  const xt = niceTicks(x0, x1, Math.max(2, Math.floor((plotR - plotL) / 70)));
  const xs = tickStep(xt);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const v of xt) {
    const x = Math.round(sx(v)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(x, plotT);
    ctx.lineTo(x, plotB);
    ctx.stroke();
    ctx.fillText(formatTick(v, xs), x, plotB + 4 + bandSpace);
  }
  ctx.strokeStyle = THEME.axis;
  ctx.strokeRect(plotL + 0.5, plotT + 0.5, plotR - plotL, plotB - plotT);

  // Axis labels (with units)
  ctx.fillStyle = THEME.text;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillText(props.xLabel, (plotL + plotR) / 2, height - 3);
  ctx.save();
  ctx.translate(12, (plotT + plotB) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textBaseline = 'middle';
  ctx.fillText(props.yLabel, 0, 0);
  ctx.restore();

  // Threshold lines
  for (const h of props.hLines ?? []) {
    if (h.y < ext[0] || h.y > ext[1]) continue;
    const y = Math.round(sy(h.y)) + 0.5;
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = h.color;
    ctx.lineWidth = 1.25;
    ctx.beginPath();
    ctx.moveTo(plotL, y);
    ctx.lineTo(plotR, y);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = h.color;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';
    ctx.fillText(h.label, plotR - 4, y - 2);
  }

  // Series
  ctx.save();
  ctx.beginPath();
  ctx.rect(plotL, plotT, plotR - plotL, plotB - plotT);
  ctx.clip();
  for (const { line, segs } of all) {
    ctx.strokeStyle = line.color;
    ctx.fillStyle = line.color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    for (const seg of segs) {
      const first = seg[0];
      if (!first) continue;
      if (seg.length === 1) {
        ctx.beginPath();
        ctx.arc(sx(first.x), sy(first.y), 2.2, 0, Math.PI * 2);
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      ctx.moveTo(sx(first.x), sy(first.y));
      for (let i = 1; i < seg.length; i++) {
        const p = seg[i] as { x: number; y: number };
        ctx.lineTo(sx(p.x), sy(p.y));
      }
      ctx.stroke();
    }
  }
  ctx.restore();

  // State band under the plot
  if (props.bands) {
    const by = plotB + 3;
    for (const b of props.bands) {
      const bx0 = Math.max(plotL, sx(Math.max(b.start, x0)));
      const bx1 = Math.min(plotR, sx(Math.min(b.end, x1)));
      if (bx1 < plotL || bx0 > plotR) continue;
      ctx.fillStyle = b.color;
      ctx.fillRect(bx0, by, Math.max(2, bx1 - bx0), BAND_H);
    }
  }
  return { segments: segCount, points: pointCount };
}

export function SignalPlot(props: SignalPlotProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const propsRef = useRef(props);
  propsRef.current = props;
  const summaryRef = useRef<HTMLSpanElement>(null);

  const redraw = (): void => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const width = Math.max(120, Math.floor(wrap.clientWidth));
    const res = draw(canvas, width, propsRef.current);
    if (summaryRef.current) {
      summaryRef.current.textContent =
        res.points === 0 ? 'no valid samples in view' : `${res.points} valid samples in ${res.segments} unbroken segment(s)`;
    }
  };

  useEffect(() => {
    redraw();
  });

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => redraw());
    ro.observe(wrap);
    return () => ro.disconnect();
    // redraw reads the latest props via a ref
  }, []);

  const hasData = props.lines.some((l) => l.y.some((v) => v !== null && Number.isFinite(v)));
  return (
    <figure className="plot">
      <figcaption className="plot__head">
        <span className="plot__title">{props.title}</span>
        <span className="plot__summary" ref={summaryRef} aria-live="off" />
      </figcaption>
      <div className="plot__canvas-wrap" ref={wrapRef}>
        <canvas ref={canvasRef} role="img" aria-label={`${props.title}. ${props.yLabel} versus ${props.xLabel}.`} />
        {!hasData && <div className="plot__empty">{props.emptyText ?? 'No measured samples'}</div>}
      </div>
      {props.legend && props.lines.length > 1 && (
        <ul className="plot__legend">
          {props.lines.map((l) => (
            <li key={l.label}>
              <span className="swatch" style={{ background: l.color }} /> {l.label}
            </li>
          ))}
        </ul>
      )}
      {props.caption && <p className="plot__caption">{props.caption}</p>}
    </figure>
  );
}
