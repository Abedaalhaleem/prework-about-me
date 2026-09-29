// @vitest-environment jsdom
/**
 * Smoke test: mount the whole app in jsdom with a fake WebSocket and a fetch
 * that fails, and check the honesty-critical banners in the rendered DOM.
 * jsdom has no WebGL, so this also exercises the "3-D view unavailable" path.
 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { makeActivity, makeLink, makeStatus } from './test/statusFixture';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  readyState = 0;
  readonly url: string;
  readonly protocols: string | string[] | undefined;
  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = protocols;
    FakeWebSocket.instances.push(this);
  }
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  serverPush(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent);
  }
  serverClose(): void {
    this.readyState = 3;
    this.onclose?.({ code: 1006, reason: '' } as CloseEvent);
  }
}

let container: HTMLDivElement;
let root: Root;

beforeAll(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  // Every HTTP call fails: the UI must show errors, never substitute data.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ detail: 'backend not running in this test' }), { status: 503 })),
  );
  // jsdom logs "not implemented" for canvas; keep the output readable.
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const { App } = await import('./App');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<App />);
  });
});

afterAll(async () => {
  await act(async () => root.unmount());
  vi.unstubAllGlobals();
});

const text = (): string => container.textContent ?? '';

describe('App smoke (jsdom)', () => {
  it('shows NO CONNECTION TO BACKEND before any status arrives', () => {
    expect(FakeWebSocket.instances.length).toBeGreaterThan(0);
    expect(FakeWebSocket.instances[0]?.url).toMatch(/\/api\/ws$/);
    expect(text()).toContain('NO CONNECTION TO BACKEND');
    expect(text()).not.toContain('SIMULATED DATA — NOT A MEASUREMENT');
  });

  it('shows the large SIMULATED banner and the 3-D watermark for simulated status', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    const status = makeStatus({
      simulated: true,
      source_mode: 'SIMULATION',
      source_banner: 'SIMULATION',
      links: [makeLink('tx1->rx1')],
      activity: [makeActivity('tx1->rx1', 'MOTION_DETECTED')],
    });
    await act(async () => {
      ws.serverOpen();
      ws.serverPush({ type: 'status', data: status });
    });
    expect(container.querySelector('.sim-banner')?.textContent).toContain('SIMULATED DATA — NOT A MEASUREMENT');
    expect(container.querySelector('.sim-watermark')?.textContent).toContain('SIMULATED DATA — NOT A MEASUREMENT');
    expect(container.querySelector('.source-pill__text')?.textContent).toBe('SIMULATION');
    expect(text()).toContain('HARDWARE REQUIRED');
    expect(text()).toContain('measurement age');
    expect(text()).toContain('Through-wall: UNVERIFIED');
    expect(text()).not.toContain('NO CONNECTION TO BACKEND');
    // No WebGL in jsdom: the view says so instead of failing silently.
    expect(text()).toContain('3-D view unavailable');
  });

  it('labels simulated links in the Source card as simulated, never as a connected board', () => {
    const list = container.querySelector('.origin-list')?.textContent ?? '';
    expect(list).toContain('simulated link — no board');
    expect(list).not.toMatch(/\bconnected\b/);
    expect(text()).not.toContain('Receivers reported by the backend');
  });

  it('keeps the header compact: facts always visible, explanations behind one summary line', () => {
    const facts = container.querySelector('.status-facts')?.textContent ?? '';
    expect(facts).toContain('Calibration: NOT VALID');
    expect(facts).toContain('Localization');
    expect(facts).toContain('25.0 Hz');
    expect(facts).toContain('quality GOOD');
    expect(facts).toContain('Enabled now: none');
    expect(facts).not.toContain('Unsupported: none');
    const summary = container.querySelector('.status-more > summary')?.textContent ?? '';
    expect(summary).toContain('Through-wall: UNVERIFIED');
    expect(summary).toContain('Operating scope');
    const more = container.querySelector('details.status-more') as HTMLDetailsElement | null;
    expect(more?.open).toBe(false);
  });

  it('shows RECORDED REPLAY and the SIMULATED banner for a replay of simulated data, without a warning', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    const status = makeStatus({
      simulated: true,
      source_mode: 'REPLAY',
      source_banner: 'RECORDED REPLAY',
      source_state: 'FINISHED',
      links: [makeLink('tx1->rx1', { connected: false })],
    });
    await act(async () => {
      ws.serverPush({ type: 'status', data: status });
    });
    expect(container.querySelector('.source-pill__text')?.textContent).toBe('RECORDED REPLAY');
    expect(container.querySelector('.source-pill__origin')?.textContent).toBe('SIMULATED DATA');
    expect(container.querySelector('.sim-banner')?.textContent).toContain('SIMULATED DATA — NOT A MEASUREMENT');
    expect(container.querySelector('.sim-watermark')).not.toBeNull();
    expect(container.querySelector('.status-header__warning')).toBeNull();
    expect(text()).not.toContain('SIMULATION FINISHED');
    expect(container.querySelector('.origin-list')?.textContent).toContain('replayed link — simulated data, no board');
  });

  it('says NO SOURCE once and disables Stop source when nothing runs', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    await act(async () => {
      ws.serverPush({
        type: 'status',
        data: makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE', session_id: null }),
      });
    });
    const pill = container.querySelector('.source-pill')?.textContent ?? '';
    expect(pill).toBe('NO SOURCE');
    const stop = [...container.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent === 'Stop source');
    expect(stop?.disabled).toBe(true);
    // The configured receivers could not be listed (fetch fails here): unknown, so Start live stays enabled.
    const start = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => b.textContent === 'Start live (configured receivers)',
    );
    expect(start?.disabled).toBe(false);
    expect(text()).toContain('Could not list the configured receivers');
  });

  it('never shows a green LIVE pill while the live source is disconnected', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    await act(async () => {
      ws.serverPush({
        type: 'status',
        data: makeStatus({ source_state: 'DISCONNECTED', links: [makeLink('tx1->rx1', { connected: false, last_frame_age_s: 40 })] }),
      });
    });
    const pill = container.querySelector('.source-pill');
    expect(pill?.className).toContain('source-pill--live-down');
    expect(pill?.className).not.toMatch(/source-pill--live(\s|$)/);
    expect(pill?.textContent).toBe('LIVE MEASUREMENTS— DISCONNECTED (no data)');
    expect(container.querySelector('.origin-list')?.textContent).toContain('not connected');
    const stop = [...container.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent === 'Stop source');
    expect(stop?.disabled).toBe(false);
  });

  it('labels a live status LIVE MEASUREMENTS without the simulated banner', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    await act(async () => {
      ws.serverPush({ type: 'status', data: makeStatus({ links: [makeLink('tx1->rx1')] }) });
    });
    expect(container.querySelector('.source-pill__text')?.textContent).toBe('LIVE MEASUREMENTS');
    expect(container.querySelector('.sim-banner')).toBeNull();
    expect(container.querySelector('.sim-watermark')).toBeNull();
  });

  it('drops the status and shows NO CONNECTION when the socket closes', async () => {
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1] as FakeWebSocket;
    await act(async () => {
      ws.serverClose();
    });
    expect(text()).toContain('NO CONNECTION TO BACKEND');
    expect(container.querySelector('.source-pill__text')?.textContent).toBe('NO CONNECTION TO BACKEND');
    expect(text()).not.toContain('LIVE MEASUREMENTS');
  });

  it('surfaces the HTTP error for the room geometry instead of inventing one', () => {
    expect(text()).toContain('Room geometry could not be loaded');
    expect(text()).toContain('HTTP 503');
    expect(text()).toContain('NO ROOM GEOMETRY LOADED');
  });
});
