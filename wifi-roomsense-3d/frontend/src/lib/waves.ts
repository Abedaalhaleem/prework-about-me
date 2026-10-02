/**
 * Small helpers for the "Wi-Fi waves (simulated)" page. The page draws a
 * MODEL of the room layout; the only measured number on it is this computer's
 * own RSSI, shown beside the model's prediction, never fed into it.
 */

import type { RoomGeometry, SensorNode } from '../api/types';
import type { HostWifiSnapshot } from './hostWifi';

/** Nodes that can act as the Wi-Fi source in the model: routers first, then TX nodes. */
export function waveSources(room: RoomGeometry | null): SensorNode[] {
  if (!room) return [];
  const routers = room.nodes.filter((n) => n.role === 'ROUTER');
  const txs = room.nodes.filter((n) => n.role === 'TX');
  return [...routers, ...txs];
}

/** This computer's latest RSSI, only if it is fresh (else null: never a stale or invented value). */
export function freshMeasuredDbm(snap: HostWifiSnapshot | null, maxAgeMs = 5000): number | null {
  const latest = snap?.latest;
  if (!snap || !snap.running || !latest || latest.rssi_dbm === null) return null;
  if (snap.server_time_unix_ms - latest.t > maxAgeMs) return null;
  return latest.rssi_dbm;
}
