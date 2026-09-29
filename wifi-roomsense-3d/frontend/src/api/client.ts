/**
 * Minimal typed fetch wrapper for the RoomSense backend.
 *
 * - JSON in/out; errors surface the HTTP status and the FastAPI `detail`.
 * - An optional bearer token (needed whenever the server has one) is read from sessionStorage, and
 *   only if the user typed one into Settings. It is never logged, never put in
 *   a URL and never persisted beyond the browser session.
 * - There is no fallback/demo data: a failed request is an error the UI shows.
 */

import type {
  CalibrationOverview,
  CalibrationRecord,
  HealthResponse,
  JsonValue,
  LabelledEventRequest,
  PoseStatus,
  ReceiverConfig,
  RecordingInfo,
  RecordingStartRequest,
  RoomGeometry,
  RoomSaveResponse,
  SerialPortInfo,
  SignalSnapshot,
  SimulationScenario,
  SystemStatus,
  ValidationProtocol,
  ValidationRun,
  ValidationRunRequest,
  WalkTestReport,
  ZoneStatusResponse,
} from './types';
import { type ConfiguredReceiversResult, parseConfiguredReceivers } from '../lib/sourceControls';

const TOKEN_KEY = 'roomsense.apiToken';

/** Listeners notified when the token changes (the WebSocket reconnects). */
const tokenListeners = new Set<() => void>();

function safeSessionStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null;
  } catch {
    // Storage can throw (privacy mode / blocked site data). No token then.
    return null;
  }
}

export function readToken(): string | null {
  const store = safeSessionStorage();
  if (!store) return null;
  try {
    const tok = store.getItem(TOKEN_KEY);
    return tok && tok.trim().length > 0 ? tok.trim() : null;
  } catch {
    return null;
  }
}

export function saveToken(token: string): void {
  const store = safeSessionStorage();
  if (!store) throw new Error('sessionStorage is not available in this browser context');
  store.setItem(TOKEN_KEY, token.trim());
  tokenListeners.forEach((fn) => fn());
}

export function clearToken(): void {
  const store = safeSessionStorage();
  try {
    store?.removeItem(TOKEN_KEY);
  } catch {
    /* nothing stored */
  }
  tokenListeners.forEach((fn) => fn());
}

export function onTokenChange(fn: () => void): () => void {
  tokenListeners.add(fn);
  return () => tokenListeners.delete(fn);
}

/** An HTTP/network failure with enough context to show the user. */
export class ApiError extends Error {
  readonly status: number; // 0 = network failure (backend unreachable)
  readonly detail: string;
  readonly path: string;

