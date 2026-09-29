/**
 * Defensive parsing of WebSocket messages from /api/ws.
 *
 * Only a structural check is done (type tag + object payload); the backend is
 * the authority on content. Anything unexpected is dropped rather than
 * guessed at, so a malformed frame can never turn into a fake measurement.
 */

import type { SignalSnapshot, SystemStatus, WsMessage } from '../api/types';

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function looksLikeStatus(v: unknown): v is SystemStatus {
  return (
    isObject(v) &&
    typeof v.server_time_unix_ns === 'number' &&
    typeof v.source_banner === 'string' &&
    typeof v.simulated === 'boolean' &&
    typeof v.source_state === 'string' &&
    Array.isArray(v.links) &&
    Array.isArray(v.activity) &&
    Array.isArray(v.capabilities) &&
    isObject(v.zone) &&
    isObject(v.pose)
  );
}

function looksLikeSnapshot(v: unknown): v is SignalSnapshot {
  return isObject(v) && typeof v.link_id === 'string' && isObject(v.score) && Array.isArray(v.gaps);
}

export function parseWsMessage(raw: unknown): WsMessage | null {
  if (typeof raw !== 'string' || raw.length === 0) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isObject(obj)) return null;
  if (obj.type === 'status' && looksLikeStatus(obj.data)) {
    return { type: 'status', data: obj.data };
  }
  if (obj.type === 'signal' && looksLikeSnapshot(obj.data)) {
    const linkId = typeof obj.link_id === 'string' ? obj.link_id : obj.data.link_id;
    return { type: 'signal', link_id: linkId, data: obj.data };
  }
  return null;
}
