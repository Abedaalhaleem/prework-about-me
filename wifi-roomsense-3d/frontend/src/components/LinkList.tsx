/**
 * Per-link table: backend link status + latest activity result. Every number
 * is the backend's; missing values say "unavailable". The score is labelled
 * as a heuristic, never as a probability.
 *
 * Two ages are shown: "measurement age" (newest frame) and "state age" (the
 * data behind the displayed state). The state goes STALE on its own age, so
 * fresh frames never make an old state look current.
 */

import type { SystemStatus } from '../api/types';
import { formatAge, isStale, linkDisplayState, linkFreshness, statusLinkIds } from '../lib/freshness';
import { fmtInt, fmtNum, fmtPercent } from '../lib/format';
import { linkStyle, qualityCss } from '../lib/linkStyle';

export function LinkList({
  status,
  receivedAtMs,
  now,
  selected,
  onSelect,
}: {
  status: SystemStatus | null;
  receivedAtMs: number | null;
  now: number;
  selected: string | null;
  onSelect: (linkId: string) => void;
}) {
  if (!status || receivedAtMs === null) {
    return <p className="muted">No backend connection: no link data.</p>;
  }
  const ids = statusLinkIds(status);
  if (ids.length === 0) {
    return (
      <p className="muted">
        No links. Start a source (Live needs configured receivers; Replay needs a recording; Simulation is labelled as such).
      </p>
    );
  }
  const fresh = linkFreshness(status, receivedAtMs, now);
  const timeout = status.stale_clear_timeout_s;
  return (
    <div className="link-list">
      {ids.map((id) => {
        const link = status.links.find((l) => l.link_id === id);
        const act = status.activity.find((a) => a.link_id === id);
        const f = fresh.get(id);
        const age = f?.measurementAgeS ?? null;
        const stateAge = f?.stateAgeS ?? null;
        const state = linkDisplayState(act, stateAge, timeout, true, age);
        const st = linkStyle(state);
        const q = act?.quality;
        return (
          // A div (not a button) because the card holds block content; the
          // header button is the keyboard-accessible selector.
          <div key={id} className={`link-card ${selected === id ? 'link-card--selected' : ''}`} onClick={() => onSelect(id)}>
            <div className="link-card__head">
              <button
                type="button"
                className="link-card__select"
                aria-pressed={selected === id}
                title="Show this link's signals"
                onClick={(e) => {
                  e.stopPropagation();
                  onSelect(id);
                }}
              >
                <code className="link-card__id">{id}</code>
              </button>
              <span className="state-chip" style={{ borderColor: st.css, color: st.css }} title={st.description}>
                {st.short}
              </span>
            </div>
            <dl className="link-card__grid">
              <div>
                <dt title="Time since the newest measured frame on this link">measurement age</dt>
                <dd className={isStale(age, timeout) ? 'age-stale' : undefined}>{formatAge(age)}</dd>
              </div>
              <div>
                <dt title="Age of the data behind the displayed state (window end of the latest result)">state age</dt>
                <dd className={act && isStale(stateAge, timeout) ? 'age-stale' : undefined}>
                  {act ? formatAge(stateAge) : 'no result yet'}
                  {act && isStale(stateAge, timeout) && <span className="muted"> (older than {timeout} s: not shown as current)</span>}
                </dd>
              </div>
              <div>
                <dt title="Unitless heuristic, not a probability">score (heuristic)</dt>
                <dd>
                  {state === 'STALE' || state === 'NO_DATA' ? 'not current' : fmtNum(act?.activity_score ?? null, 2)}
                  {act && (
                    <span className="muted">
                      {' '}
                      / enter {act.enter_threshold} · exit {act.exit_threshold}
                    </span>
                  )}
                </dd>
              </div>
              <div>
                <dt>measured rate</dt>
                <dd>
                  {fmtNum(link?.acquisition_rate_hz ?? null, 1, 'Hz')}
                  {q?.expected_rate_hz != null && <span className="muted"> (configured {fmtNum(q.expected_rate_hz, 0, 'Hz')})</span>}
                </dd>
              </div>
              <div>
                <dt>quality</dt>
                <dd style={{ color: qualityCss(q?.level) }}>{q?.level ?? 'unavailable'}</dd>
              </div>
              <div>
                <dt>loss</dt>
                <dd>{fmtPercent(q?.loss_fraction ?? null)}</dd>
              </div>
              <div>
                <dt>RSSI (median)</dt>
                <dd>{fmtNum(q?.rssi_dbm_median ?? null, 0, 'dBm')}</dd>
              </div>
              <div>
                <dt>frames / rejected</dt>
                <dd>
                  {fmtInt(link?.frames_total ?? null)} / {fmtInt(link?.frames_rejected ?? null)}
                </dd>
              </div>
              <div>
                <dt>parse errors / fw drops</dt>
                <dd>
                  {fmtInt(link?.parse_errors ?? null)} / {fmtInt(link?.firmware_drops ?? null)}
                </dd>
              </div>
              <div>
                <dt>channel · layout</dt>
                <dd>
                  {link?.channel ?? 'unavailable'} · {link?.layout_id ?? 'unavailable'}
                </dd>
              </div>
              <div>
                <dt>connected</dt>
                <dd>{link ? (link.connected ? 'yes' : 'no') : 'unavailable'}</dd>
              </div>
            </dl>
            {act && act.reasons.length > 0 && (
              <ul className="link-card__reasons">
                {act.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
            {q && q.flags.length > 0 && <div className="link-card__flags">flags: {q.flags.join(', ')}</div>}
          </div>
        );
      })}
    </div>
  );
}
