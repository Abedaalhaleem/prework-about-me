/**
 * Mapping from SystemStatus to the permanent header texts.
 *
 * The four source banner strings are fixed by the backend contract. When the
 * data is simulated the UI must say so unmistakably, so `simulated` (or a
 * SIMULATION source mode) always wins over any other text: fail safe.
 */

import type {
  CapabilityId,
  CapabilityState,
  CapabilityStatus,
  SourceBannerText,
  SourceMode,
  SystemStatus,
} from '../api/types';
import { CAPABILITY_IDS, SOURCE_BANNER_TEXTS } from '../api/types';

export const SIMULATED_BANNER_TEXT = 'SIMULATED DATA — NOT A MEASUREMENT';
export const NO_CONNECTION_TEXT = 'NO CONNECTION TO BACKEND';
export const NOT_DISTINGUISHABLE_TEXT = 'Target-room and outside-room motion cannot be distinguished with this setup';

export type BannerSeverity = 'live' | 'replay' | 'simulation' | 'none' | 'offline';

export interface SourceBannerInfo {
  /** One of the four source banner strings, or NO CONNECTION TO BACKEND. */
  text: SourceBannerText | typeof NO_CONNECTION_TEXT;
  severity: BannerSeverity;
  /** When true the large SIMULATED DATA banner and 3-D watermark are shown. */
  showSimulatedBanner: boolean;
  /** Set when the backend's banner text disagrees with its source mode. */
  inconsistency: string | null;
}

export function bannerForMode(mode: SourceMode | null): SourceBannerText {
  switch (mode) {
    case 'LIVE':
      return 'LIVE MEASUREMENTS';
    case 'REPLAY':
      return 'RECORDED REPLAY';
    case 'SIMULATION':
      return 'SIMULATION';
    default:
      return 'NO SOURCE';
  }
}

function severityFor(text: SourceBannerText): BannerSeverity {
  switch (text) {
    case 'LIVE MEASUREMENTS':
      return 'live';
    case 'RECORDED REPLAY':
      return 'replay';
    case 'SIMULATION':
      return 'simulation';
    default:
      return 'none';
  }
}

export function isSimulated(status: SystemStatus): boolean {
  return status.simulated === true || status.source_mode === 'SIMULATION';
}

export function sourceBanner(status: SystemStatus | null, backendConnected: boolean): SourceBannerInfo {
  if (!backendConnected || !status) {
    return { text: NO_CONNECTION_TEXT, severity: 'offline', showSimulatedBanner: false, inconsistency: null };
  }
  const simulated = isSimulated(status);
  const text: SourceBannerText = simulated ? 'SIMULATION' : bannerForMode(status.source_mode);
  const backendText = status.source_banner;
  let inconsistency: string | null = null;
  if (!(SOURCE_BANNER_TEXTS as readonly string[]).includes(backendText)) {
    inconsistency = `Unrecognised source banner from backend: "${backendText}"`;
  } else if (backendText !== text) {
    inconsistency = `Backend banner "${backendText}" disagrees with source mode/simulated flag; showing "${text}"`;
  }
  return { text, severity: severityFor(text), showSimulatedBanner: simulated, inconsistency };
}

export interface ThroughWallInfo {
  label: string;
  detail: string | null;
  severity: 'warn' | 'ok' | 'bad';
}

export function throughWallInfo(status: SystemStatus): ThroughWallInfo {
  switch (status.through_wall_status) {
    case 'VALIDATED':
      return {
        label: 'Through-wall: VALIDATED',
        detail: status.through_wall_detail ?? 'Validated per protocol; see the Validation report for the measured numbers.',
        severity: 'ok',
      };
    case 'NOT_DISTINGUISHABLE':
      return { label: NOT_DISTINGUISHABLE_TEXT, detail: status.through_wall_detail, severity: 'bad' };
    default:
      // Anything unexpected is treated as unverified, never as validated.
      return {
        label: 'Through-wall: UNVERIFIED',
        detail:
          status.through_wall_detail ??
          'Sensing behind a wall has not been validated for this setup. Motion outside the target room may also trigger links.',
        severity: 'warn',
      };
  }
}

export interface CalibrationInfo {
  label: string;
  detail: string;
  valid: boolean;
}

export function calibrationInfo(status: SystemStatus): CalibrationInfo {
  const valid = status.calibration_valid === true;
  const fallback = valid
    ? `Baseline ${status.calibration?.calibration_id ?? ''}`.trim()
    : 'No valid quiet baseline: motion states stay UNKNOWN until calibrated.';
  return {
    label: valid ? 'Calibration: VALID' : 'Calibration: NOT VALID',
    detail: status.calibration_detail ?? status.calibration?.invalidated_reason ?? fallback,
    valid,
  };
}

export const CAPABILITY_LETTER: Record<CapabilityId, string> = {
  A_ACQUISITION: 'A',
  B_MOTION: 'B',
  C_ZONE: 'C',
  D_POSE: 'D',
};

export const CAPABILITY_NAME: Record<CapabilityId, string> = {
  A_ACQUISITION: 'Live CSI acquisition',
  B_MOTION: 'Motion detection',
  C_ZONE: 'Zone estimation (experimental)',
  D_POSE: 'Pose research',
};

export type ChipSeverity = 'ok' | 'gated' | 'off' | 'unsupported' | 'missing';

export function capabilitySeverity(state: CapabilityState | null): ChipSeverity {
  switch (state) {
    case 'ENABLED':
      return 'ok';
    case 'HARDWARE_REQUIRED':
    case 'REQUIRES_CALIBRATION':
    case 'REQUIRES_VALIDATION':
      return 'gated';
    case 'DISABLED':
      return 'off';
    case 'UNSUPPORTED':
      return 'unsupported';
    default:
      return 'missing';
  }
}

export interface CapabilityChip {
  id: CapabilityId;
  letter: string;
  name: string;
  state: CapabilityState | null;
  stateLabel: string;
  severity: ChipSeverity;
  reasons: string[];
}

/** One chip per capability A..D, in order; missing ones say "not reported". */
export function capabilityChips(caps: readonly CapabilityStatus[]): CapabilityChip[] {
  return CAPABILITY_IDS.map((id) => {
    const cap = caps.find((c) => c.capability === id);
    const state = cap?.state ?? null;
    return {
      id,
      letter: CAPABILITY_LETTER[id],
      name: cap?.title || CAPABILITY_NAME[id],
      state,
      stateLabel: state ? state.replace(/_/g, ' ') : 'NOT REPORTED',
      severity: capabilitySeverity(state),
      reasons: cap?.reasons ?? ['The backend did not report this capability.'],
    };
  });
}

export function enabledCapabilities(caps: readonly CapabilityStatus[]): CapabilityId[] {
  return caps.filter((c) => c.state === 'ENABLED').map((c) => c.capability);
}

export function unsupportedCapabilities(caps: readonly CapabilityStatus[]): CapabilityId[] {
  return caps.filter((c) => c.state === 'UNSUPPORTED').map((c) => c.capability);
}

export function capabilityEnabled(status: SystemStatus | null, id: CapabilityId): boolean {
  return !!status && status.capabilities.some((c) => c.capability === id && c.state === 'ENABLED');
}
