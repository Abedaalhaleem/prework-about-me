/**
 * Live status over /api/ws with exponential-backoff reconnect.
 *
 * The backend pushes `{"type":"status","data":SystemStatus}` at
 * websocket_push_hz and `{"type":"signal","link_id":..,"data":SignalSnapshot}`
 * per link. This module keeps ONLY the latest of each (bounded) and exposes
 * them to React via useSyncExternalStore.
 *
 * Honesty rules implemented here:
 *  - When the socket is not open, `status` is null. The UI then shows
 *    "NO CONNECTION TO BACKEND" instead of the last (now unverifiable) state.
 *  - A watchdog closes a socket that stops delivering status messages, so a
 *    hung backend is not mistaken for a quiet one.
 *  - Nothing is synthesised client-side, ever.
 */

import { useSyncExternalStore } from 'react';
import type { SignalSnapshot, SystemStatus } from './types';
import { onTokenChange, readToken } from './client';
import { backoffDelayMs } from '../lib/backoff';
import { parseWsMessage } from '../lib/messages';

export type ConnectionState = 'connecting' | 'open' | 'closed';

export interface SignalEntry {
  data: SignalSnapshot;
  /** Client Date.now() when the snapshot arrived. */
  receivedAtMs: number;
}

export interface LiveSnapshot {
  connection: ConnectionState;
  status: SystemStatus | null;
  /** Client Date.now() when `status` arrived; used to age measurements. */
  statusReceivedAtMs: number | null;
  signals: ReadonlyMap<string, SignalEntry>;
  lastError: string | null;
  /** Consecutive failed attempts since the last successful status. */
  attempt: number;
  nextRetryAtMs: number | null;
}

/** Never keep snapshots for more links than this (bounded memory). */
const MAX_SIGNAL_LINKS = 64;
/** Close and reconnect if no status arrives for this long while "open". */
const STATUS_WATCHDOG_MS = 6_000;
/** Characters allowed in a WebSocket subprotocol token (RFC 6455 / RFC 7230 tchar subset). */
const SUBPROTOCOL_SAFE = /^[A-Za-z0-9._~-]+$/;

