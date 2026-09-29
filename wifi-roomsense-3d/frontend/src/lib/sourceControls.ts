/**
 * Pure helpers for the source controls (Dashboard → Source).
 *
 *  - Links of the running source are labelled with where they come from, so
 *    a simulated or replayed link never reads like a connected board;
 *    "connected" is said only for LIVE links the backend reports connected.
 *  - "Start live (configured receivers)" is disabled when the backend reports
 *    that no receivers are configured. When that is unknown (older backend
 *    without GET /api/source/receivers, request failed, still loading) the
 *    button stays enabled and the backend decides (409 NO_RECEIVERS_CONFIGURED).
 *  - "Stop source" is disabled while no source is selected.
 */

import type { ConfiguredReceiver, LinkStatus, SystemStatus } from '../api/types';
import { isSimulated } from './banner';

export const NO_RECEIVERS_TEXT = 'No receivers configured — add them in configs/roomsense.toml or use Override below';

/** What the UI knows about the receivers configured on the backend. */
export type ReceiversKnowledge =
  | { kind: 'loading' }
  | { kind: 'list'; receivers: ConfiguredReceiver[] }
  /** The backend has no GET /api/source/receivers (older version): unknown. */
  | { kind: 'unsupported' }
  | { kind: 'error'; message: string };

/** Result of GET /api/source/receivers as seen by the client. */
export type ConfiguredReceiversResult = { kind: 'list'; receivers: ConfiguredReceiver[] } | { kind: 'unsupported' };

export interface Gate {
  enabled: boolean;
  /** Why it is disabled (shown next to the button); null when enabled. */
  reason: string | null;
}

export function startLiveGate(k: ReceiversKnowledge): Gate {
  if (k.kind === 'list' && k.receivers.length === 0) return { enabled: false, reason: NO_RECEIVERS_TEXT };
  return { enabled: true, reason: null };
}

export function stopSourceGate(status: SystemStatus | null): Gate {
  // Unknown (no status) stays enabled: the backend is the authority.
  if (status && status.source_state === 'NO_SOURCE') return { enabled: false, reason: 'No source is running.' };
  return { enabled: true, reason: null };
}

const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * Validate the GET /api/source/receivers payload: a JSON array of receivers
 * (a `{receivers: [...]}` wrapper is accepted too). Returns null when the
 * shape is not recognised, so a malformed answer is never read as "none".
 */
export function parseConfiguredReceivers(raw: unknown): ConfiguredReceiver[] | null {
  const list: unknown =
    raw !== null && typeof raw === 'object' && !Array.isArray(raw) && 'receivers' in raw
      ? (raw as { receivers: unknown }).receivers
      : raw;
  if (!Array.isArray(list)) return null;
  const out: ConfiguredReceiver[] = [];
  for (const item of list) {
    if (item === null || typeof item !== 'object' || Array.isArray(item)) return null;
    const r = item as Record<string, unknown>;
    const receiverId = str(r.receiver_id);
    const port = str(r.port);
    if (receiverId === null || port === null) return null;
    out.push({
      receiver_id: receiverId,
      port,
      baud: int(r.baud),
      input_format: str(r.input_format),
      transmitter_id: str(r.transmitter_id),
      transmitter_mac: str(r.transmitter_mac),
      declared_chip: str(r.declared_chip),
      declared_board: str(r.declared_board),
      ltf_config: str(r.ltf_config),
      link_id: str(r.link_id),
    });
  }
  return out;
}

/** Short one-line description of a configured receiver (only what is configured). */
export function describeReceiver(r: ConfiguredReceiver): string {
  const parts: string[] = [];
  if (r.input_format) parts.push(r.input_format);
  if (r.baud !== null) parts.push(`${r.baud} baud`);
  if (r.declared_chip) parts.push(`chip ${r.declared_chip} (declared)`);
  if (r.declared_board) parts.push(`board ${r.declared_board} (declared)`);
  if (r.ltf_config) parts.push(`LTF ${r.ltf_config}`);
  if (r.transmitter_mac) parts.push(`TX MAC ${r.transmitter_mac}`);
  return parts.join(' · ');
}

export type LinkOriginTone = 'ok' | 'warn' | 'sim' | 'replay' | 'muted';

export interface LinkOrigin {
  text: string;
  tone: LinkOriginTone;
}

/** Where a link of the running source comes from, in words. */
export function linkOrigin(status: SystemStatus, link: LinkStatus): LinkOrigin {
  const simulated = isSimulated(status);
  switch (status.source_mode) {
    case 'SIMULATION':
      return { text: 'simulated link — no board', tone: 'sim' };
    case 'REPLAY':
      return simulated
        ? { text: 'replayed link — simulated data, no board', tone: 'sim' }
        : { text: 'replayed link — recorded earlier, not live', tone: 'replay' };
    case 'LIVE':
      // Fail safe: data flagged simulated is never presented as a board.
      if (simulated) return { text: 'flagged as simulated — not a board measurement', tone: 'sim' };
      return link.connected ? { text: 'connected', tone: 'ok' } : { text: 'not connected', tone: 'warn' };
    default:
      return { text: 'no active source', tone: 'muted' };
  }
}
