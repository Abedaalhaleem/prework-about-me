/**
 * My Wi-Fi signal: live signal strength (RSSI) of this computer's own Wi-Fi.
 * Kept separate from everything else: not CSI, not motion sensing, never shown
 * in the 3-D room.
 */

import { useMemo } from 'react';
import { api } from '../api/client';
import { SignalPlot } from '../components/SignalPlot';
import { SignalRibbonView } from '../components/SignalRibbonView';
import { Card, ErrorNotice, KeyValue, Notice, Unavailable } from '../components/ui';
import { useAction, useApi } from '../hooks/useApi';
import { type HostWifiSnapshot, hostWifiStatus, signalUnit } from '../lib/hostWifi';
import { autoMaxStep, toRelativeSeconds } from '../lib/plotting';
import { ribbonSegments } from '../lib/ribbon';

const WINDOW_S = 120;

export function WifiSignalPage() {
  const snapState = useApi<HostWifiSnapshot>((sig) => api.hostWifi(WINDOW_S, sig), [], 1000);
  const start = useAction(api.hostWifiStart);
  const stop = useAction(api.hostWifiStop);
  const snap = snapState.data;
  const now = snap?.server_time_unix_ms ?? Date.now();
  const t = toRelativeSeconds(snap?.series.t ?? [], now);
  const unit = snap ? signalUnit(snap) : 'none';
  // Readings further apart than a few sample intervals are a gap in the ribbon.
  const maxGapS = Math.max(2, 3 * (snap?.interval_s ?? 1));
  const ribbon = useMemo(
    () => ({
      rssi: ribbonSegments(t, snap?.series.rssi_dbm ?? [], WINDOW_S, maxGapS),
      noise: ribbonSegments(t, snap?.series.noise_dbm ?? [], WINDOW_S, maxGapS),
    }),
    // t is derived from snap; recompute when a new snapshot arrives.
    [snap, maxGapS],
  );

  return (
    <div className="page">
      <Notice kind="warn">
        <strong>This is your computer&apos;s Wi-Fi signal strength (RSSI): one number, about twice a second.</strong> It is
        not the detailed CSI data RoomSense needs for motion sensing. It cannot see through walls, it is not used by the
        motion detector, and it is never drawn in the 3-D room. It changes mostly when you or the laptop move, when other
        devices use the network, or when the router adjusts. Your network name is never read into these samples, nothing
        is scanned, and nothing is saved to disk.
      </Notice>
      <Card
        title="My Wi-Fi signal"
        subtitle={snap ? hostWifiStatus(snap) : 'Loading…'}
        actions={
          snap?.running ? (
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => void stop.run().then(snapState.reload)} disabled={stop.busy}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--primary btn--sm"
              onClick={() => void start.run().then(snapState.reload)}
              disabled={start.busy || snap?.available === false}
            >
              Start
            </button>
          )
        }
      >
        <ErrorNotice error={snapState.error ?? start.error ?? stop.error} />
        {snap && (
          <KeyValue
            items={[
              ['Reader', snap.method ?? <Unavailable />],
              ['Computer', snap.platform],
              ['Sample interval', snap.interval_s !== null ? `${snap.interval_s} s` : <Unavailable />],
              ['Channel', snap.latest?.channel ?? <Unavailable />],
              ['Link rate', snap.latest?.tx_rate_mbps != null ? `${snap.latest.tx_rate_mbps} Mbps` : <Unavailable />],
              ['Failed reads', snap.errors > 0 ? `${snap.errors} (last: ${snap.last_error ?? 'unknown'})` : '0'],
            ]}
          />
        )}
        {snap && unit !== 'percent' && (
          <>
            <Notice kind="info">
              <strong>Try this:</strong> keep the laptop still for 30 s and the ribbon stays fairly flat. Then walk between the
              laptop and your router a few times, cover the laptop&apos;s screen edge with both hands, or carry it toward and
              away from the router, and watch the ribbon dip and rise. Your body absorbs some Wi-Fi signal: that is the
              same effect motion sensing uses, but this single number is far too coarse to locate anyone or to see
              through walls.
            </Notice>
            <h3 className="h3 mt">3-D view</h3>
            <SignalRibbonView rssi={ribbon.rssi} noise={ribbon.noise} />
            <h3 className="h3 mt">Flat chart</h3>
          </>
        )}
        {snap && unit !== 'percent' && (
          <SignalPlot
            title="Signal strength (RSSI) and noise"
            caption="dBm, closer to 0 is stronger. Gaps are failed or missing readings; lines are not bridged."
            xLabel="time relative to now (s)"
            yLabel="dBm"
            xDomain={[-WINDOW_S, 0]}
            lines={[
              { label: 'signal (RSSI)', color: '#4dabf7', x: t, y: snap.series.rssi_dbm },
              { label: 'noise', color: '#868e96', x: t, y: snap.series.noise_dbm },
            ]}
            maxStep={autoMaxStep(t, 6)}
            height={260}
            legend
            emptyText={
              snap.available === false ? 'Not available on this computer (see above).' : snap.running ? 'Waiting for readings…' : 'Press Start to see the signal.'
            }
          />
        )}
        {snap && unit === 'percent' && (
          <SignalPlot
            title="Signal (percent, as reported by Windows)"
            xLabel="time relative to now (s)"
            yLabel="signal (%)"
            xDomain={[-WINDOW_S, 0]}
            yDomain={[0, 100]}
            lines={[{ label: 'signal', color: '#4dabf7', x: t, y: snap.series.signal_percent }]}
            maxStep={autoMaxStep(t, 6)}
            height={260}
          />
        )}
      </Card>
    </div>
  );
}