function wsUrl(): string {
  const loc = window.location;
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${loc.host}/api/ws`;
}

const EMPTY_SIGNALS: ReadonlyMap<string, SignalEntry> = new Map();

class LiveConnection {
  private ws: WebSocket | null = null;
  private listeners = new Set<() => void>();
  private retryTimer: number | null = null;
  private watchdogTimer: number | null = null;
  private stopTimer: number | null = null;
  private running = false;
  private unsubscribeToken: (() => void) | null = null;
  private snapshot: LiveSnapshot = {
    connection: 'closed',
    status: null,
    statusReceivedAtMs: null,
    signals: EMPTY_SIGNALS,
    lastError: null,
    attempt: 0,
    nextRetryAtMs: null,
  };

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    if (this.stopTimer !== null) {
      window.clearTimeout(this.stopTimer);
      this.stopTimer = null;
    }
    if (!this.running) this.start();
    return () => {
      this.listeners.delete(fn);
      if (this.listeners.size === 0) {
        // Delay the teardown so React StrictMode's mount/unmount/mount cycle
        // does not open two sockets.
        this.stopTimer = window.setTimeout(() => this.stop(), 1_000);
      }
    };
  };

  getSnapshot = (): LiveSnapshot => this.snapshot;

  /** Force an immediate reconnect (e.g. after the API token changed). */
  reconnectNow(): void {
    this.closeSocket();
    this.clearRetry();
    this.update({ attempt: 0 });
    if (this.running) this.connect();
  }

  private start(): void {
    this.running = true;
    this.unsubscribeToken = onTokenChange(() => this.reconnectNow());
    this.connect();
  }

  private stop(): void {
    this.running = false;
    this.unsubscribeToken?.();
    this.unsubscribeToken = null;
    this.clearRetry();
    this.closeSocket();
  }

  private update(patch: Partial<LiveSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((fn) => fn());
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private armWatchdog(): void {
    if (this.watchdogTimer !== null) window.clearTimeout(this.watchdogTimer);
    this.watchdogTimer = window.setTimeout(() => {
      this.update({ lastError: `No status from backend for ${STATUS_WATCHDOG_MS / 1000} s` });
      this.closeSocket();
      this.scheduleReconnect();
    }, STATUS_WATCHDOG_MS);
  }

  private closeSocket(): void {
    if (this.watchdogTimer !== null) window.clearTimeout(this.watchdogTimer);
    this.watchdogTimer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
    // Drop everything we knew: it can no longer be verified as current.
    this.update({ connection: 'closed', status: null, statusReceivedAtMs: null, signals: EMPTY_SIGNALS });
  }

  private scheduleReconnect(): void {
    if (!this.running) return;
    this.clearRetry();
    const attempt = this.snapshot.attempt + 1;
    const delay = backoffDelayMs(attempt - 1);
    this.update({ attempt, nextRetryAtMs: Date.now() + delay });
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  private connect(): void {
    if (!this.running) return;
    this.update({ connection: 'connecting', nextRetryAtMs: null });
    const token = readToken();
    let protocols: string[] | undefined;
    if (token) {
      // Browsers cannot set an Authorization header on a WebSocket; the token
      // travels as a subprotocol instead of a URL query (URLs end up in logs).
      if (!SUBPROTOCOL_SAFE.test(token)) {
        this.update({
          connection: 'closed',
          lastError: 'The API token contains characters that cannot be sent over a WebSocket (use A-Z a-z 0-9 . _ ~ -).',
        });
        this.scheduleReconnect();
        return;
      }
      protocols = ['roomsense.v1', `bearer.${token}`];
    }
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl(), protocols);
    } catch (err) {
      this.update({ connection: 'closed', lastError: err instanceof Error ? err.message : String(err) });
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.update({ connection: 'open', lastError: null });
      this.armWatchdog();
    };
    ws.onmessage = (ev: MessageEvent) => this.onMessage(ev.data);
    ws.onerror = () => {
      this.update({ lastError: 'WebSocket error (backend not reachable at /api/ws)' });
    };
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      const why = ev.reason ? `${ev.code} ${ev.reason}` : `code ${ev.code}`;
      this.closeSocket();
      this.update({ lastError: this.snapshot.lastError ?? `WebSocket closed (${why})` });
      this.scheduleReconnect();
    };
  }

  private onMessage(raw: unknown): void {
    const msg = parseWsMessage(raw);
    if (!msg) return; // malformed frames are dropped, never guessed at
    const now = Date.now();
    if (msg.type === 'status') {
      this.armWatchdog();
      // Prune snapshots for links the backend no longer reports.
      const live = new Set(msg.data.links.map((l) => l.link_id));
      let signals = this.snapshot.signals;
      if ([...signals.keys()].some((k) => !live.has(k))) {
        const next = new Map<string, SignalEntry>();
        signals.forEach((v, k) => {
          if (live.has(k)) next.set(k, v);
        });
        signals = next;
      }
      this.update({ status: msg.data, statusReceivedAtMs: now, signals, attempt: 0, lastError: null });
    } else {
      const next = new Map(this.snapshot.signals);
      next.set(msg.link_id, { data: msg.data, receivedAtMs: now });
      while (next.size > MAX_SIGNAL_LINKS) {
        const oldest = next.keys().next();
        if (oldest.done) break;
        next.delete(oldest.value);
      }
      this.update({ signals: next });
    }
  }
}

export const liveConnection = new LiveConnection();

/** Latest SystemStatus + per-link SignalSnapshot, pushed by the backend. */
export function useLiveStatus(): LiveSnapshot {
  return useSyncExternalStore(liveConnection.subscribe, liveConnection.getSnapshot, liveConnection.getSnapshot);
}
