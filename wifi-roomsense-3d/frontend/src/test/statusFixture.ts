/**
 * TEST FIXTURE ONLY. Imported exclusively by *.test.ts files; the app never
 * imports this module (there is no client-side demo data). It builds
 * structurally valid SystemStatus objects so the pure logic can be tested.
 */

import type { ActivityResult, ActivityState, LinkStatus, SystemStatus } from '../api/types';

export const T0_NS = 1_700_000_000_000_000_000;

export function makeLink(id: string, patch: Partial<LinkStatus> = {}): LinkStatus {
  const [tx = 'tx1', rx = 'rx1'] = id.split('->');
  return {
    link_id: id,
    receiver_id: rx,
    transmitter_id: tx,
    connected: true,
    last_frame_age_s: 0.1,
    acquisition_rate_hz: 25,
    frames_total: 100,
    frames_rejected: 0,
    parse_errors: 0,
    firmware_drops: null,
    layout_id: null,
    channel: 6,
    device: null,
    clock_offset_ms: null,
    clock_drift_ppm: null,
    ...patch,
  };
}

export function makeActivity(
  id: string,
  state: ActivityState,
  windowEndNs: number | null = T0_NS,
  patch: Partial<ActivityResult> = {},
): ActivityResult {
  return {
    link_id: id,
    state,
    activity_score: 1.0,
    enter_threshold: 4,
    exit_threshold: 2.5,
    calibrated_probability: null,
    uncertainty: null,
    quality: {
      level: 'GOOD',
      packet_rate_hz: 25,
      expected_rate_hz: 25,
      loss_fraction: 0,
      max_gap_s: 0.05,
      timing_jitter_ms: 1,
      rssi_dbm_median: -50,
      valid_subcarrier_fraction: 1,
      frames_in_window: 50,
      rejected_frames: 0,
      flags: [],
    },
    reasons: [],
    provenance: {
      source_mode: 'LIVE',
      session_id: 's1',
      link_ids: [id],
      window_start_unix_ns: windowEndNs === null ? null : windowEndNs - 2e9,
      window_end_unix_ns: windowEndNs,
      window_frame_count: 50,
      config_version: 'cfg-test',
      model_version: null,
      calibration_id: null,
      computed_at_unix_ns: T0_NS,
      measurement_age_s: 0,
    },
    ...patch,
  };
}

export function makeStatus(patch: Partial<SystemStatus> = {}): SystemStatus {
  return {
    server_time_unix_ns: T0_NS,
    source_mode: 'LIVE',
    source_banner: 'LIVE MEASUREMENTS',
    simulated: false,
    source_state: 'RUNNING',
    source_detail: null,
    session_id: 's1',
    hardware_required: true,
    capabilities: [],
    links: [],
    activity: [],
    zone: {
      state: 'DISABLED',
      zone_id: null,
      zone_label: null,
      model_scores: {},
      display_anchor: null,
      display_anchor_note: 'Zone centre is a display anchor, not a measured position.',
      reasons: ['no validated zone model'],
      model_id: null,
      criteria_version: null,
      provenance: null,
    },
    pose: { enabled: false, label: 'EXPERIMENTAL', model_id: null, missing_requirements: [], manifest: null },
    calibration: null,
    calibration_valid: false,
    calibration_detail: null,
    localization_status: 'DISABLED: no validated zone model',
    through_wall_status: 'UNVERIFIED',
    through_wall_detail: null,
    recording_active: false,
    recording_id: null,
    operating_scope: 'One moving participant.',
    stale_clear_timeout_s: 5,
    unsupported_capabilities: [],
    notes: [],
    ...patch,
  };
}
