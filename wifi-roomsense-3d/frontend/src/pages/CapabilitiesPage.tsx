/**
 * Capabilities & Research: what each capability can currently claim, with
 * the four evidence levels kept separate, plus zone (C) and pose (D) status.
 * Pose output is EXPERIMENTAL and never appears in the live view.
 */

import { api } from '../api/client';
import type { CapabilityStatus, VerificationStatus } from '../api/types';
import { CAPABILITY_IDS } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { JsonView } from '../components/JsonView';
import { Badge, Card, ErrorNotice, KeyValue, Notice } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { CAPABILITY_LETTER, CAPABILITY_NAME, capabilitySeverity } from '../lib/banner';

const EVIDENCE: { key: keyof Omit<VerificationStatus, 'notes'>; label: string; help: string }[] = [
  { key: 'software_tested', label: 'software-tested', help: 'Automated tests ran (synthetic / fixture data only).' },
  { key: 'firmware_compiled', label: 'firmware-compiled', help: 'Built with the pinned ESP-IDF for a real target.' },
  { key: 'hardware_tested', label: 'hardware-tested', help: 'Exercised with real boards.' },
  { key: 'through_wall_validated', label: 'through-wall-validated', help: 'Validated behind a real wall per protocol.' },
];

function Evidence({ ok }: { ok: boolean }) {
  return ok ? (
    <span className="evidence evidence--yes">✓ yes</span>
  ) : (
    <span className="evidence evidence--no">✗ not established</span>
  );
}

function CapabilityTable({ caps }: { caps: CapabilityStatus[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>capability</th>
            <th>state</th>
            {EVIDENCE.map((e) => (
              <th key={e.key} title={e.help}>
                {e.label}
              </th>
            ))}
            <th>reasons &amp; notes</th>
          </tr>
        </thead>
        <tbody>
          {CAPABILITY_IDS.map((id) => {
            const cap = caps.find((c) => c.capability === id);
            return (
              <tr key={id}>
                <td>
                  <strong>{CAPABILITY_LETTER[id]}</strong> · {cap?.title || CAPABILITY_NAME[id]}
                </td>
                <td>
                  <span className={`cap-chip cap-chip--${capabilitySeverity(cap?.state ?? null)} cap-chip--static`}>
                    {cap?.state.replace(/_/g, ' ') ?? 'NOT REPORTED'}
                  </span>
                </td>
                {EVIDENCE.map((e) => (
                  <td key={e.key}>{cap ? <Evidence ok={cap.verification[e.key] === true} /> : <span className="unavailable">unavailable</span>}</td>
                ))}
                <td>
                  {cap && cap.reasons.length + cap.verification.notes.length > 0 ? (
                    <ul className="compact-list">
                      {cap.reasons.map((r, i) => (
                        <li key={`r${i}`}>{r}</li>
                      ))}
                      {cap.verification.notes.map((n, i) => (
                        <li key={`n${i}`} className="muted">
                          {n}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function CapabilitiesPage({ live }: { live: LiveSnapshot }) {
  const zone = useApi((sig) => api.zoneStatus(sig), [], 10_000);
  const pose = useApi((sig) => api.poseStatus(sig), []);
  const status = live.connection === 'open' ? live.status : null;
  const caps = status?.capabilities ?? [];
  const unsupported = caps.filter((c) => c.state === 'UNSUPPORTED');

  return (
    <div className="page">
      <Card
        title="Capabilities"
        subtitle="A: live CSI acquisition · B: motion detection · C: experimental zone estimation · D: pose research. Evidence levels are independent: software tests are not hardware tests, and neither is a through-wall validation."
      >
        {!status && <Notice kind="warn">No backend connection: capability states unavailable.</Notice>}
        {status && <CapabilityTable caps={caps} />}
        {status && (status.unsupported_capabilities ?? []).length > 0 && (
          <div className="mt">
            <h3 className="h3">What this system does not do</h3>
            <div className="table-wrap">
              <table className="table table--compact">
                <thead>
                  <tr>
                    <th>claim not made</th>
                    <th>why</th>
                  </tr>
                </thead>
                <tbody>
                  {(status.unsupported_capabilities ?? []).map((u) => (
                    <tr key={u.id}>
                      <td>{u.claim}</td>
                      <td>{u.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        {status && (
          <div className="mt">
            <strong>Capabilities in state UNSUPPORTED:</strong>{' '}
            {unsupported.length === 0 ? (
              <span className="muted">none reported</span>
            ) : (
              <ul className="compact-list">
                {unsupported.map((c) => (
                  <li key={c.capability}>
                    {CAPABILITY_LETTER[c.capability]} · {c.title}: {c.reasons.join('; ') || 'no reason given'}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </Card>

      <Card
        title="C · Zone estimation (experimental)"
        subtitle="Enabled only after a model passes the predefined criteria on held-out sessions, with the same hardware and room."
        actions={
          <button type="button" className="btn btn--ghost btn--sm" onClick={zone.reload}>
            Refresh
          </button>
        }
      >
        {status && (
          <KeyValue
            items={[
              ['live zone state', <Badge key="s" tone={status.zone.state === 'ESTIMATE' ? 'info' : 'neutral'}>{status.zone.state}</Badge>],
              ['localization status', status.localization_status],
              ['live reasons', status.zone.reasons.join('; ') || '—'],
            ]}
          />
        )}
        <ErrorNotice error={zone.error} title="Could not load zone status" />
        {zone.data && (
          <div className="stack mt">
            <KeyValue
              items={[
                ['state', <Badge key="z">{String(zone.data.state)}</Badge>],
                ['reasons', zone.data.reasons.length ? zone.data.reasons.join('; ') : '—'],
              ]}
            />
            <details className="details" open>
              <summary>Enablement criteria</summary>
              <JsonView value={zone.data.criteria} nullLabel="not available" />
            </details>
            <details className="details" open>
              <summary>Last training / evaluation report (confusion matrix, abstention rate)</summary>
              {zone.data.report ? <JsonView value={zone.data.report} nullLabel="NOT MEASURED" /> : <p className="muted">No report: no model has been trained/evaluated.</p>}
            </details>
            <p className="hint">Model scores are classifier scores, not calibrated probabilities.</p>
          </div>
        )}
      </Card>

      <Card
        title={
          <>
            D · Pose research <Badge tone="sim">EXPERIMENTAL</Badge>
          </>
        }
        subtitle="Pose output is never shown in the live 3-D view. It stays disabled unless compatible hardware, usable trained weights and a measured validation exist."
      >
        <ErrorNotice error={pose.error} title="Could not load pose status" />
        {pose.data && (
          <div className="stack">
            <KeyValue
              items={[
                ['enabled', pose.data.enabled ? 'yes' : 'no'],
                ['label', pose.data.label],
                ['model', pose.data.model_id ?? 'none'],
              ]}
            />
            <div>
              <strong>Missing requirements</strong>
              {pose.data.missing_requirements.length > 0 ? (
                <ul className="compact-list">
                  {pose.data.missing_requirements.map((m, i) => (
                    <li key={i}>{m}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted">None listed.</p>
              )}
            </div>
            {pose.data.manifest && (
              <details className="details">
                <summary>Model manifest</summary>
                <JsonView value={pose.data.manifest} />
              </details>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
