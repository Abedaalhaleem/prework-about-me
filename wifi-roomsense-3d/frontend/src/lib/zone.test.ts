import { describe, expect, it } from 'vitest';
import { zoneDisplayDecision } from './zone';
import { T0_NS, makeActivity, makeStatus } from '../test/statusFixture';
import { blankRoom } from './roomValidation';
import type { CapabilityStatus, RoomGeometry, SystemStatus } from '../api/types';

const verification = {
  software_tested: true,
  firmware_compiled: false,
  hardware_tested: false,
  through_wall_validated: false,
  notes: [],
};
const zoneCap = (state: CapabilityStatus['state']): CapabilityStatus => ({
  capability: 'C_ZONE',
  title: 'Zone',
  state,
  reasons: [],
  verification,
});

function roomWithZone(): RoomGeometry {
  const r = blankRoom();
  r.zones.push({ id: 'z1', label: 'Desk', kind: 'TARGET_ROOM_ZONE', polygon: [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }] });
  return r;
}

function estimateStatus(windowEndNs: number, capState: CapabilityStatus['state'] = 'ENABLED'): SystemStatus {
  return makeStatus({
    capabilities: [zoneCap(capState)],
    zone: {
      state: 'ESTIMATE',
      zone_id: 'z1',
      zone_label: 'Desk',
      model_scores: { z1: 2.1 },
      display_anchor: { x: 0.66, y: 0.33 },
      display_anchor_note: 'Zone centre is a display anchor, not a measured position.',
      reasons: [],
      model_id: 'm1',
      criteria_version: 'c1',
      provenance: makeActivity('tx1->rx1', 'MOTION_DETECTED', windowEndNs).provenance,
    },
  });
}

describe('zoneDisplayDecision', () => {
  it('shows a fresh ESTIMATE only when C_ZONE is ENABLED', () => {
    const d = zoneDisplayDecision(estimateStatus(T0_NS - 1e9), roomWithZone(), 0, 0);
    expect(d.show).toBe(true);
    expect(d.zoneId).toBe('z1');
  });
  it('hides the estimate when C_ZONE is not ENABLED', () => {
    expect(zoneDisplayDecision(estimateStatus(T0_NS, 'REQUIRES_VALIDATION'), roomWithZone(), 0, 0).show).toBe(false);
  });
  it('hides a stale estimate', () => {
    // 1 s old at receipt, 10 s elapsed since -> 11 s > 5 s timeout
    expect(zoneDisplayDecision(estimateStatus(T0_NS - 1e9), roomWithZone(), 0, 10_000).show).toBe(false);
  });
  it('hides DISABLED/ABSTAIN and unknown zones', () => {
    expect(zoneDisplayDecision(makeStatus(), roomWithZone(), 0, 0).show).toBe(false);
    expect(zoneDisplayDecision(estimateStatus(T0_NS), blankRoom(), 0, 0).show).toBe(false);
    expect(zoneDisplayDecision(null, roomWithZone(), null, 0).show).toBe(false);
  });
});
