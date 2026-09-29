/**
 * Dashboard: 3-D room with per-link activity, link list, signal plots and
 * source controls.
 */

import { useState } from 'react';
import type { LiveSnapshot } from '../api/ws';
import { LinkList } from '../components/LinkList';
import { RoomView } from '../components/RoomView';
import { SignalPanel } from '../components/SignalPanel';
import { SourceControls } from '../components/SourceControls';
import { Card } from '../components/ui';
import { useRoom } from '../hooks/RoomContext';
import { serverNowMs, statusLinkIds } from '../lib/freshness';

export function DashboardPage({ live, now }: { live: LiveSnapshot; now: number }) {
  const { room, error: roomError } = useRoom();
  const [selected, setSelected] = useState<string | null>(null);
  const connected = live.connection === 'open' && live.status !== null && live.statusReceivedAtMs !== null;
  const status = connected ? live.status : null;
  const receivedAt = live.statusReceivedAtMs;
  const ids = status ? statusLinkIds(status) : [];
  const linkId = selected && ids.includes(selected) ? selected : (ids[0] ?? null);
  const serverNow = status && receivedAt !== null ? serverNowMs(status, receivedAt, now) : null;

  return (
    <div className="page page--dashboard">
      <div className="dash-grid">
        <Card title="Room (3-D)" className="dash-grid__room" subtitle="Drag to rotate · scroll to zoom · right-drag to pan">
          <RoomView room={room} roomError={roomError} live={live} now={now} />
        </Card>
        <div className="dash-grid__side">
          <Card title="Source">
            <SourceControls status={status} />
          </Card>
          <Card title="Links" subtitle="Select a link to plot its signals">
            <LinkList status={status} receivedAtMs={receivedAt} now={now} selected={linkId} onSelect={setSelected} />
          </Card>
        </div>
      </div>
      <Card title={linkId ? `Signals · ${linkId}` : 'Signals'} subtitle="Hatched = gap (no data). Lines never bridge missing samples.">
        <SignalPanel entry={linkId ? live.signals.get(linkId) : undefined} serverNowMs={serverNow} clientNowMs={now} />
      </Card>
      {status && status.notes.length > 0 && (
        <Card title="Backend notes">
          <ul className="compact-list">
            {status.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
