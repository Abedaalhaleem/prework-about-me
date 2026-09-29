/** Small controlled inputs used by the geometry editors. */

import { useEffect, useState } from 'react';
import type { Vec2 } from '../api/types';

/**
 * Number input that keeps the user's text while typing ("-", "1.") and
 * reports NaN for anything unparsable, so validation can flag it instead of
 * silently substituting 0.
 */
export function NumInput({
  value,
  onChange,
  step = 0.01,
  ariaLabel,
  min,
  max,
}: {
  value: number;
  onChange: (v: number) => void;
  step?: number;
  ariaLabel: string;
  min?: number;
  max?: number;
}) {
  const [text, setText] = useState(Number.isFinite(value) ? String(value) : '');
  useEffect(() => {
    // Sync when the value changes from outside (e.g. loading the example).
    const parsed = text.trim() === '' ? Number.NaN : Number(text);
    if (!(Object.is(parsed, value) || (Number.isNaN(parsed) && Number.isNaN(value)))) {
      setText(Number.isFinite(value) ? String(value) : '');
    }
    // Only external value changes matter here; `text` is deliberately not a dependency.
  }, [value]);
  const invalid = text.trim() !== '' && !Number.isFinite(Number(text));
  return (
    <input
      className={`num-input ${invalid || text.trim() === '' ? 'num-input--invalid' : ''}`}
      type="text"
      inputMode="decimal"
      aria-label={ariaLabel}
      aria-invalid={invalid || text.trim() === ''}
      value={text}
      data-step={step}
      data-min={min}
      data-max={max}
      onChange={(e) => {
        setText(e.target.value);
        const t = e.target.value.trim();
        onChange(t === '' ? Number.NaN : Number(t));
      }}
    />
  );
}

/** Editable table of (x, y) points (metres). */
export function PointTable({
  points,
  onChange,
  label,
}: {
  points: Vec2[];
  onChange: (pts: Vec2[]) => void;
  label: string;
}) {
  const set = (i: number, patch: Partial<Vec2>): void => onChange(points.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  return (
    <div className="point-table">
      <table className="table table--compact">
        <thead>
          <tr>
            <th>#</th>
            <th>x (m, east)</th>
            <th>y (m, north)</th>
            <th aria-label="actions" />
          </tr>
        </thead>
        <tbody>
          {points.map((p, i) => (
            <tr key={i}>
              <td>{i + 1}</td>
              <td>
                <NumInput ariaLabel={`${label} point ${i + 1} x`} value={p.x} onChange={(x) => set(i, { x })} />
              </td>
              <td>
                <NumInput ariaLabel={`${label} point ${i + 1} y`} value={p.y} onChange={(y) => set(i, { y })} />
              </td>
              <td>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => onChange(points.filter((_, j) => j !== i))}
                  aria-label={`Remove ${label} point ${i + 1}`}
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="btn btn--ghost btn--sm" onClick={() => onChange([...points, { x: Number.NaN, y: Number.NaN }])}>
        Add point
      </button>
    </div>
  );
}
