import { describe, expect, it } from 'vitest';
import {
  NO_CONNECTION_TEXT,
  NOT_DISTINGUISHABLE_TEXT,
  SIMULATED_BANNER_TEXT,
  bannerInconsistencies,
  calibrationInfo,
  capabilityChips,
  capabilitySummaryText,
  enabledCapabilities,
  liveHealth,
  reasonCode,
  sourceBanner,
  sourcePillText,
  throughWallInfo,
  unsupportedCapabilities,
} from './banner';
import { makeLink, makeStatus } from '../test/statusFixture';
import type { CapabilityStatus, SystemStatus } from '../api/types';

const verification = {
  software_tested: true,
  firmware_compiled: false,
  hardware_tested: false,
  through_wall_validated: false,
  notes: [],
};

describe('sourceBanner', () => {
  it('shows the SIMULATED banner when status.simulated is true', () => {
    const b = sourceBanner(makeStatus({ simulated: true, source_mode: 'SIMULATION', source_banner: 'SIMULATION' }), true);
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.text).toBe('SIMULATION');
    expect(b.severity).toBe('simulation');
    expect(b.inconsistency).toBeNull();
    expect(b.simulatedDetail).toMatch(/simulator/);
    expect(SIMULATED_BANNER_TEXT).toBe('SIMULATED DATA — NOT A MEASUREMENT');
  });

  it('shows RECORDED REPLAY plus the SIMULATED banner, without a warning, for a replay of simulated data', () => {
    // Documented combination (docs/ARCHITECTURE.md "Simulated flag").
    const b = sourceBanner(
      makeStatus({ simulated: true, source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY', source_state: 'FINISHED' }),
      true,
    );
    expect(b.text).toBe('RECORDED REPLAY');
    expect(b.severity).toBe('replay');
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.originText).toBe('SIMULATED DATA');
    expect(b.simulatedDetail).toMatch(/replay of a recording of simulated data/i);
    expect(b.stateText).toBe('FINISHED');
    expect(b.inconsistency).toBeNull();
    expect(sourcePillText(b)).toBe('RECORDED REPLAY (SIMULATED DATA) — FINISHED');
  });

  it('shows a replay of measured data as RECORDED REPLAY without the SIMULATED banner', () => {
    const b = sourceBanner(makeStatus({ source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY' }), true);
    expect(b.text).toBe('RECORDED REPLAY');
    expect(b.showSimulatedBanner).toBe(false);
    expect(b.originText).toBeNull();
    expect(b.inconsistency).toBeNull();
  });

  it('fails safe: simulated=true wins even if the banner text says live', () => {
    const b = sourceBanner(makeStatus({ simulated: true, source_mode: 'LIVE', source_banner: 'LIVE MEASUREMENTS' }), true);
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.text).toBe('SIMULATION');
    expect(b.severity).toBe('simulation');
    expect(b.inconsistency).toMatch(/simulated while the source mode is LIVE/);
  });

  it('treats a SIMULATION source mode as simulated even if the flag is false, and flags it', () => {
    const b = sourceBanner(makeStatus({ simulated: false, source_mode: 'SIMULATION', source_banner: 'SIMULATION' }), true);
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.text).toBe('SIMULATION');
    expect(b.inconsistency).toMatch(/did not flag the data as simulated/);
  });

  it('flags a banner text that does not match the mode, and still fails safe', () => {
    const b = sourceBanner(makeStatus({ source_mode: 'LIVE', source_banner: 'SIMULATION', simulated: false }), true);
    expect(b.text).toBe('SIMULATION');
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.inconsistency).toMatch(/does not match source mode LIVE/);
    const r = sourceBanner(makeStatus({ source_mode: 'REPLAY', source_banner: 'LIVE MEASUREMENTS' }), true);
    expect(r.text).toBe('RECORDED REPLAY');
    expect(r.showSimulatedBanner).toBe(false);
    expect(r.inconsistency).toMatch(/expected "RECORDED REPLAY"/);
  });

  it('maps each source mode to its exact banner text', () => {
    const live = sourceBanner(makeStatus({ links: [makeLink('tx1->rx1')] }), true);
    expect(live.text).toBe('LIVE MEASUREMENTS');
    expect(live.severity).toBe('live');
    expect(live.stateText).toBe('RUNNING');
    expect(sourceBanner(makeStatus({ source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY' }), true).text).toBe(
      'RECORDED REPLAY',
    );
    const none = sourceBanner(makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE' }), true);
    expect(none.text).toBe('NO SOURCE');
    expect(none.showSimulatedBanner).toBe(false);
    expect(none.inconsistency).toBeNull();
  });

  it('says NO SOURCE once, not "NO SOURCE NO SOURCE"', () => {
    const none = sourceBanner(makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE' }), true);
    expect(none.stateText).toBeNull();
    expect(sourcePillText(none)).toBe('NO SOURCE');
  });

  it('shows NO CONNECTION TO BACKEND without a connection, never stale data', () => {
    expect(sourceBanner(makeStatus(), false).text).toBe(NO_CONNECTION_TEXT);
    expect(sourceBanner(null, true).text).toBe('NO CONNECTION TO BACKEND');
    expect(sourceBanner(null, false).showSimulatedBanner).toBe(false);
    expect(sourceBanner(null, false).stateText).toBeNull();
  });

  it('flags an unrecognised banner string', () => {
    const b = sourceBanner(makeStatus({ source_banner: 'DEMO' }), true);
    expect(b.text).toBe('LIVE MEASUREMENTS');
    expect(b.inconsistency).toMatch(/Unrecognised/);
  });

  it('reports no inconsistency for any documented combination', () => {
    const documented: Partial<SystemStatus>[] = [
      { source_mode: 'LIVE', source_banner: 'LIVE MEASUREMENTS', simulated: false },
      { source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY', simulated: false },
      { source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY', simulated: true },
      { source_mode: 'SIMULATION', source_banner: 'SIMULATION', simulated: true },
      { source_mode: null, source_banner: 'NO SOURCE', simulated: false, source_state: 'NO_SOURCE' },
    ];
    for (const patch of documented) expect(bannerInconsistencies(makeStatus(patch))).toEqual([]);
  });
});

describe('LIVE pill health', () => {
  const live = (patch: Partial<SystemStatus>): SystemStatus => makeStatus({ source_mode: 'LIVE', ...patch });

  it('is never the green live style while the source is DISCONNECTED or in ERROR', () => {
    for (const state of ['DISCONNECTED', 'ERROR'] as const) {
      const b = sourceBanner(live({ source_state: state, links: [makeLink('tx1->rx1', { connected: false })] }), true);
      expect(b.text).toBe('LIVE MEASUREMENTS');
      expect(b.severity).toBe('live-down');
      expect(b.stateText).toBe(`${state} (no data)`);
    }
    const d = sourceBanner(live({ source_state: 'DISCONNECTED', links: [makeLink('tx1->rx1', { connected: false })] }), true);
    expect(sourcePillText(d)).toBe('LIVE MEASUREMENTS — DISCONNECTED (no data)');
  });

  it('is not green while RUNNING without any connected link', () => {
    const b = sourceBanner(live({ source_state: 'RUNNING', links: [makeLink('tx1->rx1', { connected: false })] }), true);
    expect(b.severity).toBe('live-down');
    expect(b.stateText).toBe('NO RECEIVER CONNECTED (no data)');
    expect(liveHealth(live({ source_state: 'RUNNING', links: [] })).label).toBe('NO LINK REPORTED (no data)');
    expect(liveHealth(live({ source_state: 'CONNECTING' })).label).toBe('CONNECTING (no data yet)');
  });

  it('is degraded when only some links are connected, and green only when all are', () => {
    const some = sourceBanner(
      live({ links: [makeLink('tx1->rx1'), makeLink('tx1->rx2', { connected: false })] }),
      true,
    );
    expect(some.severity).toBe('live-degraded');
    expect(some.stateText).toBe('RUNNING · 1 of 2 links connected');
    const all = sourceBanner(live({ links: [makeLink('tx1->rx1'), makeLink('tx1->rx2')] }), true);
    expect(all.severity).toBe('live');
  });
});

describe('throughWallInfo', () => {
  it('is UNVERIFIED by default', () => {
    expect(throughWallInfo(makeStatus()).label).toBe('Through-wall: UNVERIFIED');
  });
  it('uses the exact NOT_DISTINGUISHABLE sentence', () => {
    const info = throughWallInfo(makeStatus({ through_wall_status: 'NOT_DISTINGUISHABLE' }));
    expect(info.label).toBe('Target-room and outside-room motion cannot be distinguished with this setup');
    expect(info.label).toBe(NOT_DISTINGUISHABLE_TEXT);
  });
  it('treats unexpected values as unverified', () => {
    const s = makeStatus();
    (s as { through_wall_status: string }).through_wall_status = 'MAYBE';
    expect(throughWallInfo(s).label).toBe('Through-wall: UNVERIFIED');
  });
});

describe('calibrationInfo', () => {
  it('reports NOT VALID with a reason when there is no baseline', () => {
    const info = calibrationInfo(makeStatus());
    expect(info.valid).toBe(false);
    expect(info.label).toBe('Calibration: NOT VALID');
    expect(info.detail.length).toBeGreaterThan(0);
  });
  it('passes through the backend detail', () => {
    expect(calibrationInfo(makeStatus({ calibration_valid: true, calibration_detail: 'baseline ok' })).detail).toBe(
      'baseline ok',
    );
  });
});

describe('capability chips', () => {
  const caps: CapabilityStatus[] = [
    { capability: 'A_ACQUISITION', title: 'Acquisition', state: 'ENABLED', reasons: [], verification },
    { capability: 'B_MOTION', title: 'Motion', state: 'REQUIRES_CALIBRATION', reasons: ['no baseline'], verification },
    { capability: 'D_POSE', title: 'Pose', state: 'UNSUPPORTED', reasons: ['no weights'], verification },
  ];
  it('returns A..D in order and marks missing ones as not reported', () => {
    const chips = capabilityChips(caps);
    expect(chips.map((c) => c.letter)).toEqual(['A', 'B', 'C', 'D']);
    expect(chips[0]?.severity).toBe('ok');
    expect(chips[1]?.severity).toBe('gated');
    expect(chips[2]?.stateLabel).toBe('NOT REPORTED');
    expect(chips[2]?.severity).toBe('missing');
    expect(chips[3]?.severity).toBe('unsupported');
    expect(chips[1]?.reasons).toEqual(['no baseline']);
  });
  it('lists enabled and unsupported capabilities', () => {
    expect(enabledCapabilities(caps)).toEqual(['A_ACQUISITION']);
    expect(unsupportedCapabilities(caps)).toEqual(['D_POSE']);
  });
});

describe('capabilitySummaryText', () => {
  const cap = (capability: CapabilityStatus['capability'], state: CapabilityStatus['state']): CapabilityStatus => ({
    capability,
    title: capability,
    state,
    reasons: [],
    verification,
  });

  it('says what is enabled and names the gate of what is not', () => {
    const caps = [
      cap('A_ACQUISITION', 'HARDWARE_REQUIRED'),
      cap('B_MOTION', 'ENABLED'),
      cap('C_ZONE', 'DISABLED'),
      cap('D_POSE', 'DISABLED'),
    ];
    expect(capabilitySummaryText(caps)).toBe('Enabled now: B · Not enabled: A (hardware required), C, D');
  });

  it('never says "Unsupported: none" and handles nothing enabled or not reported', () => {
    const text = capabilitySummaryText([cap('A_ACQUISITION', 'HARDWARE_REQUIRED'), cap('B_MOTION', 'REQUIRES_CALIBRATION')]);
    expect(text).toBe(
      'Enabled now: none · Not enabled: A (hardware required), B (requires calibration), C (not reported), D (not reported)',
    );
    expect(text).not.toMatch(/Unsupported/);
    expect(capabilitySummaryText([cap('D_POSE', 'UNSUPPORTED')])).toMatch(/D \(unsupported\)/);
  });

  it('omits the "Not enabled" part when everything is enabled', () => {
    const all = (['A_ACQUISITION', 'B_MOTION', 'C_ZONE', 'D_POSE'] as const).map((c) => cap(c, 'ENABLED'));
    expect(capabilitySummaryText(all)).toBe('Enabled now: A, B, C, D');
  });
});

describe('reasonCode', () => {
  it('extracts the primary code of a "CODE: text" reason', () => {
    expect(reasonCode('NO_BASELINE: no valid quiet baseline')).toBe('NO_BASELINE');
    expect(reasonCode('CALIBRATING: recording a quiet baseline')).toBe('CALIBRATING');
    expect(reasonCode('Quiet baseline abc recorded 3 s ago')).toBeNull();
    expect(reasonCode(null)).toBeNull();
  });
});
