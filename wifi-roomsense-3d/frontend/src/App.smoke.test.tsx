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
