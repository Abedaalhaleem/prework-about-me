/**
 * "My Wi-Fi signal": signal strength (RSSI) of the computer running RoomSense.
 *
 * This is one coarse number from the operating system, not CSI. It is shown on
 * its own page only and never feeds the motion detector or the 3-D room.
 */

export interface HostWifiSnapshot {
  note: string;
  running: boolean;
  platform: string;
  method: string | null;
  /** null = not known yet (the reader has not been started). */
  available: boolean | null;
  reason: string | null;
  interval_s: number | null;
  server_time_unix_ms: number;
  errors: number;
  last_error: string | null;
  latest: {
    t: number;
    rssi_dbm: number | null;
    noise_dbm: number | null;
    signal_percent: number | null;
    tx_rate_mbps: number | null;
    channel: number | null;
    connected: boolean;
  } | null;
  series: {
    t: number[];
    rssi_dbm: (number | null)[];
    noise_dbm: (number | null)[];
    signal_percent: (number | null)[];
  };
}

/** Which unit the OS reports: dBm (macOS, Linux) or a percentage (Windows). */
export function signalUnit(snap: HostWifiSnapshot): 'dbm' | 'percent' | 'none' {
  if (snap.series.rssi_dbm.some((v) => v !== null)) return 'dbm';
  if (snap.series.signal_percent.some((v) => v !== null)) return 'percent';
  return 'none';
}

/** Short status line; never claims more than the snapshot says. */
export function hostWifiStatus(snap: HostWifiSnapshot): string {
  if (snap.available === false) return `Not available on this computer: ${snap.reason ?? 'unknown reason'}`;
  if (!snap.running) return 'Stopped. Press Start to sample this computer\'s Wi-Fi signal.';
  const l = snap.latest;
  if (!l) return `Starting (${snap.method ?? 'reader'})…`;
  if (!l.connected) return 'No reading: this computer is not connected to a Wi-Fi network (or the reading failed).';
  if (l.rssi_dbm !== null) return `Signal ${l.rssi_dbm} dBm${l.noise_dbm !== null ? ` · noise ${l.noise_dbm} dBm` : ''}`;
  if (l.signal_percent !== null) return `Signal ${l.signal_percent}% (Windows reports a percentage, not dBm)`;
  return 'No reading in the latest sample.';
}
