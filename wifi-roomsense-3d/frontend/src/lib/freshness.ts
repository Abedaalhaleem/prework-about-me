/**
 * Measurement age and staleness, computed client-side.
 *
 * "Measurement age" is the time since the newest *measured* frame, NOT the
 * render frame rate and NOT the WebSocket push rate. It keeps growing between
 * status messages because we add the time elapsed since the status arrived.
 *
 * To avoid depending on the browser and server clocks agreeing (LAN mode),
 * ages are computed in the server's clock: (server_time - window_end) plus the
 * locally measured time since the status was received.
 */

import type { ActivityResult, ActivityState, LinkStatus, SystemStatus } from '../api/types';

/** Link states the UI can draw: backend states plus two client-side ones. */
export type LinkDisplayState = ActivityState | 'STALE' | 'NO_DATA';

export function elapsedSinceS(receivedAtMs: number, nowMs: number): number {
  return Math.max(0, (nowMs - receivedAtMs) / 1000);
}

/** Server clock "now" in Unix ms, extrapolated from the last status. */
export function serverNowMs(status: SystemStatus, receivedAtMs: number, nowMs: number): number {
  return status.server_time_unix_ns / 1e6 + Math.max(0, nowMs - receivedAtMs);
}

/** Age of a window end (Unix ns, server clock) at client time `nowMs`. */
export function ageFromWindowEndS(
  windowEndUnixNs: number | null | undefined,
  serverTimeUnixNs: number,
  elapsedS: number,
): number | null {
  if (windowEndUnixNs === null || windowEndUnixNs === undefined || !Number.isFinite(windowEndUnixNs)) return null;
  const atServer = (serverTimeUnixNs - windowEndUnixNs) / 1e9;
  // A negative value means the window end is "in the future" of the server
  // clock (clock step); clamp rather than report a negative age.
  return Math.max(0, atServer) + elapsedS;
}

/**
 * Age of the newest measurement for one link, in seconds, or null when the
 * backend reports nothing we can age (never guessed).
 */
export function linkMeasurementAgeS(
  link: LinkStatus | undefined,
  activity: ActivityResult | undefined,
  status: SystemStatus,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  const elapsed = elapsedSinceS(receivedAtMs, nowMs);
  const candidates: number[] = [];
  if (link && link.last_frame_age_s !== null && Number.isFinite(link.last_frame_age_s)) {
    candidates.push(Math.max(0, link.last_frame_age_s) + elapsed);
  }
  if (activity) {
    const a = ageFromWindowEndS(activity.provenance.window_end_unix_ns, status.server_time_unix_ns, elapsed);
    if (a !== null) candidates.push(a);
  }
  return candidates.length > 0 ? Math.min(...candidates) : null;
}

/** All link ids known to the status (links and activity results). */
export function statusLinkIds(status: SystemStatus): string[] {
  const ids = new Set<string>();
  status.links.forEach((l) => ids.add(l.link_id));
  status.activity.forEach((a) => ids.add(a.link_id));
  return [...ids];
}

export function linkAges(status: SystemStatus, receivedAtMs: number, nowMs: number): Map<string, number | null> {
  const out = new Map<string, number | null>();
  for (const id of statusLinkIds(status)) {
    const link = status.links.find((l) => l.link_id === id);
    const act = status.activity.find((a) => a.link_id === id);
    out.set(id, linkMeasurementAgeS(link, act, status, receivedAtMs, nowMs));
  }
  return out;
}

/** Age of the newest measurement on any link, or null if none. */
export function newestMeasurementAgeS(status: SystemStatus, receivedAtMs: number, nowMs: number): number | null {
  let best: number | null = null;
  linkAges(status, receivedAtMs, nowMs).forEach((age) => {
    if (age !== null && (best === null || age < best)) best = age;
  });
  return best;
}

/** Unknown age counts as stale: we cannot show it as current. */
export function isStale(ageS: number | null, timeoutS: number): boolean {
  return ageS === null || !Number.isFinite(ageS) || ageS > timeoutS;
}

/**
 * What to draw for a link. Stale data is never shown with its last state:
 * it becomes STALE (grey). SENSOR_OFFLINE always wins. No connection or no
 * result at all is NO_DATA.
 */
export function linkDisplayState(
  activity: ActivityResult | undefined,
  ageS: number | null,
  timeoutS: number,
  backendConnected: boolean,
): LinkDisplayState {
  if (!backendConnected || !activity) return 'NO_DATA';
  if (activity.state === 'SENSOR_OFFLINE') return 'SENSOR_OFFLINE';
  if (isStale(ageS, timeoutS)) return 'STALE';
  return activity.state;
}

export function formatAge(ageS: number | null): string {
  if (ageS === null || !Number.isFinite(ageS)) return 'unavailable';
  if (ageS < 10) return `${ageS.toFixed(1)} s`;
  if (ageS < 120) return `${Math.round(ageS)} s`;
  if (ageS < 7200) {
    const m = Math.floor(ageS / 60);
    const s = Math.round(ageS - m * 60);
    return `${m} min ${s} s`;
  }
  return `${(ageS / 3600).toFixed(1)} h`;
}
