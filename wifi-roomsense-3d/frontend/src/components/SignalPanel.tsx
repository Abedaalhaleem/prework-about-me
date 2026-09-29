/**
 * The five signal plots for one link, built from the backend SignalSnapshot.
 * Time axes are relative to the (extrapolated) server clock "now", so data
 * visibly slides left and out of view if updates stop.
 */

import type { SignalSnapshot } from '../api/types';
import type { SignalEntry } from '../api/ws';
import { formatAge } from '../lib/freshness';
import { linkStyle } from '../lib/linkStyle';
import {
  amplitudeSeries,
  autoMaxStep,
  gapsToRelativeSeconds,
  rampColor,
  stateBands,
  toRelativeSeconds,
} from '../lib/plotting';
import { SignalPlot } from './SignalPlot';

const SCORE_CAPTION = 'Heuristic activity score (unitless) — not a probability';

export function SignalPanel({
  entry,
  serverNowMs,
  clientNowMs,
}: {
  entry: SignalEntry | undefined;
  /** Server-clock "now" in Unix ms (see lib/freshness serverNowMs). */
  serverNowMs: number | null;
  clientNowMs: number;
}) {
  if (!entry || serverNowMs === null) {
    return (
      <div className="plots-empty">
        No signal snapshot has been received for this link. Plots appear only when the backend sends measured (or clearly
        labelled simulated/replayed) samples.
      </div>
    );
  }
  const snap: SignalSnapshot = entry.data;
  const now = serverNowMs;
  const seconds = Number.isFinite(snap.seconds) && snap.seconds > 0 ? snap.seconds : 60;
  const xDomain: [number, number] = [-seconds, 0];
  const gaps = gapsToRelativeSeconds(snap.gaps ?? [], now);
  const xLabel = 'time relative to now (s)';

  const scoreT = toRelativeSeconds(snap.score?.t ?? [], now);
  const bands = stateBands(scoreT, snap.score?.state ?? [], gaps).map((b) => ({
    start: b.start,
    end: b.end,
    color: linkStyle(b.state).css,
  }));
  const hLines = [
    ...(snap.enter_threshold !== null && Number.isFinite(snap.enter_threshold)
      ? [{ y: snap.enter_threshold, label: `enter ${snap.enter_threshold}`, color: '#ff9f43' }]
      : []),
    ...(snap.exit_threshold !== null && Number.isFinite(snap.exit_threshold)
      ? [{ y: snap.exit_threshold, label: `exit ${snap.exit_threshold}`, color: '#4dabf7' }]
      : []),
  ];

  const rateT = toRelativeSeconds(snap.rate_hz?.t ?? [], now);
  const rssiT = toRelativeSeconds(snap.rssi_dbm?.t ?? [], now);
  const amp = amplitudeSeries(snap.amplitude, 10);
  const profileK = snap.latest_profile?.k ?? [];
  const profileAge = snap.latest_profile?.t != null ? Math.max(0, (now - snap.latest_profile.t) / 1000) : null;
  // useNow() ticks every 500 ms, so a just-arrived snapshot can look "newer than now"; clamp.
  const snapshotAge = Math.max(0, (clientNowMs - entry.receivedAtMs) / 1000);

  return (
    <div className="plots">
      <div className="plots__meta">
        Snapshot for <code>{snap.link_id}</code> · source {snap.source_mode ?? 'unavailable'} · received {formatAge(snapshotAge)} ago ·
        hatched areas are gaps (no data; lines are not bridged)
      </div>
      <SignalPlot
        title="Activity score"
        caption={SCORE_CAPTION}
        xLabel={xLabel}
        yLabel="score (unitless)"
        xDomain={xDomain}
        lines={[{ label: 'score', color: '#e9ecef', x: scoreT, y: snap.score?.v ?? [] }]}
        gaps={gaps}
        hLines={hLines}
        bands={bands}
        maxStep={autoMaxStep(scoreT, 6)}
        emptyText="No score samples (e.g. no valid baseline or no data)"
      />
      <div className="plots__legend-states">
        State band:{' '}
        {(['MOTION_DETECTED', 'NO_MOTION_DETECTED', 'UNKNOWN', 'CALIBRATING', 'SENSOR_OFFLINE'] as const).map((s) => (
          <span key={s} className="legend-chip">
            <span className="swatch" style={{ background: linkStyle(s).css }} />
            {linkStyle(s).short}
          </span>
        ))}
      </div>
      <div className="plots__grid">
        <SignalPlot
          title="Measured packet rate"
          xLabel={xLabel}
          yLabel="rate (Hz)"
          xDomain={xDomain}
          lines={[{ label: 'rate', color: '#63e6be', x: rateT, y: snap.rate_hz?.v ?? [] }]}
          gaps={gaps}
          maxStep={autoMaxStep(rateT, 6)}
          height={150}
        />
        <SignalPlot
          title="RSSI"
          xLabel={xLabel}
          yLabel="RSSI (dBm)"
          xDomain={xDomain}
          lines={[{ label: 'rssi', color: '#b197fc', x: rssiT, y: snap.rssi_dbm?.v ?? [] }]}
          gaps={gaps}
          maxStep={autoMaxStep(rssiT, 6)}
          height={150}
          emptyText="RSSI not reported"
        />
      </div>
      <SignalPlot
        title={`Amplitude over time (${amp.length} provided subcarrier${amp.length === 1 ? '' : 's'})`}
        xLabel={xLabel}
        yLabel="|CSI| (arb. units)"
        xDomain={xDomain}
        lines={amp.map((s, i) => ({
          label: `k=${s.k}`,
          color: rampColor(i, amp.length),
          x: toRelativeSeconds(s.t, now),
          y: s.v,
        }))}
        gaps={gaps}
        maxStep={autoMaxStep(toRelativeSeconds(snap.amplitude?.t ?? [], now), 6)}
        legend
        emptyText="No amplitude samples (or an amplitude matrix this UI cannot interpret safely)"
      />
      <SignalPlot
        title={`Latest amplitude profile${profileAge !== null ? ` (${formatAge(profileAge)} old)` : ''}`}
        xLabel="subcarrier index k"
        yLabel="|CSI| (arb. units)"
        xDomain={
          profileK.length > 0
            ? [Math.min(...profileK), Math.max(...profileK)]
            : [0, 1]
        }
        lines={[{ label: 'profile', color: '#ffd43b', x: profileK, y: snap.latest_profile?.amp ?? [] }]}
        maxStep={autoMaxStep(profileK, 1.5)}
        height={150}
        caption="Null/missing subcarriers break the line; nothing is interpolated."
        emptyText="No profile available"
      />
    </div>
  );
}
