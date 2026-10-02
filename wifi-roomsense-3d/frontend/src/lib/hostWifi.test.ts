import { describe, expect, it } from 'vitest';
import { type HostWifiSnapshot, hostWifiStatus, signalUnit } from './hostWifi';

function snap(over: Partial<HostWifiSnapshot> = {}): HostWifiSnapshot {
  return {
    note: 'not CSI',
    running: true,
    platform: 'Darwin 25.0',
    method: 'macOS CoreWLAN',
    available: true,
    reason: null,
    interval_s: 0.5,
    server_time_unix_ms: 10_000,
    errors: 0,
    last_error: null,
    latest: { t: 9_500, rssi_dbm: -56, noise_dbm: -94, signal_percent: null, tx_rate_mbps: 864, channel: 36, connected: true },
    series: { t: [9_000, 9_500], rssi_dbm: [null, -56], noise_dbm: [null, -94], signal_percent: [null, null] },
    ...over,
  };
}

describe('host Wi-Fi signal', () => {
  it('reports dBm when RSSI exists', () => {
    expect(signalUnit(snap())).toBe('dbm');
    expect(hostWifiStatus(snap())).toBe('Signal -56 dBm · noise -94 dBm');
  });

  it('says why it is unavailable instead of showing numbers', () => {
    const s = snap({ available: false, reason: '/proc/net/wireless does not exist', running: false, latest: null });
    expect(hostWifiStatus(s)).toContain('Not available');
    expect(hostWifiStatus(s)).toContain('/proc/net/wireless');
  });

  it('before the first start it says "press Start", not "not available"', () => {
    const s = snap({ available: null, reason: null, running: false, method: null, latest: null });
    expect(hostWifiStatus(s)).toContain('Press Start');
    expect(hostWifiStatus(s)).not.toContain('Not available');
  });

  it('never invents a value when not connected', () => {
    const s = snap({ latest: { t: 1, rssi_dbm: null, noise_dbm: null, signal_percent: null, tx_rate_mbps: null, channel: null, connected: false } });
    expect(hostWifiStatus(s)).toContain('No reading');
  });

  it('uses percent for Windows readings', () => {
    const s = snap({
      latest: { t: 1, rssi_dbm: null, noise_dbm: null, signal_percent: 85, tx_rate_mbps: null, channel: 6, connected: true },
      series: { t: [1], rssi_dbm: [null], noise_dbm: [null], signal_percent: [85] },
    });
    expect(signalUnit(s)).toBe('percent');
    expect(hostWifiStatus(s)).toContain('85%');
  });
});
