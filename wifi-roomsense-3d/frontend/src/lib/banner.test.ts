import { describe, expect, it } from 'vitest';
import {
  NO_CONNECTION_TEXT,
  NOT_DISTINGUISHABLE_TEXT,
  SIMULATED_BANNER_TEXT,
  calibrationInfo,
  capabilityChips,
  enabledCapabilities,
  sourceBanner,
  throughWallInfo,
  unsupportedCapabilities,
} from './banner';
import { makeStatus } from '../test/statusFixture';
import type { CapabilityStatus } from '../api/types';

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
    expect(SIMULATED_BANNER_TEXT).toBe('SIMULATED DATA — NOT A MEASUREMENT');
  });

  it('fails safe: simulated=true wins even if the banner text says live', () => {
    const b = sourceBanner(makeStatus({ simulated: true, source_mode: 'LIVE', source_banner: 'LIVE MEASUREMENTS' }), true);
    expect(b.showSimulatedBanner).toBe(true);
    expect(b.text).toBe('SIMULATION');
    expect(b.inconsistency).not.toBeNull();
  });

  it('treats a SIMULATION source mode as simulated even if the flag is false', () => {
    const b = sourceBanner(makeStatus({ simulated: false, source_mode: 'SIMULATION', source_banner: 'SIMULATION' }), true);
    expect(b.showSimulatedBanner).toBe(true);
  });

  it('maps each source mode to its exact banner text', () => {
    expect(sourceBanner(makeStatus(), true).text).toBe('LIVE MEASUREMENTS');
    expect(sourceBanner(makeStatus({ source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY' }), true).text).toBe(
      'RECORDED REPLAY',
    );
    const none = sourceBanner(makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE' }), true);
    expect(none.text).toBe('NO SOURCE');
    expect(none.showSimulatedBanner).toBe(false);
    expect(none.inconsistency).toBeNull();
  });

  it('shows NO CONNECTION TO BACKEND without a connection, never stale data', () => {
    expect(sourceBanner(makeStatus(), false).text).toBe(NO_CONNECTION_TEXT);
    expect(sourceBanner(null, true).text).toBe('NO CONNECTION TO BACKEND');
    expect(sourceBanner(null, false).showSimulatedBanner).toBe(false);
  });

  it('flags an unrecognised banner string', () => {
    const b = sourceBanner(makeStatus({ source_banner: 'DEMO' }), true);
    expect(b.text).toBe('LIVE MEASUREMENTS');
    expect(b.inconsistency).toMatch(/Unrecognised/);
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
