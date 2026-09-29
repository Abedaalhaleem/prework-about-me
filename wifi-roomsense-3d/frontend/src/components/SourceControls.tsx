/**
 * Source selection. The user always picks the source explicitly:
 *  - Live: serial ports are listed but never auto-selected.
 *  - Replay: a stored recording at a chosen speed.
 *  - Simulation: requires an explicit acknowledgement checkbox and is never
 *    pre-selected or auto-started.
 */

import { useState } from 'react';
import { api } from '../api/client';
import type { InputFormat, LtfConfig, ReceiverConfig, SystemStatus } from '../api/types';
import { INPUT_FORMATS } from '../api/types';
import { useAction, useApi } from '../hooks/useApi';
import { fmtDuration } from '../lib/format';
import { ErrorNotice, Notice } from './ui';

type Tab = 'live' | 'replay' | 'simulation';

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];
export const SIM_ACK_TEXT = 'I understand this is simulated data, not a measurement';

interface ReceiverDraft {
  receiver_id: string;
  port: string;
  input_format: InputFormat;
  transmitter_id: string;
  transmitter_mac: string;
  declared_chip: string;
  ltf_config: LtfConfig | '';
}

const emptyReceiver = (n: number): ReceiverDraft => ({
  receiver_id: `rx${n}`,
  port: '',
  input_format: 'roomsense-rscsi-v1',
  transmitter_id: 'tx1',
  transmitter_mac: '',
  declared_chip: '',
  ltf_config: '',
});

function toReceiverConfig(d: ReceiverDraft): ReceiverConfig {
  return {
    receiver_id: d.receiver_id.trim(),
    port: d.port.trim(),
    input_format: d.input_format,
    transmitter_id: d.transmitter_id.trim() || 'tx1',
    transmitter_mac: d.transmitter_mac.trim() || null,
    declared_chip: d.declared_chip.trim() || null,
    ltf_config: d.ltf_config || null,
  };
}

function startedText(s: SystemStatus | undefined): string | null {
  if (!s) return null;
  return `Source: ${s.source_banner} (${s.source_state})${s.source_detail ? ` — ${s.source_detail}` : ''}`;
}

