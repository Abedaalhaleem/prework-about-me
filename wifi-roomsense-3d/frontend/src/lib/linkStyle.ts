/**
 * Visual style for a sensing link (TX -> RX line) by its display state.
 *
 * Activity is shown per link only. A "no motion" link means exactly that:
 * no motion detected on that one radio path. It is never labelled as an
 * empty room, and stale or unknown links are grey, never cool/"clear".
 */

import type { LinkDisplayState } from './freshness';

export interface LinkStyle {
  /** 0xRRGGBB for three.js */
  color: number;
  /** CSS colour string for DOM/canvas */
  css: string;
  dashed: boolean;
  opacity: number;
  /** Line width in CSS pixels (three.js Line2 / canvas). */
  widthPx: number;
  /** Short state label for chips. */
  short: string;
  /** Plain-language description (tooltips, legend). */
  description: string;
}

const STYLES: Record<LinkDisplayState, LinkStyle> = {
  MOTION_DETECTED: {
    color: 0xff9f43,
    css: '#ff9f43',
    dashed: false,
    opacity: 1,
    widthPx: 6,
    short: 'MOTION',
    description: 'Motion detected on this link (heuristic score above the enter threshold)',
  },
  NO_MOTION_DETECTED: {
    color: 0x4dabf7,
    css: '#4dabf7',
    dashed: false,
    opacity: 0.9,
    widthPx: 3,
    short: 'NO MOTION',
    description: 'No motion detected on this link. This does not mean the room is empty: a motionless person can remain undetected.',
  },
  UNKNOWN: {
    color: 0x8a94a6,
    css: '#8a94a6',
    dashed: false,
    opacity: 0.8,
    widthPx: 2.5,
    short: 'UNKNOWN',
    description: 'Unknown: insufficient quality, no valid baseline, or not enough data for a decision',
  },
  CALIBRATING: {
    color: 0xa7b0c0,
    css: '#a7b0c0',
    dashed: false,
    opacity: 0.8,
    widthPx: 2.5,
    short: 'CALIBRATING',
    description: 'Calibrating: recording the quiet baseline; no motion decisions are made',
  },
  SENSOR_OFFLINE: {
    color: 0xb3262e,
    css: '#d0434b',
    dashed: true,
    opacity: 1,
    widthPx: 3,
    short: 'OFFLINE',
    description: 'Sensor offline: no frames from this link recently',
  },
  STALE: {
    color: 0x6b7280,
    css: '#8b93a1',
    dashed: true,
    opacity: 0.7,
    widthPx: 2,
    short: 'STALE',
    description: 'Stale: the newest measurement is older than the clear timeout, so the last state is not shown',
  },
  NO_DATA: {
    color: 0x4b5563,
    css: '#7b8494',
    dashed: true,
    opacity: 0.6,
    widthPx: 2,
    short: 'NO DATA',
    description: 'No data for this link (no backend connection or no result yet)',
  },
};

export function linkStyle(state: LinkDisplayState): LinkStyle {
  return STYLES[state] ?? STYLES.NO_DATA;
}

export const LEGEND_ORDER: readonly LinkDisplayState[] = [
  'MOTION_DETECTED',
  'NO_MOTION_DETECTED',
  'UNKNOWN',
  'CALIBRATING',
  'STALE',
  'SENSOR_OFFLINE',
  'NO_DATA',
];

/** Colour for a quality level chip. */
export function qualityCss(level: string | null | undefined): string {
  switch (level) {
    case 'GOOD':
      return '#51cf66';
    case 'DEGRADED':
      return '#fcc419';
    case 'BAD':
      return '#ff6b6b';
    default:
      return '#8a94a6';
  }
}
