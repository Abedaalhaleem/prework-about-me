/**
 * Permanent header shown on every page. It is the single place that says
 * where the data comes from, how old it is and what the system can and
 * cannot claim. All values come from the backend status; ages are computed
 * client-side so they keep growing if updates stop.
 *
 * Compact on purpose, so the 3-D view, links and plots stay in view:
 *  - sticky: the SIMULATED DATA banner (when applicable), the connection
 *    banner, and one row with the source pill, measurement age and the
 *    capability chips;
 *  - always visible below it: measured rate and quality per link,
 *    calibration validity, localization status and what is enabled now;
 *  - one summary line (through-wall status, operating scope, number of
 *    claims never made) that expands on click to the full explanations.
 */

import type { SystemStatus } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import {
  SIMULATED_BANNER_TEXT,
  calibrationInfo,
  capabilitySummaryText,
  reasonCode,
  sourceBanner,
  sourcePillText,
  throughWallInfo,
} from '../lib/banner';
import { formatAge, isStale, linkFreshness, newestMeasurementAgeS } from '../lib/freshness';
import { fmtNum } from '../lib/format';
import { qualityCss } from '../lib/linkStyle';
import { CapabilityChips } from './CapabilityChips';

export function SimulatedBanner({ detail }: { detail: string }) {
  return (
    <div className="sim-banner" role="alert" aria-live="assertive">
      <span className="sim-banner__text">{SIMULATED_BANNER_TEXT}</span>
      <span className="sim-banner__sub">{detail}</span>
    </div>
  );
}

function NoConnectionBanner({ live, now }: { live: LiveSnapshot; now: number }) {
  const retryIn = live.nextRetryAtMs !== null ? Math.max(0, (live.nextRetryAtMs - now) / 1000) : null;
  return (
    <div className="offline-banner" role="alert">
      <span className="offline-banner__text">NO CONNECTION TO BACKEND</span>
      <span className="offline-banner__sub">
        {live.connection === 'connecting'
          ? 'Connecting to /api/ws …'
          : live.connection === 'open'
            ? 'Socket open; waiting for the first status message …'
            : retryIn !== null
            ? `Retrying in ${retryIn.toFixed(0)} s (attempt ${live.attempt}).`
            : 'Waiting to reconnect.'}{' '}
        No data is shown until the backend answers. {live.lastError ? `Last error: ${live.lastError}` : ''}
      </span>
    </div>
  );
}

/** Measured acquisition rate and data quality per link. */
function LinkFacts({ status, receivedAt, now }: { status: SystemStatus; receivedAt: number; now: number }) {
  const fresh = linkFreshness(status, receivedAt, now);
  const timeout = status.stale_clear_timeout_s;
  if (fresh.size === 0) return <span className="muted">none reported</span>;
  return (
    <ul className="link-rates">
      {[...fresh.entries()].map(([id, f]) => {
        const link = status.links.find((l) => l.link_id === id);
        const act = status.activity.find((a) => a.link_id === id);
        const level = act?.quality.level ?? null;
        const stale = isStale(f.measurementAgeS, timeout) || (act !== undefined && isStale(f.stateAgeS, timeout));
        return (
          <li key={id} className={`link-rate ${stale ? 'link-rate--stale' : ''}`}>
            <code>{id}</code>
            <span className="link-rate__rate" title="Measured acquisition rate over the last few seconds (not the configured rate)">
              {fmtNum(link?.acquisition_rate_hz ?? null, 1, 'Hz')}
            </span>
            <span className="link-rate__quality" style={{ color: qualityCss(level) }} title="Data quality level of the latest window">
              quality {level ?? 'unavailable'}
            </span>
            {stale && <span className="link-rate__stale">stale</span>}
          </li>
        );
      })}
    </ul>
  );
}

/** The facts that must never be collapsed away. */
function StatusFacts({ status, receivedAt, now }: { status: SystemStatus; receivedAt: number; now: number }) {
  const cal = calibrationInfo(status);
  const code = reasonCode(cal.detail);
  return (
    <div className="status-facts">
      <div className="status-facts__item">
        <span className="status-facts__label" title="Measured acquisition rate · data quality, per link">
          Links
        </span>
        <LinkFacts status={status} receivedAt={receivedAt} now={now} />
      </div>
      <div className="status-facts__item" title={cal.detail}>
        <span className={`pill ${cal.valid ? 'pill--ok' : 'pill--warn'}`}>{cal.label}</span>
        {code && <span className="status-facts__code">{code}</span>}
      </div>
      <div className="status-facts__item">
        <span className="status-facts__label">Localization</span>
        <span className="status-facts__text">{status.localization_status}</span>
      </div>
      <div className="status-facts__item">
        <span className="status-facts__label">Capabilities</span>
        <span className="status-facts__text">{capabilitySummaryText(status.capabilities)}</span>
      </div>
    </div>
  );
}

