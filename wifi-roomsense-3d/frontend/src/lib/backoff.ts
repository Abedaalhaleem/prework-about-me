/**
 * Exponential reconnect backoff for the status WebSocket.
 *
 * Deterministic when `random` is fixed so it can be unit-tested. Jitter keeps
 * several open tabs from reconnecting in lock-step after a backend restart.
 */

export interface BackoffOptions {
  baseMs?: number;
  maxMs?: number;
  /** Fraction of the delay that is randomised, 0..1. */
  jitter?: number;
  random?: () => number;
}

export function backoffDelayMs(attempt: number, opts: BackoffOptions = {}): number {
  const base = opts.baseMs ?? 500;
  const max = opts.maxMs ?? 15_000;
  const jitter = Math.min(Math.max(opts.jitter ?? 0.2, 0), 1);
  const random = opts.random ?? Math.random;
  const n = Math.max(0, Math.floor(attempt));
  // Cap the exponent so 2**n never overflows for long outages.
  const raw = Math.min(max, base * 2 ** Math.min(n, 20));
  const spread = raw * jitter;
  const delay = raw - spread + spread * 2 * random();
  return Math.round(Math.min(max, Math.max(base * (1 - jitter), delay)));
}
