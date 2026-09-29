import { describe, expect, it } from 'vitest';
import {
  NO_RECEIVERS_TEXT,
  describeReceiver,
  linkOrigin,
  parseConfiguredReceivers,
  startLiveGate,
  stopSourceGate,
} from './sourceControls';
import { makeLink, makeStatus } from '../test/statusFixture';

const RX = {
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
};

describe('linkOrigin (links of the running source)', () => {
  it('labels simulated links as simulated, never as connected', () => {
    const status = makeStatus({ source_mode: 'SIMULATION', source_banner: 'SIMULATION', simulated: true });
    const o = linkOrigin(status, makeLink('tx1->rx1', { connected: true }));
    expect(o.text).toBe('simulated link — no board');
    expect(o.text).not.toMatch(/\bconnected\b/);
    expect(o.tone).toBe('sim');
  });

  it('labels replayed links, and says when the replayed data is simulated', () => {
    const replay = makeStatus({ source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY' });
    expect(linkOrigin(replay, makeLink('tx1->rx1')).text).toBe('replayed link — recorded earlier, not live');
    const replaySim = makeStatus({ source_mode: 'REPLAY', source_banner: 'RECORDED REPLAY', simulated: true });
    const o = linkOrigin(replaySim, makeLink('tx1->rx1', { connected: true }));
    expect(o.text).toBe('replayed link — simulated data, no board');
    expect(o.text).not.toMatch(/\bconnected\b/);
  });

  it('says "connected" only for LIVE links the backend reports connected', () => {
    const live = makeStatus();
    expect(linkOrigin(live, makeLink('tx1->rx1', { connected: true }))).toEqual({ text: 'connected', tone: 'ok' });
    expect(linkOrigin(live, makeLink('tx1->rx1', { connected: false }))).toEqual({ text: 'not connected', tone: 'warn' });
    // Fail safe: a LIVE status flagged simulated never shows a "connected" board.
    const odd = makeStatus({ simulated: true });
    expect(linkOrigin(odd, makeLink('tx1->rx1', { connected: true })).tone).toBe('sim');
  });
});

describe('startLiveGate', () => {
  it('is disabled with an explanation when the backend reports no configured receivers', () => {
    const g = startLiveGate({ kind: 'list', receivers: [] });
    expect(g.enabled).toBe(false);
    expect(g.reason).toBe(NO_RECEIVERS_TEXT);
    expect(NO_RECEIVERS_TEXT).toBe('No receivers configured — add them in configs/roomsense.toml or use Override below');
  });

  it('stays enabled when receivers are configured or when it is unknown (older backend, error, loading)', () => {
    expect(startLiveGate({ kind: 'list', receivers: [RX] }).enabled).toBe(true);
    expect(startLiveGate({ kind: 'unsupported' }).enabled).toBe(true);
    expect(startLiveGate({ kind: 'error', message: 'HTTP 500' }).enabled).toBe(true);
    expect(startLiveGate({ kind: 'loading' }).enabled).toBe(true);
  });
});

describe('stopSourceGate', () => {
  it('is disabled only when the backend says no source is selected', () => {
    const none = stopSourceGate(makeStatus({ source_mode: null, source_banner: 'NO SOURCE', source_state: 'NO_SOURCE' }));
    expect(none.enabled).toBe(false);
    expect(none.reason).toMatch(/No source/);
    for (const state of ['RUNNING', 'CONNECTING', 'DISCONNECTED', 'FINISHED', 'ERROR'] as const) {
      expect(stopSourceGate(makeStatus({ source_state: state })).enabled).toBe(true);
    }
    expect(stopSourceGate(null).enabled).toBe(true); // unknown: let the backend decide
  });
});

describe('parseConfiguredReceivers', () => {
  it('accepts the documented array (and a {receivers} wrapper)', () => {
    expect(parseConfiguredReceivers([RX])).toEqual([RX]);
    expect(parseConfiguredReceivers({ receivers: [RX] })).toEqual([RX]);
    expect(parseConfiguredReceivers([])).toEqual([]);
  });

  it('fills missing optional fields with null, never with invented values', () => {
    const [r] = parseConfiguredReceivers([{ receiver_id: 'rx2', port: 'COM5' }]) ?? [];
    expect(r).toEqual({
      receiver_id: 'rx2',
      port: 'COM5',
      baud: null,
      input_format: null,
      transmitter_id: null,
      transmitter_mac: null,
      declared_chip: null,
      declared_board: null,
      ltf_config: null,
      link_id: null,
    });
  });

  it('rejects unexpected shapes instead of reading them as "none configured"', () => {
    expect(parseConfiguredReceivers(null)).toBeNull();
    expect(parseConfiguredReceivers({ detail: 'Not Found' })).toBeNull();
    expect(parseConfiguredReceivers('<!doctype html>')).toBeNull();
    expect(parseConfiguredReceivers([{ port: '/dev/ttyUSB0' }])).toBeNull();
    expect(parseConfiguredReceivers([RX, 42])).toBeNull();
  });
});

describe('describeReceiver', () => {
  it('lists only what is configured and marks declared identity as declared', () => {
    expect(describeReceiver(RX)).toBe('roomsense-rscsi-v1 · 921600 baud');
    expect(
      describeReceiver({ ...RX, declared_chip: 'esp32s3', declared_board: 'DevKitC-1U', ltf_config: 'lltf_only', transmitter_mac: 'aa:bb:cc:dd:ee:ff' }),
    ).toBe('roomsense-rscsi-v1 · 921600 baud · chip esp32s3 (declared) · board DevKitC-1U (declared) · LTF lltf_only · TX MAC aa:bb:cc:dd:ee:ff');
  });
});
