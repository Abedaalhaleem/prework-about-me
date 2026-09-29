// @vitest-environment jsdom
/**
 * Smoke test for the Source card against GET /api/source/receivers:
 *  - [] (none configured): "Start live" is disabled and says why;
 *  - 404 (older backend without the endpoint): unknown, the button stays enabled;
 *  - a list: the receivers are shown and the button is enabled.
 * Every other request fails, so nothing else can be mistaken for data.
 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { makeStatus } from '../test/statusFixture';
import { SourceControls } from './SourceControls';

let container: HTMLDivElement;
let root: Root;

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function mount(receivers: Response): Promise<void> {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/source/receivers') return receivers.clone();
      return new Response(JSON.stringify({ detail: 'not available in this test' }), { status: 503 });
    }),
  );
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const status = makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE' });
  await act(async () => {
    root.render(<SourceControls status={status} />);
  });
  // Let the fetch promise chain settle.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

const button = (label: string): HTMLButtonElement | undefined =>
  [...container.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent === label);

describe('Source card and configured receivers (jsdom)', () => {
  it('disables Start live with an explanation when no receivers are configured', async () => {
    await mount(new Response('[]', { status: 200 }));
    const start = button('Start live (configured receivers)');
    expect(start?.disabled).toBe(true);
    expect(container.textContent).toContain(
      'No receivers configured — add them in configs/roomsense.toml or use Override below',
    );
    expect(start?.getAttribute('aria-describedby')).toBeTruthy();
    // Nothing runs: Stop is disabled too.
    expect(button('Stop source')?.disabled).toBe(true);
  });

  it('treats a 404 from an older backend as unknown and keeps Start live enabled', async () => {
    await mount(new Response(JSON.stringify({ detail: 'NOT_FOUND: unknown API route' }), { status: 404 }));
    expect(button('Start live (configured receivers)')?.disabled).toBe(false);
    expect(container.textContent).toContain('does not list its configured receivers');
    expect(container.textContent).not.toContain('No receivers configured');
    expect(container.textContent).not.toContain('Could not list the configured receivers');
  });

  it('lists the configured receivers and keeps Start live enabled', async () => {
    const rx = [
      {
        receiver_id: 'rx1',
        port: '/dev/ttyUSB0',
        baud: 921600,
        input_format: 'roomsense-rscsi-v1',
        transmitter_id: 'tx1',
        transmitter_mac: null,
        declared_chip: null,
        declared_board: null,
        ltf_config: null,
        link_id: 'tx1->rx1',
      },
    ];
    await mount(new Response(JSON.stringify(rx), { status: 200 }));
    expect(button('Start live (configured receivers)')?.disabled).toBe(false);
    const list = container.querySelector('.receiver-list')?.textContent ?? '';
    expect(list).toContain('rx1');
    expect(list).toContain('/dev/ttyUSB0');
    expect(list).toContain('921600 baud');
  });
});
