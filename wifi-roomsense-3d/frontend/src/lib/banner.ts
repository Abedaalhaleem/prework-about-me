/**
 * Mapping from SystemStatus to the permanent header texts.
 *
 * The four source banner strings are fixed by the backend contract. The
 * source pill names the source MODE (LIVE MEASUREMENTS / RECORDED REPLAY /
 * SIMULATION / NO SOURCE). Whether the data is simulated is shown separately,
 * by the large SIMULATED DATA banner. The two are independent on purpose: a
 * replay of a simulated recording is documented as source_banner
 * "RECORDED REPLAY" with simulated=true (docs/ARCHITECTURE.md "Simulated
 * flag") and must show the RECORDED REPLAY pill AND the SIMULATED DATA banner.
 *
 * Fail safe:
 *  - if any signal says the data is simulated, the SIMULATED DATA banner is
 *    shown, and simulated data is never labelled LIVE MEASUREMENTS;
 *  - a LIVE source that is not delivering (disconnected, error, no connected
 *    link) never gets the green "live" style;
 *  - only real contract violations are reported as inconsistencies.
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
import { humanize } from './format';

export const SIMULATED_BANNER_TEXT = 'SIMULATED DATA — NOT A MEASUREMENT';
export const NO_CONNECTION_TEXT = 'NO CONNECTION TO BACKEND';
export const NOT_DISTINGUISHABLE_TEXT = 'Target-room and outside-room motion cannot be distinguished with this setup';

/**
 * Pill style. The three live styles say whether the LIVE source is actually
 * delivering: every link connected / some links connected / no data.
 */
export type BannerSeverity =
  | 'live'
  | 'live-degraded'
  | 'live-down'
  | 'replay'
  | 'simulation'
  | 'none'
  | 'offline';