function LivePanel({ status }: { status: SystemStatus | null }) {
  const ports = useApi((sig) => api.serialPorts(sig), []);
  const [drafts, setDrafts] = useState<ReceiverDraft[]>([]);
  const start = useAction((receivers?: ReceiverConfig[]) => api.startLive(receivers));
  const configured = status?.links ?? [];

  const update = (i: number, patch: Partial<ReceiverDraft>): void =>
    setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const draftsValid = drafts.every((d) => d.receiver_id.trim() && d.port.trim());

  return (
    <div className="stack">
      <div className="row row--between">
        <h3 className="h3">Serial ports on this machine</h3>
        <button type="button" className="btn btn--ghost btn--sm" onClick={ports.reload} disabled={ports.loading}>
          {ports.loading ? 'Scanning…' : 'Rescan'}
        </button>
      </div>
      <ErrorNotice error={ports.error} title="Could not list serial ports" />
      {ports.data && ports.data.length === 0 && <p className="muted">No serial ports found. Is a board plugged in?</p>}
      {ports.data && ports.data.length > 0 && (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>device</th>
                <th>description</th>
                <th>VID:PID</th>
                <th>USB-UART bridge?</th>
              </tr>
            </thead>
            <tbody>
              {ports.data.map((p) => (
                <tr key={p.device}>
                  <td>
                    <code>{p.device}</code>
                  </td>
                  <td>{p.description ?? 'unavailable'}</td>
                  <td>
                    {p.vid !== null && p.pid !== null
                      ? `${p.vid.toString(16).padStart(4, '0')}:${p.pid.toString(16).padStart(4, '0')}`
                      : 'unavailable'}
                  </td>
                  <td>{p.likely_usb_uart_bridge ? 'likely' : 'unknown'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="hint">Ports are never guessed. Receivers are configured in configs/roomsense.toml or below.</p>

      <h3 className="h3">Receivers reported by the backend</h3>
      {configured.length === 0 ? (
        <p className="muted">None reported for the current source.</p>
      ) : (
        <ul className="plain-list">
          {configured.map((l) => (
            <li key={l.link_id}>
              <code>{l.receiver_id}</code> ← <code>{l.transmitter_id}</code> ({l.connected ? 'connected' : 'not connected'})
            </li>
          ))}
        </ul>
      )}

      <button type="button" className="btn btn--primary" disabled={start.busy} onClick={() => void start.run(undefined)}>
        Start live (configured receivers)
      </button>

      <details className="details">
        <summary>Override receivers for this session</summary>
        <div className="stack">
          {drafts.map((d, i) => (
            <fieldset key={i} className="receiver-row">
              <legend>Receiver {i + 1}</legend>
              <label>
                receiver id
                <input value={d.receiver_id} onChange={(e) => update(i, { receiver_id: e.target.value })} />
              </label>
              <label>
                serial port
                <input
                  list="serial-port-options"
                  value={d.port}
                  placeholder="/dev/ttyUSB0"
                  onChange={(e) => update(i, { port: e.target.value })}
                />
              </label>
              <label>
                input format
                <select value={d.input_format} onChange={(e) => update(i, { input_format: e.target.value as InputFormat })}>
                  {INPUT_FORMATS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                transmitter id
                <input value={d.transmitter_id} onChange={(e) => update(i, { transmitter_id: e.target.value })} />
              </label>
              <label>
                transmitter MAC
                <input
                  value={d.transmitter_mac}
                  placeholder="aa:bb:cc:dd:ee:ff"
                  onChange={(e) => update(i, { transmitter_mac: e.target.value })}
                />
              </label>
              <label>
                declared chip (upstream formats)
                <input value={d.declared_chip} placeholder="esp32s3" onChange={(e) => update(i, { declared_chip: e.target.value })} />
              </label>
              <label>
                LTF config (upstream formats)
                <select value={d.ltf_config} onChange={(e) => update(i, { ltf_config: e.target.value as LtfConfig | '' })}>
                  <option value="">(reported by firmware)</option>
                  <option value="lltf_only">lltf_only</option>
                  <option value="lltf_htltf_stbc">lltf_htltf_stbc</option>
                  <option value="c5_default">c5_default</option>
                </select>
              </label>
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setDrafts((ds) => ds.filter((_, j) => j !== i))}>
                Remove
              </button>
            </fieldset>
          ))}
          <datalist id="serial-port-options">
            {(ports.data ?? []).map((p) => (
              <option key={p.device} value={p.device} />
            ))}
          </datalist>
          <div className="row">
            <button type="button" className="btn btn--ghost btn--sm" onClick={() => setDrafts((ds) => [...ds, emptyReceiver(ds.length + 1)])}>
              Add receiver
            </button>
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={start.busy || drafts.length === 0 || !draftsValid}
              onClick={() => void start.run(drafts.map(toReceiverConfig))}
            >
              Start live with these receivers
            </button>
          </div>
        </div>
      </details>
      <ErrorNotice error={start.error} title="Live source not started" />
      {start.result && <Notice kind="ok">{startedText(start.result)}</Notice>}
    </div>
  );
}

function ReplayPanel() {
  const recs = useApi((sig) => api.recordings(sig), []);
  const [recordingId, setRecordingId] = useState('');
  const [speed, setSpeed] = useState(1);
  const start = useAction((id: string, sp: number) => api.startReplay(id, sp));
  return (
    <div className="stack">
      <ErrorNotice error={recs.error} title="Could not list recordings" />
      <label className="field">
        Recording
        <select value={recordingId} onChange={(e) => setRecordingId(e.target.value)}>
          <option value="">— choose a recording —</option>
          {(recs.data ?? []).map((r) => (
            <option key={r.recording_id} value={r.recording_id}>
              {r.label ?? r.recording_id} · {fmtDuration(r.duration_s ?? null)}
              {r.synthetic || r.source_mode === 'SIMULATION' ? ' · SYNTHETIC' : ''}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        Speed
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </label>
      <p className="hint">Replays keep the original gaps. The header will say RECORDED REPLAY.</p>
      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!recordingId || start.busy}
          onClick={() => void start.run(recordingId, speed)}
        >
          Start replay
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={recs.reload}>
          Refresh list
        </button>
      </div>
      <ErrorNotice error={start.error} title="Replay not started" />
      {start.result && <Notice kind="ok">{startedText(start.result)}</Notice>}
    </div>
  );
}

function SimulationPanel() {
  const scenarios = useApi((sig) => api.simulationScenarios(sig), []);
  const [scenario, setScenario] = useState('');
  const [ack, setAck] = useState(false);
  const [seed, setSeed] = useState('');
  const start = useAction((name: string, s?: number) => api.startSimulation(name, true, s));
  const chosen = scenarios.data?.find((s) => s.name === scenario);
  const seedNum = seed.trim() === '' ? undefined : Number(seed);
  const seedOk = seedNum === undefined || (Number.isInteger(seedNum) && seedNum >= 0);

  return (
    <div className="stack">
      <Notice kind="warn">
        Simulation produces <strong>synthetic</strong> data from a seeded generator. It is for testing the software only and is
        never a measurement. The whole UI will show a SIMULATED DATA banner.
      </Notice>
      <ErrorNotice error={scenarios.error} title="Could not list scenarios" />
      <label className="field">
        Scenario
        <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
          <option value="">— choose a scenario —</option>
          {(scenarios.data ?? []).map((s) => (
            <option key={s.name} value={s.name}>
              {s.name} ({fmtDuration(s.duration_s)})
            </option>
          ))}
        </select>
      </label>
      {chosen && <p className="hint">{chosen.description}</p>}
      <label className="field">
        Seed (optional, integer)
        <input value={seed} onChange={(e) => setSeed(e.target.value)} inputMode="numeric" placeholder="default" />
      </label>
      <label className="check check--important">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>{SIM_ACK_TEXT}</span>
      </label>
      <button
        type="button"
        className="btn btn--warn"
        disabled={!scenario || !ack || !seedOk || start.busy}
        onClick={async () => {
          const r = await start.run(scenario, seedNum);
          if (r) setAck(false); // require a fresh acknowledgement next time
        }}
      >
        Start simulation
      </button>
      <ErrorNotice error={start.error} title="Simulation not started" />
      {start.result && <Notice kind="warn">{startedText(start.result)}</Notice>}
    </div>
  );
}

export function SourceControls({ status }: { status: SystemStatus | null }) {
  const [tab, setTab] = useState<Tab>('live');
  const stop = useAction(() => api.stopSource());
  return (
    <div className="source-controls">
      <div className="row row--between">
        <div className="segmented" role="tablist" aria-label="Source type">
          {(['live', 'replay', 'simulation'] as const).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} aria-pressed={tab === t} onClick={() => setTab(t)}>
              {t === 'live' ? 'Live' : t === 'replay' ? 'Replay' : 'Simulation'}
            </button>
          ))}
        </div>
        <button type="button" className="btn btn--danger btn--sm" disabled={stop.busy} onClick={() => void stop.run()}>
          Stop source
        </button>
      </div>
      <ErrorNotice error={stop.error} title="Stop failed" />
      {tab === 'live' && <LivePanel status={status} />}
      {tab === 'replay' && <ReplayPanel />}
      {tab === 'simulation' && <SimulationPanel />}
    </div>
  );
}
