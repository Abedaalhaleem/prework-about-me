/**
 * Recordings & Replay. Recording is opt-in and requires the consent form
 * (no names collected). Recordings stay on this machine.
 */

import { useState } from 'react';
import { api } from '../api/client';
import type { RecordingInfo, RecordingStartRequest } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { Badge, Card, ErrorNotice, Notice } from '../components/ui';
import { useAction, useApi } from '../hooks/useApi';
import { CONSENT_CHECKBOX_TEXT, CONSENT_STATEMENT_TEXT, CONSENT_STATEMENT_VERSION } from '../lib/consent';
import { fmtBytes, fmtDuration, fmtInt, fmtUnixNs } from '../lib/format';

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];

function RecordingForm({ recordingActive, onChanged }: { recordingActive: boolean; onChanged: () => void }) {
  const [consented, setConsented] = useState(false);
  const [participants, setParticipants] = useState('1');
  const [purpose, setPurpose] = useState('');
  const [label, setLabel] = useState('');
  const [scenario, setScenario] = useState('');
  const [notes, setNotes] = useState('');
  const [maxMinutes, setMaxMinutes] = useState('');
  const start = useAction((req: RecordingStartRequest) => api.startRecording(req));
  const stop = useAction(() => api.stopRecording());

  const count = Number(participants);
  const countOk = Number.isInteger(count) && count >= 1 && count <= 1000;
  const maxS = maxMinutes.trim() === '' ? null : Number(maxMinutes) * 60;
  const maxOk = maxS === null || (Number.isFinite(maxS) && maxS > 0);
  const canStart = consented && countOk && purpose.trim() !== '' && label.trim() !== '' && maxOk && !recordingActive;

  return (
    <div className="stack">
      <div className="consent">
        <div className="consent__title">Consent statement ({CONSENT_STATEMENT_VERSION})</div>
        <pre className="consent__text">{CONSENT_STATEMENT_TEXT}</pre>
      </div>
      <div className="form-grid">
        <label className="field">
          Number of people present
          <input value={participants} inputMode="numeric" onChange={(e) => setParticipants(e.target.value)} aria-invalid={!countOk} />
        </label>
        <label className="field field--wide">
          Purpose (no names)
          <input value={purpose} placeholder="e.g. baseline test of the hallway link" onChange={(e) => setPurpose(e.target.value)} />
        </label>
        <label className="field">
          Recording label
          <input value={label} placeholder="e.g. empty-room-evening" onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="field">
          Scenario (optional)
          <input value={scenario} placeholder="e.g. S1_EMPTY_TARGET_ROOM" onChange={(e) => setScenario(e.target.value)} />
        </label>
        <label className="field">
          Max duration (minutes, optional)
          <input value={maxMinutes} inputMode="decimal" onChange={(e) => setMaxMinutes(e.target.value)} aria-invalid={!maxOk} />
        </label>
        <label className="field field--wide">
          Notes (optional, no names)
          <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
      </div>
      <label className="check check--important">
        <input type="checkbox" checked={consented} onChange={(e) => setConsented(e.target.checked)} />
        <span>{CONSENT_CHECKBOX_TEXT}</span>
      </label>
      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!canStart || start.busy}
          onClick={async () => {
            const r = await start.run({
              consent: {
                all_participants_consented: true,
                participant_count: count,
                purpose: purpose.trim(),
                statement_version: CONSENT_STATEMENT_VERSION,
              },
              label: label.trim(),
              scenario: scenario.trim() || null,
              notes: notes.trim() || null,
              max_seconds: maxS,
            });
            if (r) {
              setConsented(false); // each recording needs its own confirmation
              onChanged();
            }
          }}
        >
          ● Start recording
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={!recordingActive || stop.busy}
          onClick={async () => {
            await stop.run();
            onChanged();
          }}
        >
          ■ Stop recording
        </button>
      </div>
      {recordingActive && <Notice kind="info">A recording is in progress.</Notice>}
      <ErrorNotice error={start.error} title="Recording not started" />
      <ErrorNotice error={stop.error} title="Stop failed" />
      {start.result && <Notice kind="ok">Recording started: {start.result.recording_id}</Notice>}
      {stop.result && (
        <Notice kind="ok">
          Recording {stop.result.recording_id} stopped: {stop.result.status}, {fmtDuration(stop.result.duration_s)},{' '}
          {fmtBytes(stop.result.bytes)}
        </Notice>
      )}
    </div>
  );
}

