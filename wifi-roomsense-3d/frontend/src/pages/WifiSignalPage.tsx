/**
 * My Wi-Fi signal: live signal strength (RSSI) of this computer's own Wi-Fi.
 * Kept separate from everything else: not CSI, not motion sensing, never shown
 * in the 3-D room.
 */

import { api } from '../api/client';
import { SignalPlot } from '../components/SignalPlot';
import { Card, ErrorNotice, KeyValue, Notice, Unavailable } from '../components/ui';
import { useAction, useApi } from '../hooks/useApi';
import { type HostWifiSnapshot, hostWifiStatus, signalUnit } from '../lib/hostWifi';
import { autoMaxStep, toRelativeSeconds } from '../lib/plotting';

const WINDOW_S = 120;

export function WifiSignalPage() {
  const snapState = useApi<HostWifiSnapshot>((sig) => api.hostWifi(WINDOW_S, sig), [], 1000);
  const start = useAction(api.hostWifiStart);
  const stop = useAction(api.hostWifiStop);
  const snap = snapState.data;
  const now = snap?.server_time_unix_ms ?? Date.now();
  const t = toRelativeSeconds(snap?.series.t ?? [], now);
  const unit = snap ? signalUnit(snap) : 'none';

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
              !snap.available ? 'Not available on this computer (see above).' : snap.running ? 'Waiting for readings…' : 'Press Start to see the signal.'
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
