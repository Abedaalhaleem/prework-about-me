// @vitest-environment jsdom
/**
 * Smoke test with data: a room (labelled EXAMPLE), a live status with one
 * link and a signal snapshot containing gap markers. Then every page is
 * opened once to catch runtime errors. Endpoints other than /api/room fail,
 * so every page must show an error instead of inventing content.
 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import type { RoomGeometry, SignalSnapshot } from './api/types';
import { T0_NS, makeActivity, makeLink, makeStatus } from './test/statusFixture';

class FakeWebSocket {
  static last: FakeWebSocket | null = null;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  constructor() {
    FakeWebSocket.last = this;
  }
  send(): void {}
  close(): void {}
}

// TEST FIXTURE: a tiny room, served only by the mocked fetch below.
const ROOM: RoomGeometry = {
  geometry_id: 'fixture',
  provenance: 'EXAMPLE',
  name: 'Fixture room',
  width_m: 4,
  depth_m: 3,
  height_m: 2.5,
  walls: [
    { id: 'w1', start: { x: 0, y: 0 }, end: { x: 4, y: 0 }, height_m: 2.5, thickness_m: 0.1, material: null, is_target_room_boundary: true },
  ],
  doors: [{ id: 'd1', wall_id: 'w1', offset_m: 1, width_m: 0.9, height_m: 2 }],
  nodes: [
    { id: 'tx1', role: 'TX', label: 'tx', position: { x: 0.5, y: 0.5, z: 1 }, inside_target_room: true, device_mac: null },
    { id: 'rx1', role: 'RX', label: 'rx', position: { x: 3.5, y: 2.5, z: 1 }, inside_target_room: true, device_mac: null },
  ],
  links: [{ link_id: 'tx1->rx1', transmitter_id: 'tx1', receiver_id: 'rx1' }],
  zones: [{ id: 'z1', label: 'Desk', kind: 'TARGET_ROOM_ZONE', polygon: [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 1.5 }] }],
  target_room_polygon: [{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 4, y: 3 }, { x: 0, y: 3 }],
  notes: null,
};

const nowMs = T0_NS / 1e6;
const SNAP: SignalSnapshot = {
  link_id: 'tx1->rx1',
  source_mode: 'LIVE',
  seconds: 60,
  score: { t: [nowMs - 3000, nowMs - 2000, nowMs - 1000], v: [1, null, 5], state: ['NO_MOTION_DETECTED', null, 'MOTION_DETECTED'] },
  enter_threshold: 4,
  exit_threshold: 2.5,
  rate_hz: { t: [nowMs - 3000, nowMs - 1000], v: [25, 24] },
  rssi_dbm: { t: [nowMs - 3000, nowMs - 1000], v: [-50, null] },
  amplitude: { t: [nowMs - 3000, nowMs - 2000, nowMs - 1000], k: [-2, 2], v: [[1, 2], null, [3, 4]] },
  latest_profile: { t: nowMs - 1000, k: [-2, 2], amp: [3, 4] },
  gaps: [{ start: nowMs - 2500, end: nowMs - 1500 }],
};

let container: HTMLDivElement;
let root: Root;
const text = (): string => container.textContent ?? '';

beforeAll(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/room') return new Response(JSON.stringify(ROOM), { status: 200 });
      return new Response(JSON.stringify({ detail: 'not available in this test' }), { status: 503 });
    }),
  );
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  window.location.hash = '';
  const { App } = await import('./App');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<App />);
  });
  const ws = FakeWebSocket.last as FakeWebSocket;
  const status = makeStatus({
    links: [makeLink('tx1->rx1', { last_frame_age_s: 0.2 })],
    activity: [makeActivity('tx1->rx1', 'MOTION_DETECTED', T0_NS - 0.3e9, { activity_score: 5.2 })],
    unsupported_capabilities: [{ id: 'count', claim: 'People counting', reason: 'single-person scope' }],
  });
  await act(async () => {
    ws.onopen?.(new Event('open'));
    ws.onmessage?.({ data: JSON.stringify({ type: 'status', data: status }) } as MessageEvent);
    ws.onmessage?.({ data: JSON.stringify({ type: 'signal', link_id: 'tx1->rx1', data: SNAP }) } as MessageEvent);
  });
});

afterAll(async () => {
  await act(async () => root.unmount());
  vi.unstubAllGlobals();
});

describe('Dashboard with data (jsdom)', () => {
  it('labels the geometry and shows per-link state, not a room-wide claim', () => {
    expect(container.querySelector('.geo-label')?.textContent).toContain('EXAMPLE GEOMETRY');
    expect(container.querySelector('.state-chip')?.textContent).toBe('MOTION');
    expect(text()).toContain('Link state (per link, not a map of the room)');
    expect(text()).toContain('Zone estimation DISABLED');
    expect(text()).not.toContain('Estimated zone (experimental):');
    expect(text()).toContain('People counting');
  });

  it('renders the plots with the heuristic-score caption', () => {
    expect(text()).toContain('Heuristic activity score (unitless) — not a probability');
    expect(text()).toContain('Amplitude over time (2 provided subcarriers)');
    // Axis labels are drawn on the canvas; the accessible label carries them too.
    const labels = [...container.querySelectorAll('.plot canvas')].map((c) => c.getAttribute('aria-label') ?? '');
    expect(labels.some((l) => l.includes('time relative to now (s)'))).toBe(true);
    expect(labels.some((l) => l.includes('RSSI (dBm)'))).toBe(true);
    // Score has 3 samples with a null in the middle: 2 valid samples in 2 unbroken segments.
    const summaries = [...container.querySelectorAll('.plot__summary')].map((e) => e.textContent);
    expect(summaries[0]).toBe('2 valid samples in 2 unbroken segment(s)');
    expect(text()).not.toMatch(/received -/);
  });

  it('opens every page without crashing and shows errors instead of data', async () => {
    const tabs = [...container.querySelectorAll<HTMLButtonElement>('.tabs__tab')];
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Dashboard',
      'Calibration',
      'Recordings & Replay',
      'Validation',
      'Capabilities & Research',
      'Hardware',
      'Settings',
    ]);
    for (const tab of tabs) {
      await act(async () => {
        tab.click();
      });
      // The permanent header is present on every page.
      expect(container.querySelector('.source-pill__text')?.textContent).toBe('LIVE MEASUREMENTS');
    }
    // Last page visited is Settings; go back through a few and check error surfacing.
    await act(async () => tabs[2]?.click());
    expect(text()).toContain('All people present have been informed and agreed');
    expect(text()).toContain('Could not list recordings');
    await act(async () => tabs[3]?.click());
    expect(text()).toContain('Could not load the protocol');
    await act(async () => tabs[4]?.click());
    expect(text()).toContain('through-wall-validated');
    expect(text()).toContain('EXPERIMENTAL');
    await act(async () => tabs[5]?.click());
    expect(text()).toContain('HARDWARE REQUIRED');
    expect(text()).toContain('Hardware inspection failed');
    await act(async () => tabs[1]?.click());
    expect(text()).toContain('EXAMPLE GEOMETRY (sample data — not your room)');
  });
});
