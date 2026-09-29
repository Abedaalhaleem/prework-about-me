/** Formatting helpers. `null` always renders as "unavailable", never as 0. */

export const UNAVAILABLE = 'unavailable';

export function fmtNum(v: number | null | undefined, digits = 1, unit = ''): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return UNAVAILABLE;
  const s = v.toFixed(digits);
  return unit ? `${s} ${unit}` : s;
}

export function fmtInt(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return UNAVAILABLE;
  return Math.round(v).toLocaleString('en-US');
}

export function fmtPercent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) return UNAVAILABLE;
  return `${(fraction * 100).toFixed(digits)} %`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return UNAVAILABLE;
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return UNAVAILABLE;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m} min ${s} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function fmtUnixNs(ns: number | null | undefined): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return UNAVAILABLE;
  const d = new Date(ns / 1e6);
  return Number.isNaN(d.getTime()) ? UNAVAILABLE : d.toLocaleString();
}

/** Human label from an ENUM_LIKE_VALUE. */
export function humanize(v: string): string {
  return v.replace(/_/g, ' ');
}
