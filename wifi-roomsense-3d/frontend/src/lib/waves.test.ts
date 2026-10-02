import { describe, expect, it } from 'vitest';
import type { RoomGeometry } from '../api/types';
import type { HostWifiSnapshot } from './hostWifi';
import { freshMeasuredDbm, waveSources } from './waves';

const node = (id: string, role: 'TX' | 'RX' | 'ROUTER') => ({
  id,
  role,
  label: id,
  position: { x: 1, y: 1, z: 1 },
  inside_target_room: null,
  device_mac: null,
});

const snap = (over: Partial<HostWifiSnapshot>): HostWifiSnapshot => ({
  note: '',
  running: true,
  platform: 'Darwin',
  method: 'corewlan',
  available: true,
  reason: null,
  interval_s: 0.5,
  server_time_unix_ms: 10_000,
  errors: 0,
  last_error: null,
  latest: { t: 9_500, rssi_dbm: -55, noise_dbm: -92, signal_percent: null, tx_rate_mbps: null, channel: 36, connected: true },
  series: { t: [], rssi_dbm: [], noise_dbm: [], signal_percent: [] },
  ...over,
});

describe('waves helpers', () => {
  it('lists routers before TX nodes and skips receivers', () => {
    const room = { nodes: [node('tx1', 'TX'), node('rx1', 'RX'), node('r1', 'ROUTER')] } as unknown as RoomGeometry;
    expect(waveSources(room).map((n) => n.id)).toEqual(['r1', 'tx1']);
    expect(waveSources(null)).toEqual([]);
  });

  it('returns the measured RSSI only while it is fresh', () => {
    expect(freshMeasuredDbm(snap({}))).toBe(-55);
    expect(freshMeasuredDbm(snap({ server_time_unix_ms: 20_000 }))).toBeNull(); // 10.5 s old
    expect(freshMeasuredDbm(snap({ running: false }))).toBeNull();
    expect(freshMeasuredDbm(snap({ latest: null }))).toBeNull();
    expect(freshMeasuredDbm(null)).toBeNull();
  });
});
