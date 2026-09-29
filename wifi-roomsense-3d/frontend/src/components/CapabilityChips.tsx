import { useId } from 'react';
import type { CapabilityStatus } from '../api/types';
import { capabilityChips } from '../lib/banner';

/**
 * A/B/C/D capability chips. Each chip shows the backend state; hovering or
 * focusing it reveals the reasons. Nothing is inferred client-side.
 */
export function CapabilityChips({ capabilities, onOpen }: { capabilities: readonly CapabilityStatus[]; onOpen?: () => void }) {
  const baseId = useId();
  return (
    <ul className="cap-chips" aria-label="Capabilities">
      {capabilityChips(capabilities).map((chip) => {
        const tipId = `${baseId}-${chip.letter}`;
        return (
          <li key={chip.id} className="cap-chip-wrap">
            <button
              type="button"
              className={`cap-chip cap-chip--${chip.severity}`}
              aria-describedby={tipId}
              onClick={onOpen}
            >
              <span className="cap-chip__letter">{chip.letter}</span>
              <span className="cap-chip__state">{chip.stateLabel}</span>
            </button>
            <div role="tooltip" id={tipId} className="tooltip">
              <strong>
                {chip.letter} · {chip.name}
              </strong>
              <div className="tooltip__state">State: {chip.stateLabel}</div>
              {chip.reasons.length > 0 ? (
                <ul>
                  {chip.reasons.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              ) : (
                <div className="muted">No reasons given by the backend.</div>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
