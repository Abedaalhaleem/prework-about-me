import { describe, expect, it } from 'vitest';
import { parseWsMessage } from './messages';
import { makeStatus } from '../test/statusFixture';

describe('parseWsMessage', () => {
  it('accepts status and signal messages', () => {
    const status = makeStatus({ simulated: true });
    const m = parseWsMessage(JSON.stringify({ type: 'status', data: status }));
    expect(m?.type).toBe('status');
    const snap = {
      link_id: 'tx1->rx1',
      source_mode: 'LIVE',
      seconds: 60,
      score: { t: [], v: [], state: [] },
      enter_threshold: 4,
      exit_threshold: 2.5,
      rate_hz: { t: [], v: [] },
      rssi_dbm: { t: [], v: [] },
      amplitude: { t: [], k: [], v: [] },
      latest_profile: { t: null, k: [], amp: [] },
      gaps: [],
    };
    const s = parseWsMessage(JSON.stringify({ type: 'signal', link_id: 'tx1->rx1', data: snap }));
    expect(s?.type === 'signal' && s.link_id).toBe('tx1->rx1');
  });
  it('drops malformed messages instead of guessing', () => {
    expect(parseWsMessage('not json')).toBeNull();
    expect(parseWsMessage(JSON.stringify({ type: 'status', data: { simulated: 'yes' } }))).toBeNull();
    expect(parseWsMessage(JSON.stringify({ type: 'other', data: {} }))).toBeNull();
    expect(parseWsMessage(42)).toBeNull();
  });
});
