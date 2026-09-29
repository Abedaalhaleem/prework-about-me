/**
 * Through-wall validation: protocol scenarios, run metadata, ground-truth
 * event marking, and the report (with "NOT MEASURED" preserved wherever the
 * backend has no data).
 */

import { useState } from 'react';
import { api } from '../api/client';
import type { EventKind, ProtocolScenario, ValidationRun, ValidationRunRequest } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { JsonView } from '../components/JsonView';
import { Card, ErrorNotice, Notice } from '../components/ui';
import { useAction, useApi } from '../hooks/useApi';
import { fmtDuration, fmtUnixNs } from '../lib/format';

function ScenarioCard({ s, selected, onSelect }: { s: ProtocolScenario; selected: boolean; onSelect: () => void }) {
  return (
    <div className={`scenario ${selected ? 'scenario--selected' : ''}`}>
      <div className="row row--between">
        <h3 className="h3">
          <code>{s.scenario_id}</code> {s.title}
        </h3>
        <button type="button" className="btn btn--sm" aria-pressed={selected} onClick={onSelect}>
          {selected ? 'Selected' : 'Select'}
        </button>
      </div>
      <p>{s.purpose}</p>
      <ol className="scenario__steps">
        {s.instructions.map((i, k) => (
          <li key={k}>{i}</li>
        ))}
      </ol>
      <div className="scenario__meta">
        Minimum duration: {fmtDuration(s.min_duration_s)} · Labels:{' '}
        {s.interval_labels.length > 0 ? s.interval_labels.join(', ') : 'none (no START/END events needed)'} ·{' '}
        {s.counts_toward_status ? 'counts toward the through-wall status' : 'informational only'}
      </div>
      <details className="details">
        <summary>What is measured</summary>
        <ul>
          {s.measured.map((m, k) => (
            <li key={k}>{m}</li>
          ))}
        </ul>
        <p className="muted">{s.pass_fail_meaning}</p>
      </details>
    </div>
  );
}

function RunForm({ scenario, onStarted }: { scenario: ProtocolScenario | null; onStarted: (r: ValidationRun) => void }) {
  const [placement, setPlacement] = useState('');
  const [wall, setWall] = useState('');
  const [channel, setChannel] = useState('');
  const [conditions, setConditions] = useState('');
  const [notes, setNotes] = useState('');
  const start = useAction((req: ValidationRunRequest) => api.startValidationRun(req));
  const ch = channel.trim() === '' ? null : Number(channel);
  const chOk = ch === null || (Number.isInteger(ch) && ch >= 1 && ch <= 233);
  return (
    <div className="stack">
      <div className="form-grid">
        <label className="field field--wide">
          Placement (where each TX/RX is mounted, which side of the wall)
          <textarea rows={2} value={placement} onChange={(e) => setPlacement(e.target.value)} />
        </label>
        <label className="field field--wide">
          Wall description (material, thickness — say "unknown" rather than guessing)
          <input value={wall} onChange={(e) => setWall(e.target.value)} />
        </label>
        <label className="field">
          Wi-Fi channel
          <input value={channel} inputMode="numeric" onChange={(e) => setChannel(e.target.value)} aria-invalid={!chOk} />
        </label>
        <label className="field field--wide">
          Conditions (people/pets nearby, fans, windows, Wi-Fi load, time of day)
          <input value={conditions} onChange={(e) => setConditions(e.target.value)} />
        </label>
        <label className="field field--wide">
          Notes
          <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
      </div>
      <button
        type="button"
        className="btn btn--primary"
        disabled={!scenario || !chOk || start.busy}
        onClick={async () => {
          if (!scenario) return;
          const r = await start.run({
            scenario_id: scenario.scenario_id,
            placement: placement.trim(),
            wall_description: wall.trim(),
            channel: ch,
            conditions: conditions.trim(),
            notes: notes.trim(),
          });
          if (r) onStarted(r);
        }}
      >
        Start validation run{scenario ? ` (${scenario.scenario_id})` : ''}
      </button>
      {!scenario && <p className="muted">Select a scenario above first.</p>}
      <ErrorNotice error={start.error} title="Run not started" />
    </div>
  );
}