/** One line that expands on click: through-wall status, operating scope, claims never made. */
function StatusMore({ status }: { status: SystemStatus }) {
  const tw = throughWallInfo(status);
  const cal = calibrationInfo(status);
  const claims = status.unsupported_capabilities ?? [];
  return (
    <details className="status-more">
      <summary className="status-more__summary">
        <span className={`pill pill--${tw.severity}`}>{tw.label}</span>
        <span className="status-more__scope">
          <strong>Operating scope:</strong> {status.operating_scope}
        </span>
        {claims.length > 0 && (
          <span className="status-more__claims">
            Never claimed (out of scope): {claims.length}
          </span>
        )}
        <span className="status-more__toggle" aria-hidden="true" />
      </summary>
      <dl className="status-more__body">
        <div>
          <dt>
            <span className={`pill pill--${tw.severity}`}>{tw.label}</span>
          </dt>
          <dd>{tw.detail ?? 'No further detail from the backend.'}</dd>
        </div>
        <div>
          <dt>Operating scope</dt>
          <dd>{status.operating_scope}</dd>
        </div>
        <div>
          <dt>{cal.label}</dt>
          <dd>{cal.detail}</dd>
        </div>
        {claims.length > 0 && (
          <div className="status-more__wide">
            <dt>Never claimed (out of scope)</dt>
            <dd>
              <ul className="status-more__claims-list">
                {claims.map((u) => (
                  <li key={u.id}>
                    {u.claim} <span className="muted">— {u.reason}</span>
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
    </details>
  );
}

export function StatusHeader({ live, now, onOpenCapabilities }: { live: LiveSnapshot; now: number; onOpenCapabilities: () => void }) {
  const connected = live.connection === 'open' && live.status !== null && live.statusReceivedAtMs !== null;
  const status = connected ? live.status : null;
  const receivedAt = live.statusReceivedAtMs ?? now;
  const banner = sourceBanner(status, connected);
  const newestAge = status ? newestMeasurementAgeS(status, receivedAt, now) : null;
  const ageStale = status ? isStale(newestAge, status.stale_clear_timeout_s) : true;

  return (
    <>
      {/* Sticky: source/simulation/connection state must stay visible while scrolling. */}
      <div className="header-sticky">
        {banner.showSimulatedBanner && <SimulatedBanner detail={banner.simulatedDetail} />}
        {!connected && <NoConnectionBanner live={live} now={now} />}
        <header className="status-header">
          <div className="status-header__row">
            <div className="brand">
              <span className="brand__name">WiFi RoomSense 3D</span>
              <span className="brand__tag">local · experimental</span>
            </div>
            <div className={`source-pill source-pill--${banner.severity}`} aria-label="Data source" title={sourcePillText(banner)}>
              <span className="source-pill__text">{banner.text}</span>
              {banner.originText && <span className="source-pill__origin">{banner.originText}</span>}
              {banner.stateText && <span className="source-pill__state">— {banner.stateText}</span>}
            </div>
            {status?.hardware_required && (
              <span className="badge badge--hw" title="Real ESP32 hardware is required for live measurements.">
                HARDWARE REQUIRED
              </span>
            )}
            {status?.recording_active && (
              <span className="badge badge--rec" title={`Recording ${status.recording_id ?? ''}`}>
                ● REC
              </span>
            )}
            <div
              className={`age ${ageStale ? 'age--stale' : ''}`}
              title="Time since the newest measured frame on any link (not the screen refresh rate)"
            >
              <span className="age__label">measurement age</span>
              <span className="age__value">{status ? formatAge(newestAge) : 'unavailable'}</span>
              {/* "unavailable" already says there is none; flag only an age that is too old. */}
              {status && ageStale && newestAge !== null && <span className="age__flag">stale</span>}
            </div>
            {status && <CapabilityChips capabilities={status.capabilities} onOpen={onOpenCapabilities} />}
          </div>
        </header>
      </div>
      {status && (
        <div className="status-details-bar">
          {(status.source_detail || banner.inconsistency) && (
            <div className="status-details-bar__source">
              {status.source_detail && <span className="status-header__source-detail">Source: {status.source_detail}</span>}
              {banner.inconsistency && (
                <span className="status-header__warning" role="alert">
                  ⚠ Inconsistent backend status: {banner.inconsistency}
                </span>
              )}
            </div>
          )}
          <StatusFacts status={status} receivedAt={receivedAt} now={now} />
          <StatusMore status={status} />
        </div>
      )}
    </>
  );
}