  constructor(path: string, status: number, detail: string) {
    super(status === 0 ? `Backend unreachable (${path}): ${detail}` : `HTTP ${status} on ${path}: ${detail}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

/**
 * Turn a FastAPI error body into readable text. FastAPI sends
 * `{"detail": "text"}` or `{"detail": [{"loc": [...], "msg": "..."}]}`.
 */
export function formatErrorDetail(body: unknown): string {
  if (body === null || body === undefined) return 'no detail';
  if (typeof body === 'string') return body.slice(0, 2000);
  if (typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d: unknown) => {
          if (d && typeof d === 'object') {
            const rec = d as { loc?: unknown; msg?: unknown };
            const loc = Array.isArray(rec.loc) ? rec.loc.join('.') : '';
            const msg = typeof rec.msg === 'string' ? rec.msg : JSON.stringify(d);
            return loc ? `${loc}: ${msg}` : msg;
          }
          return String(d);
        })
        .join('; ');
    }
    if (detail && typeof detail === 'object') return JSON.stringify(detail).slice(0, 2000);
  }
  return JSON.stringify(body).slice(0, 2000);
}

function authHeaders(): Record<string, string> {
  const token = readToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE';

async function request(path: string, method: Method, body?: unknown, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json', ...authHeaders() };
  let payload: string | undefined;
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: payload, signal, cache: 'no-store' });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError(path, 0, err instanceof Error ? err.message : String(err));
  }
  if (!res.ok) {
    let parsed: unknown = null;
    const text = await res.text().catch(() => '');
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = text;
    }
    throw new ApiError(path, res.status, formatErrorDetail(parsed) || res.statusText);
  }
  return res;
}

export async function apiJson<T>(path: string, method: Method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await request(path, method, body, signal);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(path, res.status, 'response was not valid JSON');
  }
}

/**
 * Download a file through fetch so the bearer token (if any) is sent in a
 * header instead of a URL. The filename comes from Content-Disposition when
 * the server provides one.
 */
export async function apiDownload(path: string, fallbackName: string): Promise<void> {
  const res = await request(path, 'GET');
  const blob = await res.blob();
  const cd = res.headers.get('Content-Disposition') ?? '';
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const name = match?.[1] ? decodeURIComponent(match[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    // Give the browser a moment to start the download before revoking.
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}

const enc = encodeURIComponent;

/**
 * GET /api/source/receivers. An older backend without the endpoint answers
 * 404 (or 405): that is "unknown", not an error and not "none configured".
 * A payload of an unexpected shape is an error, never read as an empty list.
 */
async function configuredReceivers(signal?: AbortSignal): Promise<ConfiguredReceiversResult> {
  const path = '/api/source/receivers';
  let raw: unknown;
  try {
    raw = await apiJson<unknown>(path, 'GET', undefined, signal);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) return { kind: 'unsupported' };
    throw err;
  }
  const receivers = parseConfiguredReceivers(raw);
  if (receivers === null) throw new ApiError(path, 200, 'unexpected response shape (expected a list of receivers)');
  return { kind: 'list', receivers };
}

/** Typed endpoint helpers (docs/ARCHITECTURE.md "HTTP API"). */
export const api = {
  health: (signal?: AbortSignal) => apiJson<HealthResponse>('/api/health', 'GET', undefined, signal),
  status: (signal?: AbortSignal) => apiJson<SystemStatus>('/api/status', 'GET', undefined, signal),
  signal: (linkId: string, seconds = 60, signal?: AbortSignal) =>
    apiJson<SignalSnapshot>(`/api/signal?link_id=${enc(linkId)}&seconds=${enc(String(seconds))}`, 'GET', undefined, signal),

  serialPorts: (signal?: AbortSignal) => apiJson<SerialPortInfo[]>('/api/serial/ports', 'GET', undefined, signal),
  configuredReceivers,
  startLive: (receivers?: ReceiverConfig[]) =>
    apiJson<SystemStatus>('/api/source/live', 'POST', receivers && receivers.length > 0 ? { receivers } : {}),
  startReplay: (recordingId: string, speed: number) =>
    apiJson<SystemStatus>('/api/source/replay', 'POST', { recording_id: recordingId, speed }),
  /** `acknowledge_simulated` is a literal true: the caller must have obtained explicit consent. */
  startSimulation: (scenario: string, acknowledge: true, seed?: number) =>
    apiJson<SystemStatus>('/api/source/simulation', 'POST', {
      scenario,
      acknowledge_simulated: acknowledge,
      ...(seed !== undefined ? { seed } : {}),
    }),
  stopSource: () => apiJson<SystemStatus>('/api/source/stop', 'POST', {}),
  simulationScenarios: (signal?: AbortSignal) =>
    apiJson<SimulationScenario[]>('/api/simulation/scenarios', 'GET', undefined, signal),

  calibration: (signal?: AbortSignal) => apiJson<CalibrationOverview>('/api/calibration', 'GET', undefined, signal),
  baselineStart: (linkIds: string[] | null) =>
    apiJson<JsonValue>('/api/calibration/baseline/start', 'POST', {
      confirm_room_empty: true,
      ...(linkIds && linkIds.length > 0 ? { link_ids: linkIds } : {}),
    }),
  baselineStop: () => apiJson<CalibrationRecord>('/api/calibration/baseline/stop', 'POST', {}),
  baselineCancel: () => apiJson<JsonValue>('/api/calibration/baseline/cancel', 'POST', {}),
  walkTestStart: () => apiJson<JsonValue>('/api/calibration/walk-test/start', 'POST', {}),
  walkTestStop: () => apiJson<WalkTestReport>('/api/calibration/walk-test/stop', 'POST', {}),
  invalidateCalibration: (reason: string) => apiJson<JsonValue>('/api/calibration/invalidate', 'POST', { reason }),

  room: (signal?: AbortSignal) => apiJson<RoomGeometry>('/api/room', 'GET', undefined, signal),
  roomExample: (signal?: AbortSignal) => apiJson<RoomGeometry>('/api/room/example', 'GET', undefined, signal),
  saveRoom: (room: RoomGeometry) => apiJson<RoomSaveResponse>('/api/room', 'PUT', room),

  recordings: (signal?: AbortSignal) => apiJson<RecordingInfo[]>('/api/recordings', 'GET', undefined, signal),
  startRecording: (req: RecordingStartRequest) => apiJson<RecordingInfo>('/api/recordings/start', 'POST', req),
  stopRecording: () => apiJson<RecordingInfo>('/api/recordings/stop', 'POST', {}),
  deleteRecording: (id: string) => apiJson<{ deleted: boolean }>(`/api/recordings/${enc(id)}`, 'DELETE'),
  exportRecording: (id: string) => apiDownload(`/api/recordings/${enc(id)}/export`, `recording-${id}.zip`),

  events: (signal?: AbortSignal) => apiJson<JsonValue>('/api/events', 'GET', undefined, signal),
  postEvent: (ev: LabelledEventRequest) => apiJson<JsonValue>('/api/events', 'POST', ev),

  validationProtocol: (signal?: AbortSignal) =>
    apiJson<ValidationProtocol>('/api/validation/protocol', 'GET', undefined, signal),
  startValidationRun: (req: ValidationRunRequest) => apiJson<ValidationRun>('/api/validation/runs', 'POST', req),
  stopValidationRun: (runId: string) => apiJson<ValidationRun>(`/api/validation/runs/${enc(runId)}/stop`, 'POST', {}),
  validationReport: (signal?: AbortSignal) => apiJson<JsonValue>('/api/validation/report', 'GET', undefined, signal),
  downloadValidationReportMd: () => apiDownload('/api/validation/report.md', 'roomsense-validation-report.md'),

  zoneStatus: (signal?: AbortSignal) => apiJson<ZoneStatusResponse>('/api/zone/status', 'GET', undefined, signal),
  poseStatus: (signal?: AbortSignal) => apiJson<PoseStatus>('/api/pose/status', 'GET', undefined, signal),
  hardware: (signal?: AbortSignal) => apiJson<JsonValue>('/api/hardware', 'GET', undefined, signal),
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
