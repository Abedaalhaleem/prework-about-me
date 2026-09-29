import { describe, expect, it } from 'vitest';
import {
  activityStateAgeS,
  ageFromWindowEndS,
  formatAge,
  isStale,
  linkDisplayAgeS,
  linkDisplayState,
  linkFreshness,
  linkMeasurementAgeS,
  newestMeasurementAgeS,
  provenanceAgeS,
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
    expect(age).toBeCloseTo(1.2); // newest frame 0.2 s old at receipt + 1 s since
  });

  it('measurement age is the frame age, not the age of the latest result', () => {
    const status = makeStatus({
      links: [makeLink('tx1->rx1', { last_frame_age_s: 0.1 })],
      activity: [makeActivity('tx1->rx1', 'NO_MOTION_DETECTED', T0_NS - 30e9)],
    });
    expect(linkMeasurementAgeS(status.links[0], status.activity[0], status, RECEIVED, RECEIVED)).toBeCloseTo(0.1);
  });

  it('falls back to the result window end only when no frame age is reported', () => {
    const status = makeStatus({ activity: [makeActivity('tx1->rx1', 'UNKNOWN', T0_NS - 2e9)] });
    expect(linkMeasurementAgeS(undefined, status.activity[0], status, RECEIVED, RECEIVED + 1000)).toBeCloseTo(3);
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

  it('ignores frame arrivals when judging the activity state (processing fell behind)', () => {
    // Frames keep arriving (newest 0.1 s old) but the latest result describes a
    // window that ended 8 s ago and was computed 7 s ago: timeout is 5 s.
    const act = makeActivity('tx1->rx1', 'MOTION_DETECTED', T0_NS - 8e9, {
      provenance: { ...makeActivity('tx1->rx1', 'MOTION_DETECTED', T0_NS - 8e9).provenance, computed_at_unix_ns: T0_NS - 7e9 },
    });
    const status = makeStatus({ links: [makeLink('tx1->rx1', { last_frame_age_s: 0.1 })], activity: [act] });
    const f = linkFreshness(status, RECEIVED, RECEIVED).get('tx1->rx1');
    expect(f?.measurementAgeS).toBeCloseTo(0.1);
    expect(f?.stateAgeS).toBeCloseTo(8);
    expect(linkDisplayState(act, f?.stateAgeS ?? null, 5, true, f?.measurementAgeS ?? null)).toBe('STALE');
    // The header's measurement age still reports the fresh frames.
    expect(newestMeasurementAgeS(status, RECEIVED, RECEIVED)).toBeCloseTo(0.1);
  });

  it('ages a result by the older of its window end and its computation time', () => {
    const base = makeActivity('x', 'NO_MOTION_DETECTED', T0_NS - 1e9).provenance;
    // Window ended 1 s ago but the result was computed 6 s ago (clock oddity): 6 s.
    expect(provenanceAgeS({ ...base, computed_at_unix_ns: T0_NS - 6e9 }, T0_NS, 0)).toBeCloseTo(6);
    // Result without a window: its computation time decides.
    expect(provenanceAgeS({ ...base, window_end_unix_ns: null, computed_at_unix_ns: T0_NS - 7e9 }, T0_NS, 0)).toBeCloseTo(7);
    expect(provenanceAgeS(null, T0_NS, 0)).toBeNull();
  });

  it('a fresh result keeps its state until local time makes it stale', () => {
    const act = makeActivity('tx1->rx1', 'NO_MOTION_DETECTED', T0_NS - 0.5e9);
    const status = makeStatus({ links: [makeLink('tx1->rx1', { last_frame_age_s: 0.1 })], activity: [act] });
    const fresh = activityStateAgeS(act, status, RECEIVED, RECEIVED + 1000);
    expect(fresh).toBeCloseTo(1.5);
    expect(linkDisplayState(act, fresh, 5, true, 1.1)).toBe('NO_MOTION_DETECTED');
    // No new status for 6 s: the same result is now 6.5 s old.
    const later = activityStateAgeS(act, status, RECEIVED, RECEIVED + 6000);
    expect(linkDisplayState(act, later, 5, true, null)).toBe('STALE');
  });

  it('labels a state with the older of its own age and the measurement age', () => {
    // SENSOR_OFFLINE verdict computed just now (no window) about a link silent for 45 s.
    const offline = makeActivity('tx1->rx1', 'SENSOR_OFFLINE', null);
    const status = makeStatus({ links: [makeLink('tx1->rx1', { last_frame_age_s: 45, connected: false })], activity: [offline] });
    const f = linkFreshness(status, RECEIVED, RECEIVED).get('tx1->rx1');
    expect(f?.stateAgeS).toBeCloseTo(0);
    expect(linkDisplayAgeS(f)).toBeCloseTo(45);
    expect(linkDisplayAgeS(f, 'SENSOR_OFFLINE')).toBeCloseTo(45);
    // A link that never delivered a frame: the offline verdict has no data age to show.
    expect(linkDisplayAgeS({ measurementAgeS: null, stateAgeS: 0.4 }, 'SENSOR_OFFLINE')).toBeNull();
    // Stale state with fresh frames: the state's own age.
    expect(linkDisplayAgeS({ measurementAgeS: 0.1, stateAgeS: 8 })).toBe(8);
    expect(linkDisplayAgeS({ measurementAgeS: null, stateAgeS: null })).toBeNull();
    expect(linkDisplayAgeS(undefined)).toBeNull();
  });

  it('a known stale measurement age also makes the state STALE (fail safe)', () => {
    const act = makeActivity('tx1->rx1', 'NO_MOTION_DETECTED');
    expect(linkDisplayState(act, 1, 5, true, 9)).toBe('STALE');
    expect(linkDisplayState(act, 1, 5, true, null)).toBe('NO_MOTION_DETECTED');
  });

  it('formats ages', () => {
    expect(formatAge(null)).toBe('unavailable');
    expect(formatAge(0.42)).toBe('0.4 s');
    expect(formatAge(75)).toBe('75 s');
    expect(formatAge(185)).toBe('3 min 5 s');
  });
});
