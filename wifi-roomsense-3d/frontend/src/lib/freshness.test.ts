import { describe, expect, it } from 'vitest';
import {
  ageFromWindowEndS,
  formatAge,
  isStale,
  linkDisplayState,
  linkMeasurementAgeS,
  newestMeasurementAgeS,
  serverNowMs,
} from './freshness';
import { T0_NS, makeActivity, makeLink, makeStatus } from '../test/statusFixture';

const RECEIVED = 1_000_000; // client ms when the status arrived

describe('measurement age', () => {
  it('ages the window end in the server clock and adds time since receipt', () => {
    // window ended 1.5 s before server_time; 2 s have passed on the client since.
    const age = ageFromWindowEndS(T0_NS - 1.5e9, T0_NS, 2);
    expect(age).toBeCloseTo(3.5);
  });

  it('is independent of a skewed client clock', () => {
    const status = makeStatus({
      links: [makeLink('tx1->rx1', { last_frame_age_s: 0.2 })],
      activity: [makeActivity('tx1->rx1', 'NO_MOTION_DETECTED', T0_NS - 0.5e9)],
    });
    // Client clock is wildly different from the server clock; only the delta matters.
    const age = linkMeasurementAgeS(status.links[0], status.activity[0], status, 42, 42 + 1000);
    expect(age).toBeCloseTo(1.2); // min(0.2 + 1, 0.5 + 1)
  });

  it('returns null when nothing can be aged (never guesses)', () => {
    const status = makeStatus({
      links: [makeLink('tx1->rx1', { last_frame_age_s: null })],
      activity: [makeActivity('tx1->rx1', 'UNKNOWN', null)],
    });
    expect(linkMeasurementAgeS(status.links[0], status.activity[0], status, RECEIVED, RECEIVED)).toBeNull();
    expect(newestMeasurementAgeS(status, RECEIVED, RECEIVED)).toBeNull();
  });

  it('reports the newest measurement across links', () => {
    const status = makeStatus({
      links: [makeLink('a->b', { last_frame_age_s: 3 }), makeLink('c->d', { last_frame_age_s: 0.5 })],
    });
    expect(newestMeasurementAgeS(status, RECEIVED, RECEIVED + 500)).toBeCloseTo(1.0);
  });

  it('extrapolates the server clock', () => {
    expect(serverNowMs(makeStatus(), RECEIVED, RECEIVED + 250)).toBe(T0_NS / 1e6 + 250);
  });
});

describe('staleness and display state', () => {
  it('unknown age is stale', () => {
    expect(isStale(null, 5)).toBe(true);
    expect(isStale(4.9, 5)).toBe(false);
    expect(isStale(5.1, 5)).toBe(true);
  });

  it('shows stale data as STALE, never with its last state', () => {
    const act = makeActivity('tx1->rx1', 'NO_MOTION_DETECTED');
    expect(linkDisplayState(act, 6, 5, true)).toBe('STALE');
    expect(linkDisplayState(makeActivity('tx1->rx1', 'MOTION_DETECTED'), 6, 5, true)).toBe('STALE');
    expect(linkDisplayState(act, 1, 5, true)).toBe('NO_MOTION_DETECTED');
  });

  it('keeps SENSOR_OFFLINE and maps missing data/connection to NO_DATA', () => {
    expect(linkDisplayState(makeActivity('x', 'SENSOR_OFFLINE'), null, 5, true)).toBe('SENSOR_OFFLINE');
    expect(linkDisplayState(undefined, 1, 5, true)).toBe('NO_DATA');
    expect(linkDisplayState(makeActivity('x', 'MOTION_DETECTED'), 0.1, 5, false)).toBe('NO_DATA');
  });

  it('formats ages', () => {
    expect(formatAge(null)).toBe('unavailable');
    expect(formatAge(0.42)).toBe('0.4 s');
    expect(formatAge(75)).toBe('75 s');
    expect(formatAge(185)).toBe('3 min 5 s');
  });
});
