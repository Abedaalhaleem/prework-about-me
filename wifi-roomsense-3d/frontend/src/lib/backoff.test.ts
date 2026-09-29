import { describe, expect, it } from 'vitest';
import { backoffDelayMs } from './backoff';

describe('backoffDelayMs', () => {
  const noJitter = { jitter: 0 };
  it('grows exponentially and is capped', () => {
    expect(backoffDelayMs(0, noJitter)).toBe(500);
    expect(backoffDelayMs(1, noJitter)).toBe(1000);
    expect(backoffDelayMs(3, noJitter)).toBe(4000);
    expect(backoffDelayMs(50, noJitter)).toBe(15000);
  });
  it('keeps jitter within bounds', () => {
    for (const r of [0, 0.5, 0.999]) {
      const d = backoffDelayMs(2, { jitter: 0.2, random: () => r });
      expect(d).toBeGreaterThanOrEqual(1600);
      expect(d).toBeLessThanOrEqual(2400);
    }
  });
});