function EventMarker({ labels }: { labels: string[] }) {
  const [label, setLabel] = useState('');
  const [notes, setNotes] = useState('');
  const [log, setLog] = useState<string[]>([]);
  const post = useAction((kind: EventKind) => api.postEvent({ label: label.trim(), kind, notes: notes.trim() || null }));
  const mark = async (kind: EventKind): Promise<void> => {
    const r = await post.run(kind);
    if (r !== undefined) {
      // Keep only the last few entries (bounded).
      setLog((l) => [`${new Date().toLocaleTimeString()} ${kind} ${label.trim()}`, ...l].slice(0, 20));
    }
  };
  return (
    <div className="stack">
      <p className="hint">
        Mark what you <strong>observe</strong> (not what the display shows), at the moment it happens. The backend
        timestamps the event on arrival.
      </p>
      <div className="form-grid">
        <label className="field">
          Label
          <input list="event-labels" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. MOVING" />
          <datalist id="event-labels">
            {labels.map((l) => (
              <option key={l} value={l} />
            ))}
          </datalist>
        </label>
        <label className="field field--wide">
          Notes (optional)
          <input value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
      </div>
      <div className="row">
        {(['START', 'END', 'MARK'] as const).map((k) => (
          <button key={k} type="button" className="btn" disabled={!label.trim() || post.busy} onClick={() => void mark(k)}>
            {k}
          </button>
        ))}
      </div>
      <ErrorNotice error={post.error} title="Event not recorded" />
      {log.length > 0 && (
        <ul className="event-log">
          {log.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ValidationPage({ live }: { live: LiveSnapshot }) {
  const protocol = useApi((sig) => api.validationProtocol(sig), []);
  const report = useApi((sig) => api.validationReport(sig), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [run, setRun] = useState<ValidationRun | null>(null);
  const stop = useAction((id: string) => api.stopValidationRun(id));
  const download = useAction(() => api.downloadValidationReportMd());
  const scenarios = protocol.data?.scenarios ?? [];
  const selected = scenarios.find((s) => s.scenario_id === selectedId) ?? null;
  const status = live.connection === 'open' ? live.status : null;
  const labels = [...new Set(scenarios.flatMap((s) => s.interval_labels))];

  return (
    <div className="page">
      {status && status.source_mode !== 'LIVE' && (
        <Notice kind="warn">
          The current source is {status.source_banner}. Only LIVE hardware runs count as evidence; simulation and replay runs
          are listed in the report but never counted.
        </Notice>
      )}
      <Card
        title="Through-wall validation protocol"
        subtitle={protocol.data ? `Protocol ${protocol.data.protocol_version}` : undefined}
      >
        <ErrorNotice error={protocol.error} title="Could not load the protocol" />
        {protocol.data && (
          <>
            <details className="details" open>
              <summary>General rules</summary>
              <ul>
                {protocol.data.general_rules.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </details>
            <details className="details">
              <summary>Required run metadata</summary>
              <dl className="kv">
                {protocol.data.required_metadata.map((m) => (
                  <div className="kv__row" key={m.field}>
                    <dt>{m.field}</dt>
                    <dd>{m.help}</dd>
                  </div>
                ))}
              </dl>
            </details>
            <div className="scenario-list">
              {scenarios.map((s) => (
                <ScenarioCard key={s.scenario_id} s={s} selected={s.scenario_id === selectedId} onSelect={() => setSelectedId(s.scenario_id)} />
              ))}
            </div>
          </>
        )}
      </Card>

      <div className="two-col">
        <Card title="Run">
          {run && run.status === 'RUNNING' ? (
            <div className="stack">
              <Notice kind="info">
                Run <code>{run.run_id}</code> ({run.scenario_id}) started {fmtUnixNs(run.started_at_unix_ns)} with source{' '}
                {run.source_mode}.
              </Notice>
              <button
                type="button"
                className="btn btn--danger"
                disabled={stop.busy}
                onClick={async () => {
                  const r = await stop.run(run.run_id);
                  if (r) {
                    setRun(r);
                    report.reload();
                  }
                }}
              >
                Stop run
              </button>
              <ErrorNotice error={stop.error} title="Stop failed" />
            </div>
          ) : (
            <>
              {run && (
                <Notice kind="ok">
                  Last run <code>{run.run_id}</code>: {run.status}
                  {run.ended_at_unix_ns ? `, ended ${fmtUnixNs(run.ended_at_unix_ns)}` : ''}.
                </Notice>
              )}
              <RunForm scenario={selected} onStarted={setRun} />
            </>
          )}
        </Card>
        <Card title="Mark events (ground truth)">
          <EventMarker labels={labels} />
        </Card>
      </div>

      <Card
        title="Validation report"
        subtitle="Computed by the backend from stored runs and events. 'NOT MEASURED' means there is no data for that item."
        actions={
          <div className="row">
            <button type="button" className="btn btn--ghost btn--sm" onClick={report.reload}>
              Refresh
            </button>
            <button type="button" className="btn btn--sm" disabled={download.busy} onClick={() => void download.run()}>
              Download markdown
            </button>
          </div>
        }
      >
        <ErrorNotice error={report.error} title="Could not load the report" />
        <ErrorNotice error={download.error} title="Download failed" />
        {report.data !== null && <JsonView value={report.data} nullLabel="NOT MEASURED" />}
      </Card>
    </div>
  );
}
