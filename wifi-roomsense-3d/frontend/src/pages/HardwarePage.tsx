/**
 * Hardware inspection (GET /api/hardware). Non-destructive: nothing here
 * flashes, erases or reconfigures a device.
 */

import { api } from '../api/client';
import type { JsonValue } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { JsonView } from '../components/JsonView';
import { Card, ErrorNotice, Notice } from '../components/ui';
import { useApi } from '../hooks/useApi';

function pick(obj: JsonValue | null, keys: string[]): JsonValue | undefined {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return undefined;
  for (const k of keys) if (k in obj) return obj[k];
  return undefined;
}

export function HardwarePage({ live }: { live: LiveSnapshot }) {
  const hw = useApi((sig) => api.hardware(sig), []);
  const status = live.connection === 'open' ? live.status : null;
  const detected = pick(hw.data, ['detected', 'detected_hardware', 'ports', 'receivers']);
  const recommended = pick(hw.data, ['recommended', 'recommended_hardware', 'recommendations']);
  const rest =
    hw.data && typeof hw.data === 'object' && !Array.isArray(hw.data)
      ? Object.fromEntries(
          Object.entries(hw.data).filter(
            ([k]) => !['detected', 'detected_hardware', 'ports', 'receivers', 'recommended', 'recommended_hardware', 'recommendations'].includes(k),
          ),
        )
      : hw.data;

  return (
    <div className="page">
      {status?.hardware_required && (
        <div className="hw-required" role="note">
          <strong>HARDWARE REQUIRED</strong> — live measurements need ESP32 boards connected over USB with RoomSense (or
          documented esp-csi) firmware. Without them only replay and clearly labelled simulation are available.
        </div>
      )}
      <Card
        title="Hardware"
        subtitle="Read-only inspection. RoomSense never flashes, erases or reconfigures devices or routers."
        actions={
          <button type="button" className="btn btn--ghost btn--sm" onClick={hw.reload} disabled={hw.loading}>
            {hw.loading ? 'Inspecting…' : 'Re-inspect'}
          </button>
        }
      >
        <ErrorNotice error={hw.error} title="Hardware inspection failed" />
        {hw.data !== null && (
          <div className="two-col">
            <div>
              <h3 className="h3">Detected</h3>
              {detected !== undefined ? <JsonView value={detected} /> : <Notice kind="info">The backend did not report a "detected" section.</Notice>}
            </div>
            <div>
              <h3 className="h3">Recommended</h3>
              {recommended !== undefined ? (
                <JsonView value={recommended} />
              ) : (
                <Notice kind="info">The backend did not report a "recommended" section.</Notice>
              )}
            </div>
          </div>
        )}
        {rest !== null && rest !== undefined && typeof rest === 'object' && Object.keys(rest).length > 0 && (
          <details className="details mt" open>
            <summary>Other inspection details</summary>
            <JsonView value={rest} />
          </details>
        )}
      </Card>
      {status && status.links.length > 0 && (
        <Card title="Devices reported by the active source">
          <JsonView
            value={status.links.map((l) => ({
              link_id: l.link_id,
              receiver_id: l.receiver_id,
              connected: l.connected,
              channel: l.channel,
              layout_id: l.layout_id,
              device: l.device,
              clock_offset_ms: l.clock_offset_ms,
              clock_drift_ppm: l.clock_drift_ppm,
            }))}
          />
        </Card>
      )}
    </div>
  );
}