export interface SourceBannerInfo {
  /** One of the four source banner strings, or NO CONNECTION TO BACKEND. */
  text: SourceBannerText | typeof NO_CONNECTION_TEXT;
  /**
   * What the source is doing, shown after the text ("RUNNING", "FINISHED",
   * "DISCONNECTED (no data)", ...). Null when it would only repeat the text
   * (NO SOURCE) or when there is no backend connection.
   */
  stateText: string | null;
  severity: BannerSeverity;
  /**
   * Origin of the data when the mode alone does not say it: "SIMULATED DATA"
   * for a replay of a simulated recording. Null otherwise.
   */
  originText: string | null;
  /** When true the large SIMULATED DATA banner and 3-D watermark are shown. */
  showSimulatedBanner: boolean;
  /** Second line of the SIMULATED DATA banner: where the simulated data comes from. */
  simulatedDetail: string;
  /** Set when the backend status violates the banner contract. */
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

/** Fail safe: any one of the three signals is enough to treat the data as simulated. */
export function isSimulated(status: SystemStatus): boolean {
  return status.simulated === true || status.source_mode === 'SIMULATION' || status.source_banner === 'SIMULATION';
}

/**
 * Contract violations between source_mode, source_banner and simulated.
 * Documented combinations (including REPLAY + simulated=true) return [].
 */
export function bannerInconsistencies(status: SystemStatus): string[] {
  const out: string[] = [];
  const mode = status.source_mode;
  const expected = bannerForMode(mode);
  const backendText = status.source_banner;
  if (!(SOURCE_BANNER_TEXTS as readonly string[]).includes(backendText)) {
    out.push(`Unrecognised source banner from backend: "${backendText}"`);
  } else if (backendText !== expected) {
    out.push(`Backend banner "${backendText}" does not match source mode ${mode ?? 'none'} (expected "${expected}")`);
  }
  if (mode === 'SIMULATION' && status.simulated !== true) {
    out.push('Source mode is SIMULATION but the backend did not flag the data as simulated');
  }
  if (status.simulated === true && mode !== 'SIMULATION' && mode !== 'REPLAY') {
    out.push(`Backend flags the data as simulated while the source mode is ${mode ?? 'none'}`);
  }
  return out;
}

export interface LiveHealth {
  /** ok: every reported link connected; degraded: some; down: no current data. */
  level: 'ok' | 'degraded' | 'down';
  label: string;
}

/**
 * Whether a LIVE source is actually delivering. A link counts as connected
 * only when the backend says so (transport up and a recent frame).
 */
export function liveHealth(status: SystemStatus): LiveHealth {
  const total = status.links.length;
  const connected = status.links.filter((l) => l.connected).length;
  switch (status.source_state) {
    case 'RUNNING':
      if (total === 0) return { level: 'down', label: 'NO LINK REPORTED (no data)' };
      if (connected === 0) return { level: 'down', label: 'NO RECEIVER CONNECTED (no data)' };
      if (connected < total) {
        return { level: 'degraded', label: `RUNNING · ${connected} of ${total} links connected` };
      }
      return { level: 'ok', label: 'RUNNING' };
    case 'CONNECTING':
      return { level: 'down', label: 'CONNECTING (no data yet)' };
    default:
      // DISCONNECTED, ERROR, FINISHED, NO_SOURCE or anything unexpected.
      return { level: 'down', label: `${humanize(status.source_state)} (no data)` };
  }
}

function simulatedDetailFor(status: SystemStatus): string {
  if (status.source_mode === 'REPLAY') {
    return 'Replay of a recording of simulated data. Nothing on screen was measured by a sensor.';
  }
  if (status.source_mode === 'SIMULATION') {
    return 'Generated by the built-in simulator. Nothing on screen was measured by a sensor.';
  }
  return 'The backend reports this data as simulated. Nothing on screen was measured by a sensor.';
}

export function sourceBanner(status: SystemStatus | null, backendConnected: boolean): SourceBannerInfo {
  if (!backendConnected || !status) {
    return {
      text: NO_CONNECTION_TEXT,
      stateText: null,
      severity: 'offline',
      originText: null,
      showSimulatedBanner: false,
      simulatedDetail: '',
      inconsistency: null,
    };
  }
  const mode = status.source_mode;
  const simulated = isSimulated(status);
  // The pill names the mode. The only exception is fail-safe: simulated data
  // is never shown under a LIVE MEASUREMENTS or NO SOURCE label.
  const text: SourceBannerText = simulated && mode !== 'REPLAY' ? 'SIMULATION' : bannerForMode(mode);
  const problems = bannerInconsistencies(status);

  let severity: BannerSeverity;
  let stateText: string | null = humanize(status.source_state);
  switch (text) {
    case 'LIVE MEASUREMENTS': {
      const health = liveHealth(status);
      severity = health.level === 'ok' ? 'live' : health.level === 'degraded' ? 'live-degraded' : 'live-down';
      stateText = health.label;
      break;
    }
    case 'RECORDED REPLAY':
      severity = 'replay';
      break;
    case 'SIMULATION':
      severity = 'simulation';
      break;
    default:
      severity = 'none';
      // "NO SOURCE" + "NO SOURCE" would say the same thing twice.
      if (stateText === text) stateText = null;
  }

  return {
    text,
    stateText,
    severity,
    originText: simulated && text === 'RECORDED REPLAY' ? 'SIMULATED DATA' : null,
    showSimulatedBanner: simulated,
    simulatedDetail: simulated ? simulatedDetailFor(status) : '',
    inconsistency: problems.length > 0 ? problems.join('; ') : null,
  };
}

/** The pill as one sentence (aria-label, tooltips). */
export function sourcePillText(b: SourceBannerInfo): string {
  return [b.text, b.originText ? `(${b.originText})` : null, b.stateText ? `— ${b.stateText}` : null]
    .filter((s): s is string => !!s)
    .join(' ');
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

/**
 * The primary code of a backend reason string ("CODE: human text", see
 * docs/ARCHITECTURE.md "Reason strings"), or null when there is none.
 */
export function reasonCode(text: string | null | undefined): string | null {
  const m = /^([A-Z][A-Z0-9_]{2,}):/.exec(text ?? '');
  return m?.[1] ?? null;
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

/**
 * One line saying what is enabled now and what is not, e.g.
 * "Enabled now: B · Not enabled: A (hardware required), C, D".
 * The reason is named for every gated state; a plain DISABLED needs none.
 * This is about capabilities A–D only; claims that are never made are a
 * separate list (SystemStatus.unsupported_capabilities).
 */
export function capabilitySummaryText(caps: readonly CapabilityStatus[]): string {
  const chips = capabilityChips(caps);
  const enabled = chips.filter((c) => c.state === 'ENABLED').map((c) => c.letter);
  const notEnabled = chips
    .filter((c) => c.state !== 'ENABLED')
    .map((c) => (c.state === 'DISABLED' ? c.letter : `${c.letter} (${c.stateLabel.toLowerCase()})`));
  const parts = [`Enabled now: ${enabled.length > 0 ? enabled.join(', ') : 'none'}`];
  if (notEnabled.length > 0) parts.push(`Not enabled: ${notEnabled.join(', ')}`);
  return parts.join(' · ');
}

export function capabilityEnabled(status: SystemStatus | null, id: CapabilityId): boolean {
  return !!status && status.capabilities.some((c) => c.capability === id && c.state === 'ENABLED');
}
