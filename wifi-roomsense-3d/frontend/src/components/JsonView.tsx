/**
 * Generic, bounded renderer for JSON whose exact shape is owned by another
 * module (hardware inspection, validation report, zone report, walk test).
 *
 * - `null` renders as `nullLabel` ("unavailable" or "NOT MEASURED"), never 0.
 * - The literal string "NOT MEASURED" is preserved and highlighted.
 * - A key named `confusion_matrix` holding a 2-D number array renders as a grid.
 * - Depth and item counts are capped so a huge payload cannot freeze the UI.
 */

import type { ReactNode } from 'react';

const MAX_DEPTH = 7;
const MAX_ITEMS = 200;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isMatrix(v: unknown): v is (number | null)[][] {
  return (
    Array.isArray(v) &&
    v.length > 0 &&
    v.every((row) => Array.isArray(row) && row.every((c) => c === null || typeof c === 'number'))
  );
}

function labelsFor(parent: Record<string, unknown> | null): string[] | null {
  if (!parent) return null;
  for (const key of ['labels', 'classes', 'zone_ids', 'zones', 'class_labels']) {
    const v = parent[key];
    if (Array.isArray(v) && v.every((x) => typeof x === 'string')) return v as string[];
  }
  return null;
}

export function ConfusionMatrix({ matrix, labels }: { matrix: (number | null)[][]; labels: string[] | null }) {
  const n = matrix.length;
  return (
    <div className="table-wrap">
      <table className="table table--matrix">
        <caption>Rows: true zone · Columns: predicted zone (counts)</caption>
        <thead>
          <tr>
            <th scope="col">true \ pred</th>
            {(matrix[0] ?? []).map((_, j) => (
              <th scope="col" key={j}>
                {labels?.[j] ?? j}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.slice(0, MAX_ITEMS).map((row, i) => (
            <tr key={i}>
              <th scope="row">{labels?.[i] ?? i}</th>
              {row.map((c, j) => (
                <td key={j} className={i === j && n > 0 ? 'matrix__diag' : undefined}>
                  {c === null ? <span className="not-measured">NOT MEASURED</span> : c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Scalar({ value, nullLabel }: { value: unknown; nullLabel: string }) {
  if (value === null || value === undefined) return <span className="not-measured">{nullLabel}</span>;
  if (typeof value === 'string') {
    if (value.toUpperCase() === 'NOT MEASURED') return <span className="not-measured">NOT MEASURED</span>;
    return <span className="json-string">{value}</span>;
  }
  if (typeof value === 'boolean') return <span className={`json-bool json-bool--${value}`}>{value ? 'yes' : 'no'}</span>;
  if (typeof value === 'number') {
    return <span className="json-number">{Number.isInteger(value) ? value : Number(value.toPrecision(6))}</span>;
  }
  return <span>{String(value)}</span>;
}

function Node({
  value,
  depth,
  nullLabel,
  parent,
  keyName,
}: {
  value: unknown;
  depth: number;
  nullLabel: string;
  parent: Record<string, unknown> | null;
  keyName: string | null;
}): ReactNode {
  if (depth > MAX_DEPTH) return <span className="muted">… (nested too deeply to show)</span>;
  if (keyName === 'confusion_matrix' && isMatrix(value)) {
    return <ConfusionMatrix matrix={value} labels={labelsFor(parent)} />;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">(none)</span>;
    if (value.every((v) => !isPlainObject(v) && !Array.isArray(v))) {
      return (
        <span className="json-list">
          {value.slice(0, MAX_ITEMS).map((v, i) => (
            <span key={i}>
              {i > 0 && ', '}
              <Scalar value={v} nullLabel={nullLabel} />
            </span>
          ))}
          {value.length > MAX_ITEMS && <span className="muted"> … {value.length - MAX_ITEMS} more</span>}
        </span>
      );
    }
    return (
      <ol className="json-array">
        {value.slice(0, MAX_ITEMS).map((v, i) => (
          <li key={i}>
            <Node value={v} depth={depth + 1} nullLabel={nullLabel} parent={null} keyName={null} />
          </li>
        ))}
      </ol>
    );
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value).slice(0, MAX_ITEMS);
    if (entries.length === 0) return <span className="muted">(empty)</span>;
    return (
      <dl className="json-object">
        {entries.map(([k, v]) => (
          <div className="json-object__row" key={k}>
            <dt>{k.replace(/_/g, ' ')}</dt>
            <dd>
              <Node value={v} depth={depth + 1} nullLabel={nullLabel} parent={value} keyName={k} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Scalar value={value} nullLabel={nullLabel} />;
}

export function JsonView({ value, nullLabel = 'unavailable' }: { value: unknown; nullLabel?: string }) {
  return (
    <div className="json-view">
      <Node value={value} depth={0} nullLabel={nullLabel} parent={null} keyName={null} />
    </div>
  );
}
