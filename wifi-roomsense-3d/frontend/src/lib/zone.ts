/**
 * Whether the 3-D view may highlight an estimated zone.
 *
 * All gates must pass: the backend says ESTIMATE, capability C_ZONE is
 * ENABLED, the zone exists in the current geometry and the estimate is not
 * stale. Otherwise nothing is highlighted and the reason is shown instead.
 */

import type { RoomGeometry, SystemStatus } from '../api/types';
import { capabilityEnabled } from './banner';
import { elapsedSinceS, isStale, provenanceAgeS } from './freshness';

export interface ZoneDisplayDecision {
  show: boolean;
  zoneId: string | null;
  zoneLabel: string | null;
  reason: string;
}

const hidden = (reason: string): ZoneDisplayDecision => ({ show: false, zoneId: null, zoneLabel: null, reason });

export function zoneDisplayDecision(
  status: SystemStatus | null,
  room: RoomGeometry | null,
  receivedAtMs: number | null,
  nowMs: number,
): ZoneDisplayDecision {
  if (!status || receivedAtMs === null) return hidden('No backend status: zone estimation not shown.');
  const zone = status.zone;
  const why = zone.reasons.length > 0 ? zone.reasons.join('; ') : status.localization_status;
  if (zone.state === 'DISABLED') return hidden(`Zone estimation DISABLED: ${why}`);
  if (zone.state === 'ABSTAIN') return hidden(`Zone model abstained for this window: ${why}`);
  if (zone.state !== 'ESTIMATE') return hidden(`Unrecognised zone state "${String(zone.state)}": not shown.`);
  if (!capabilityEnabled(status, 'C_ZONE')) {
    return hidden('An estimate was reported but capability C (zone) is not ENABLED, so it is not displayed.');
  }
  if (!zone.zone_id) return hidden('Estimate without a zone id: not shown.');
  if (!room) return hidden('No room geometry loaded: estimate not shown.');
  const z = room.zones.find((zz) => zz.id === zone.zone_id);
  if (!z) return hidden(`Estimated zone "${zone.zone_id}" is not in the current room geometry: not shown.`);
  // The older of the window end and the computation time (see freshness.ts).
  const age = provenanceAgeS(zone.provenance, status.server_time_unix_ns, elapsedSinceS(receivedAtMs, nowMs));
  if (isStale(age, status.stale_clear_timeout_s)) {
    return hidden('The zone estimate is stale (older than the clear timeout): not shown.');
  }
  return { show: true, zoneId: z.id, zoneLabel: zone.zone_label ?? z.label, reason: 'Estimated zone (experimental)' };
}
