/**
 * Measurement age and staleness, computed client-side.
 *
 * Two different ages are kept apart on purpose:
 *
 *  - "Measurement age" is the time since the newest *measured frame* on a
 *    link (LinkStatus.last_frame_age_s). It is NOT the render frame rate and
 *    NOT the WebSocket push rate.
 *  - "State age" is how old the data behind the latest *activity result* is,
 *    taken from the result's own provenance (window end and computation time).
 *    A link's displayed state goes STALE when its state age exceeds
 *    stale_clear_timeout_s, regardless of frame arrivals: if processing falls
 *    behind while frames keep arriving, an old state must not look current.
 *
 * Both keep growing between status messages because we add the time elapsed
 * since the status arrived. To avoid depending on the browser and server
 * clocks agreeing (LAN mode), ages are computed in the server's clock:
 * (server_time - timestamp) plus the locally measured time since receipt.
 */

import type { ActivityResult, ActivityState, LinkStatus, Provenance, SystemStatus } from '../api/types';

/** Link states the UI can draw: backend states plus two client-side ones. */
export type LinkDisplayState = ActivityState | 'STALE' | 'NO_DATA';

export function elapsedSinceS(receivedAtMs: number, nowMs: number): number {
  return Math.max(0, (nowMs - receivedAtMs) / 1000);
}

/** Server clock "now" in Unix ms, extrapolated from the last status. */
export function serverNowMs(status: SystemStatus, receivedAtMs: number, nowMs: number): number {
  return status.server_time_unix_ns / 1e6 + Math.max(0, nowMs - receivedAtMs);
}

/** Age of a server-clock timestamp (Unix ns) at client time, or null if there is none. */
export function ageFromWindowEndS(
  windowEndUnixNs: number | null | undefined,
  serverTimeUnixNs: number,
  elapsedS: number,
): number | null {
  if (windowEndUnixNs === null || windowEndUnixNs === undefined || !Number.isFinite(windowEndUnixNs)) return null;
  const atServer = (serverTimeUnixNs - windowEndUnixNs) / 1e9;
  // A negative value means the timestamp is "in the future" of the server
  // clock (clock step); clamp rather than report a negative age.
  return Math.max(0, atServer) + elapsedS;
}

/**
 * Age of a derived result (activity or zone): the OLDER of the age of its
 * window end (the data it describes) and the age of its computation. Null
 * when the provenance carries neither timestamp.
 */
export function provenanceAgeS(
  prov: Provenance | null | undefined,
  serverTimeUnixNs: number,
  elapsedS: number,
): number | null {
  if (!prov) return null;
  const ages = [
    ageFromWindowEndS(prov.window_end_unix_ns, serverTimeUnixNs, elapsedS),
    ageFromWindowEndS(prov.computed_at_unix_ns, serverTimeUnixNs, elapsedS),
  ].filter((a): a is number => a !== null);
  return ages.length > 0 ? Math.max(...ages) : null;
}

/** Age of the newest measured frame on a link, per the backend, plus time since receipt. */
export function linkFrameAgeS(link: LinkStatus | undefined, receivedAtMs: number, nowMs: number): number | null {
  if (!link || link.last_frame_age_s === null || !Number.isFinite(link.last_frame_age_s)) return null;
  return Math.max(0, link.last_frame_age_s) + elapsedSinceS(receivedAtMs, nowMs);
}

/**
 * Measurement age of one link: the age of its newest measured frame. Only
 * when the backend reports no frame age for the link does it fall back to the
 * end of the latest result's window (itself the time of a measured frame, so
 * never fresher than the newest frame). Null when nothing can be aged — never
 * guessed.
 */
export function linkMeasurementAgeS(
  link: LinkStatus | undefined,
  activity: ActivityResult | undefined,
  status: SystemStatus,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  const frameAge = linkFrameAgeS(link, receivedAtMs, nowMs);
  if (frameAge !== null) return frameAge;
  if (!activity) return null;
  return ageFromWindowEndS(
    activity.provenance.window_end_unix_ns,
    status.server_time_unix_ns,
    elapsedSinceS(receivedAtMs, nowMs),
  );
}

/**
 * Age of the latest activity state: how old the data behind it is, from the
 * result's own provenance only (never from frame arrivals).
 */
export function activityStateAgeS(
  activity: ActivityResult | undefined,
  status: SystemStatus,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  if (!activity) return null;
  return provenanceAgeS(activity.provenance, status.server_time_unix_ns, elapsedSinceS(receivedAtMs, nowMs));
}

export interface LinkFreshness {
  /** Age of the newest measured frame ("measurement age"). */
  measurementAgeS: number | null;
  /** Age of the data behind the latest activity state (drives STALE). */
  stateAgeS: number | null;
}

/**
 * The one age to print next to a displayed link state (3-D label): the OLDER
 * of the state age and the measurement age, so a stale state with fresh
 * frames reads its own (old) age. For SENSOR_OFFLINE only the frame age means
 * anything (the offline verdict itself is always recent): a link silent for
 * 45 s reads "45 s", and a link that never delivered a frame shows no age.
 */
export function linkDisplayAgeS(f: LinkFreshness | undefined, state: LinkDisplayState | null = null): number | null {
  if (state === 'SENSOR_OFFLINE') return f?.measurementAgeS ?? null;
  const ages = [f?.stateAgeS ?? null, f?.measurementAgeS ?? null].filter((a): a is number => a !== null);
  return ages.length > 0 ? Math.max(...ages) : null;
}

/** All link ids known to the status (links and activity results). */
export function statusLinkIds(status: SystemStatus): string[] {
  const ids = new Set<string>();
  status.links.forEach((l) => ids.add(l.link_id));
  status.activity.forEach((a) => ids.add(a.link_id));
  return [...ids];
}

export function linkFreshness(status: SystemStatus, receivedAtMs: number, nowMs: number): Map<string, LinkFreshness> {
  const out = new Map<string, LinkFreshness>();
  for (const id of statusLinkIds(status)) {
    const link = status.links.find((l) => l.link_id === id);
    const act = status.activity.find((a) => a.link_id === id);
    out.set(id, {
      measurementAgeS: linkMeasurementAgeS(link, act, status, receivedAtMs, nowMs),
      stateAgeS: activityStateAgeS(act, status, receivedAtMs, nowMs),
    });
  }
  return out;
}

/** Measurement age per link. */
export function linkAges(status: SystemStatus, receivedAtMs: number, nowMs: number): Map<string, number | null> {
  const out = new Map<string, number | null>();
  linkFreshness(status, receivedAtMs, nowMs).forEach((f, id) => out.set(id, f.measurementAgeS));
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
 * it becomes STALE (grey). The state age decides (see the module comment); a
 * known measurement age older than the timeout also makes it STALE (fail
 * safe). SENSOR_OFFLINE always wins. No connection or no result is NO_DATA.
 */
export function linkDisplayState(
  activity: ActivityResult | undefined,
  stateAgeS: number | null,
  timeoutS: number,
  backendConnected: boolean,
  measurementAgeS: number | null = null,
): LinkDisplayState {
  if (!backendConnected || !activity) return 'NO_DATA';
  if (activity.state === 'SENSOR_OFFLINE') return 'SENSOR_OFFLINE';
  if (isStale(stateAgeS, timeoutS)) return 'STALE';
  if (measurementAgeS !== null && isStale(measurementAgeS, timeoutS)) return 'STALE';
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