function RecordingRow({ rec, onChanged }: { rec: RecordingInfo; onChanged: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [speed, setSpeed] = useState(1);
  const del = useAction(() => api.deleteRecording(rec.recording_id));
  const replay = useAction(() => api.startReplay(rec.recording_id, speed));
  const exp = useAction(() => api.exportRecording(rec.recording_id));
  const synthetic = rec.synthetic || rec.source_mode === 'SIMULATION' || rec.original_source_mode === 'SIMULATION';
  return (
    <tr>
      <td>
        <div className="rec-label">{rec.label}</div>
        <code className="muted">{rec.recording_id}</code>
        {rec.scenario && <div className="muted">scenario: {rec.scenario}</div>}
      </td>
      <td>
        <Badge tone={rec.status === 'COMPLETE' ? 'ok' : rec.status === 'RECORDING' ? 'info' : 'warn'}>{rec.status}</Badge>
        {rec.stop_reason && <div className="muted">{rec.stop_reason}</div>}
      </td>
      <td>
        {synthetic ? (
          <Badge tone="sim" title="Contains synthetic data: never usable as validation evidence">
            SYNTHETIC
          </Badge>
        ) : (
          <span>{rec.source_mode}</span>
        )}
        {rec.original_source_mode && rec.original_source_mode !== rec.source_mode && (
          <div className="muted">originally {rec.original_source_mode}</div>
        )}
      </td>
      <td>{fmtUnixNs(rec.created_at_unix_ns)}</td>
      <td>{fmtDuration(rec.duration_s)}</td>
      <td>
        {fmtBytes(rec.bytes)}
        <div className="muted">{fmtInt(rec.frames)} frames</div>
      </td>
      <td>{rec.link_ids.join(', ') || '—'}</td>
      <td>
        <div className="row row--wrap">
          <select aria-label="Replay speed" value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
          <button type="button" className="btn btn--sm" disabled={replay.busy || rec.status === 'RECORDING'} onClick={() => void replay.run()}>
            Replay
          </button>
          <button type="button" className="btn btn--ghost btn--sm" disabled={exp.busy} onClick={() => void exp.run()}>
            Export
          </button>
          {!confirming ? (
            <button type="button" className="btn btn--ghost btn--sm" disabled={rec.status === 'RECORDING'} onClick={() => setConfirming(true)}>
              Delete
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn btn--danger btn--sm"
                disabled={del.busy}
                onClick={async () => {
                  const r = await del.run();
                  setConfirming(false);
                  if (r) onChanged();
                }}
              >
                Confirm delete
              </button>
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setConfirming(false)}>
                Keep
              </button>
            </>
          )}
        </div>
        <ErrorNotice error={replay.error ?? exp.error ?? del.error} title="Action failed" />
        {replay.result && <div className="muted">Replay started ({replay.result.source_state}).</div>}
      </td>
    </tr>
  );
}

export function RecordingsPage({ live }: { live: LiveSnapshot }) {
  const recs = useApi((sig) => api.recordings(sig), [], 5000);
  const status = live.connection === 'open' ? live.status : null;
  return (
    <div className="page">
      <Card
        title="Record"
        subtitle="Recording is opt-in, needs everyone's agreement, and stays on this computer. Only use it in spaces you control."
      >
        <RecordingForm recordingActive={!!status?.recording_active} onChanged={recs.reload} />
      </Card>
      <Card
        title="Recordings"
        subtitle="Replays keep the original gaps and are labelled RECORDED REPLAY. Synthetic recordings are marked and never count as evidence."
        actions={
          <button type="button" className="btn btn--ghost btn--sm" onClick={recs.reload}>
            Refresh
          </button>
        }
      >
        <ErrorNotice error={recs.error} title="Could not list recordings" />
        {recs.data && recs.data.length === 0 && <p className="muted">No recordings yet.</p>}
        {recs.data && recs.data.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>label / id</th>
                  <th>status</th>
                  <th>data</th>
                  <th>created</th>
                  <th>duration</th>
                  <th>size</th>
                  <th>links</th>
                  <th>actions</th>
                </tr>
              </thead>
              <tbody>
                {recs.data.map((r) => (
                  <RecordingRow key={r.recording_id} rec={r} onChanged={recs.reload} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
