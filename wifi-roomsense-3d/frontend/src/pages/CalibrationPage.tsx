/**
 * Calibration wizard: room geometry (entered by the user, never inferred),
 * quiet baseline and walk test.
 */

import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type {
  CalibrationRecord,
  Door,
  LinkDef,
  NodeRole,
  RoomGeometry,
  SensorNode,
  SystemStatus,
  Wall,
  WalkTestReport,
  Zone,
} from '../api/types';
import { NODE_ROLES } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { NumInput, PointTable } from '../components/fields';
import { JsonView } from '../components/JsonView';
import { RoomPreview2D } from '../components/RoomPreview2D';
import { Card, ErrorNotice, KeyValue, Notice } from '../components/ui';
import { useRoom } from '../hooks/RoomContext';
import { useAction, useApi } from '../hooks/useApi';
import { calibrationInfo } from '../lib/banner';
import { fmtDuration, fmtNum, fmtPercent, fmtUnixNs } from '../lib/format';
import { blankRoom, cloneRoom, defaultLinkId, perimeterWalls, validateRoom } from '../lib/roomValidation';

type Step = 'room' | 'walls' | 'nodes' | 'links' | 'zones' | 'save' | 'baseline' | 'walk';

const STEPS: { id: Step; label: string }[] = [
  { id: 'room', label: '1 · Room' },
  { id: 'walls', label: '2 · Walls & doors' },
  { id: 'nodes', label: '3 · Sensor nodes' },
  { id: 'links', label: '4 · Links' },
  { id: 'zones', label: '5 · Zones & target room' },
  { id: 'save', label: '6 · Save geometry' },
  { id: 'baseline', label: '7 · Quiet baseline' },
  { id: 'walk', label: '8 · Walk test' },
];

export const EMPTY_ROOM_CONFIRM_TEXT = 'I confirm the target room is empty and quiet';

type Origin = 'saved' | 'example' | 'blank';

function nextId(prefix: string, existing: string[]): string {
  let i = existing.length + 1;
  while (existing.includes(`${prefix}${i}`)) i++;
  return `${prefix}${i}`;
}

// ------------------------------------------------------------------ geometry steps

function RoomStep({ draft, setDraft }: { draft: RoomGeometry; setDraft: (r: RoomGeometry) => void }) {
  return (
    <div className="form-grid">
      <label className="field">
        Name
        <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </label>
      <label className="field">
        Width (x extent, m)
        <NumInput ariaLabel="Room width" value={draft.width_m} onChange={(v) => setDraft({ ...draft, width_m: v })} />
      </label>
      <label className="field">
        Depth (y extent, m)
        <NumInput ariaLabel="Room depth" value={draft.depth_m} onChange={(v) => setDraft({ ...draft, depth_m: v })} />
      </label>
      <label className="field">
        Ceiling height (m)
        <NumInput ariaLabel="Room height" value={draft.height_m} onChange={(v) => setDraft({ ...draft, height_m: v })} />
      </label>
      <label className="field field--wide">
        Notes (optional)
        <textarea
          value={draft.notes ?? ''}
          rows={2}
          onChange={(e) => setDraft({ ...draft, notes: e.target.value.trim() === '' ? null : e.target.value })}
        />
      </label>
      <p className="hint field--wide">
        Coordinates are metres: x east, y north, z height above the floor. Measure them with a tape; they are never
        estimated from Wi-Fi.
      </p>
    </div>
  );
}

function WallsStep({ draft, setDraft }: { draft: RoomGeometry; setDraft: (r: RoomGeometry) => void }) {
  const setWall = (i: number, patch: Partial<Wall>): void =>
    setDraft({ ...draft, walls: draft.walls.map((w, j) => (j === i ? { ...w, ...patch } : w)) });
  const setDoor = (i: number, patch: Partial<Door>): void =>
    setDraft({ ...draft, doors: draft.doors.map((d, j) => (j === i ? { ...d, ...patch } : d)) });
  return (
    <div className="stack">
      <div className="row">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => {
            if (draft.walls.length > 0 && !window.confirm('Replace all walls (and remove their doors) with 4 perimeter walls?')) return;
            setDraft({ ...draft, walls: perimeterWalls(draft.width_m, draft.depth_m, draft.height_m), doors: [] });
          }}
        >
          Generate 4 perimeter walls from the room size
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() =>
            setDraft({
              ...draft,
              walls: [
                ...draft.walls,
                {
                  id: nextId('w', draft.walls.map((w) => w.id)),
                  start: { x: Number.NaN, y: Number.NaN },
                  end: { x: Number.NaN, y: Number.NaN },
                  height_m: draft.height_m,
                  thickness_m: 0.12,
                  material: null,
                  is_target_room_boundary: false,
                },
              ],
            })
          }
        >
          Add wall
        </button>
      </div>
      <div className="table-wrap">
        <table className="table table--compact">
          <thead>
            <tr>
              <th>id</th>
              <th>start x</th>
              <th>start y</th>
              <th>end x</th>
              <th>end y</th>
              <th>height (m)</th>
              <th>thickness (m)</th>
              <th>material</th>
              <th>target-room boundary</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {draft.walls.map((w, i) => (
              <tr key={i}>
                <td>
                  <input className="id-input" value={w.id} aria-label="wall id" onChange={(e) => setWall(i, { id: e.target.value })} />
                </td>
                <td>
                  <NumInput ariaLabel="start x" value={w.start.x} onChange={(x) => setWall(i, { start: { ...w.start, x } })} />
                </td>
                <td>
                  <NumInput ariaLabel="start y" value={w.start.y} onChange={(y) => setWall(i, { start: { ...w.start, y } })} />
                </td>
                <td>
                  <NumInput ariaLabel="end x" value={w.end.x} onChange={(x) => setWall(i, { end: { ...w.end, x } })} />
                </td>
                <td>
                  <NumInput ariaLabel="end y" value={w.end.y} onChange={(y) => setWall(i, { end: { ...w.end, y } })} />
                </td>
                <td>
                  <NumInput ariaLabel="wall height" value={w.height_m} onChange={(height_m) => setWall(i, { height_m })} />
                </td>
                <td>
                  <NumInput ariaLabel="wall thickness" value={w.thickness_m} onChange={(thickness_m) => setWall(i, { thickness_m })} />
                </td>
                <td>
                  <input
                    value={w.material ?? ''}
                    aria-label="material"
                    placeholder="e.g. drywall"
                    onChange={(e) => setWall(i, { material: e.target.value.trim() ? e.target.value : null })}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label="target-room boundary"
                    checked={w.is_target_room_boundary}
                    onChange={(e) => setWall(i, { is_target_room_boundary: e.target.checked })}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() =>
                      setDraft({
                        ...draft,
                        walls: draft.walls.filter((_, j) => j !== i),
                        doors: draft.doors.filter((d) => d.wall_id !== w.id),
                      })
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h3 className="h3">Doors (drawn as gaps in their wall)</h3>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={draft.walls.length === 0}
        onClick={() =>
          setDraft({
            ...draft,
            doors: [
              ...draft.doors,
              { id: nextId('d', draft.doors.map((d) => d.id)), wall_id: draft.walls[0]?.id ?? '', offset_m: Number.NaN, width_m: 0.9, height_m: 2.0 },
            ],
          })
        }
      >
        Add door
      </button>
      {draft.doors.length > 0 && (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>id</th>
                <th>wall</th>
                <th>offset from wall start (m)</th>
                <th>width (m)</th>
                <th>height (m)</th>
                <th aria-label="actions" />
              </tr>
            </thead>
            <tbody>
              {draft.doors.map((d, i) => (
                <tr key={i}>
                  <td>
                    <input className="id-input" value={d.id} aria-label="door id" onChange={(e) => setDoor(i, { id: e.target.value })} />
                  </td>
                  <td>
                    <select value={d.wall_id} aria-label="door wall" onChange={(e) => setDoor(i, { wall_id: e.target.value })}>
                      {draft.walls.map((w) => (
                        <option key={w.id} value={w.id}>
                          {w.id}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <NumInput ariaLabel="door offset" value={d.offset_m} onChange={(offset_m) => setDoor(i, { offset_m })} />
                  </td>
                  <td>
                    <NumInput ariaLabel="door width" value={d.width_m} onChange={(width_m) => setDoor(i, { width_m })} />
                  </td>
                  <td>
                    <NumInput ariaLabel="door height" value={d.height_m} onChange={(height_m) => setDoor(i, { height_m })} />
                  </td>
                  <td>
                    <button type="button" className="btn btn--ghost btn--sm" onClick={() => setDraft({ ...draft, doors: draft.doors.filter((_, j) => j !== i) })}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function NodesStep({ draft, setDraft }: { draft: RoomGeometry; setDraft: (r: RoomGeometry) => void }) {
  const setNode = (i: number, patch: Partial<SensorNode>): void =>
    setDraft({ ...draft, nodes: draft.nodes.map((n, j) => (j === i ? { ...n, ...patch } : n)) });
  const renameNode = (i: number, newId: string): void => {
    const old = draft.nodes[i]?.id;
    setDraft({
      ...draft,
      nodes: draft.nodes.map((n, j) => (j === i ? { ...n, id: newId } : n)),
      // Keep links pointing at the renamed node.
      links: draft.links.map((l) => ({
        ...l,
        transmitter_id: l.transmitter_id === old ? newId : l.transmitter_id,
        receiver_id: l.receiver_id === old ? newId : l.receiver_id,
      })),
    });
  };
  const add = (role: NodeRole): void => {
    const prefix = role === 'TX' ? 'tx' : role === 'RX' ? 'rx' : 'router';
    const id = nextId(prefix, draft.nodes.map((n) => n.id));
    setDraft({
      ...draft,
      nodes: [
        ...draft.nodes,
        { id, role, label: id, position: { x: Number.NaN, y: Number.NaN, z: Number.NaN }, inside_target_room: null, device_mac: null },
      ],
    });
  };
  return (
    <div className="stack">
      <div className="row">
        {NODE_ROLES.map((r) => (
          <button key={r} type="button" className="btn btn--ghost btn--sm" onClick={() => add(r)}>
            Add {r}
          </button>
        ))}
      </div>
      <p className="hint">
        Node ids should match the receiver/transmitter ids in configs/roomsense.toml so links line up with live data.
      </p>
      <div className="table-wrap">
        <table className="table table--compact">
          <thead>
            <tr>
              <th>id</th>
              <th>role</th>
              <th>label</th>
              <th>x (m)</th>
              <th>y (m)</th>
              <th>z height (m)</th>
              <th>inside target room</th>
              <th>device MAC (optional)</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {draft.nodes.map((n, i) => (
              <tr key={i}>
                <td>
                  <input className="id-input" value={n.id} aria-label="node id" onChange={(e) => renameNode(i, e.target.value)} />
                </td>
                <td>
                  <select value={n.role} aria-label="node role" onChange={(e) => setNode(i, { role: e.target.value as NodeRole })}>
                    {NODE_ROLES.map((r) => (
                      <option key={r}>{r}</option>
                    ))}
                  </select>
                </td>
                <td>
                  <input value={n.label} aria-label="node label" onChange={(e) => setNode(i, { label: e.target.value })} />
                </td>
                <td>
                  <NumInput ariaLabel="node x" value={n.position.x} onChange={(x) => setNode(i, { position: { ...n.position, x } })} />
                </td>
                <td>
                  <NumInput ariaLabel="node y" value={n.position.y} onChange={(y) => setNode(i, { position: { ...n.position, y } })} />
                </td>
                <td>
                  <NumInput ariaLabel="node z" value={n.position.z} onChange={(z) => setNode(i, { position: { ...n.position, z } })} />
                </td>
                <td>
                  <select
                    aria-label="inside target room"
                    value={n.inside_target_room === null ? '' : n.inside_target_room ? 'yes' : 'no'}
                    onChange={(e) =>
                      setNode(i, { inside_target_room: e.target.value === '' ? null : e.target.value === 'yes' })
                    }
                  >
                    <option value="">not set</option>
                    <option value="yes">yes</option>
                    <option value="no">no</option>
                  </select>
                </td>
                <td>
                  <input
                    value={n.device_mac ?? ''}
                    aria-label="device MAC"
                    placeholder="aa:bb:cc:dd:ee:ff"
                    onChange={(e) => setNode(i, { device_mac: e.target.value.trim() ? e.target.value.trim() : null })}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() =>
                      setDraft({
                        ...draft,
                        nodes: draft.nodes.filter((_, j) => j !== i),
                        links: draft.links.filter((l) => l.transmitter_id !== n.id && l.receiver_id !== n.id),
                      })
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function LinksStep({ draft, setDraft }: { draft: RoomGeometry; setDraft: (r: RoomGeometry) => void }) {
  const txs = draft.nodes.filter((n) => n.role !== 'RX');
  const rxs = draft.nodes.filter((n) => n.role === 'RX');
  const setLink = (i: number, patch: Partial<LinkDef>): void =>
    setDraft({
      ...draft,
      links: draft.links.map((l, j) => {
        if (j !== i) return l;
        const next = { ...l, ...patch };
        // Keep the conventional id in sync unless the user customised it.
        if (l.link_id === defaultLinkId(l.transmitter_id, l.receiver_id) && !('link_id' in patch)) {
          next.link_id = defaultLinkId(next.transmitter_id, next.receiver_id);
        }
        return next;
      }),
    });
  return (
    <div className="stack">
      <p className="hint">
        A link is one radio path TX → RX. Link ids follow <code>transmitter_id-&gt;receiver_id</code> so they match the
        backend's link ids.
      </p>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        disabled={txs.length === 0 || rxs.length === 0}
        onClick={() => {
          const tx = txs[0]?.id ?? '';
          const rx = rxs[0]?.id ?? '';
          setDraft({ ...draft, links: [...draft.links, { link_id: defaultLinkId(tx, rx), transmitter_id: tx, receiver_id: rx }] });
        }}
      >
        Add link
      </button>
      {(txs.length === 0 || rxs.length === 0) && <p className="muted">Add at least one TX/ROUTER and one RX node first.</p>}
      <div className="table-wrap">
        <table className="table table--compact">
          <thead>
            <tr>
              <th>link id</th>
              <th>transmitter</th>
              <th>receiver</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {draft.links.map((l, i) => (
              <tr key={i}>
                <td>
                  <input value={l.link_id} aria-label="link id" onChange={(e) => setLink(i, { link_id: e.target.value })} />
                </td>
                <td>
                  <select value={l.transmitter_id} aria-label="transmitter" onChange={(e) => setLink(i, { transmitter_id: e.target.value })}>
                    {draft.nodes.map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.id} ({n.role})
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <select value={l.receiver_id} aria-label="receiver" onChange={(e) => setLink(i, { receiver_id: e.target.value })}>
                    {draft.nodes.map((n) => (
                      <option key={n.id} value={n.id}>
                        {n.id} ({n.role})
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <button type="button" className="btn btn--ghost btn--sm" onClick={() => setDraft({ ...draft, links: draft.links.filter((_, j) => j !== i) })}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ZonesStep({ draft, setDraft }: { draft: RoomGeometry; setDraft: (r: RoomGeometry) => void }) {
  const setZone = (i: number, patch: Partial<Zone>): void =>
    setDraft({ ...draft, zones: draft.zones.map((z, j) => (j === i ? { ...z, ...patch } : z)) });
  return (
    <div className="stack">
      <h3 className="h3">Target-room outline</h3>
      <p className="hint">The room you want to sense. Drawn in yellow in the 3-D view.</p>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() =>
          setDraft({
            ...draft,
            target_room_polygon: [
              { x: 0, y: 0 },
              { x: draft.width_m, y: 0 },
              { x: draft.width_m, y: draft.depth_m },
              { x: 0, y: draft.depth_m },
            ],
          })
        }
      >
        Use the room rectangle
      </button>
      <PointTable label="target room" points={draft.target_room_polygon} onChange={(pts) => setDraft({ ...draft, target_room_polygon: pts })} />

      <h3 className="h3">Zones</h3>
      <p className="hint">
        Zones are floor areas used only by the experimental zone estimator (capability C). A zone polygon is a label area,
        not a measured position.
      </p>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        onClick={() => {
          const id = nextId('z', draft.zones.map((z) => z.id));
          setDraft({ ...draft, zones: [...draft.zones, { id, label: id, kind: 'TARGET_ROOM_ZONE', polygon: [] }] });
        }}
      >
        Add zone
      </button>
      {draft.zones.map((z, i) => (
        <fieldset key={i} className="zone-editor">
          <legend>Zone {i + 1}</legend>
          <div className="form-grid">
            <label className="field">
              id
              <input value={z.id} onChange={(e) => setZone(i, { id: e.target.value })} />
            </label>
            <label className="field">
              label
              <input value={z.label} onChange={(e) => setZone(i, { label: e.target.value })} />
            </label>
            <label className="field">
              kind
              <select value={z.kind} onChange={(e) => setZone(i, { kind: e.target.value as Zone['kind'] })}>
                <option value="TARGET_ROOM_ZONE">inside target room</option>
                <option value="OUTSIDE_TARGET_ROOM">outside target room</option>
              </select>
            </label>
            <div className="field">
              <button type="button" className="btn btn--ghost btn--sm" onClick={() => setDraft({ ...draft, zones: draft.zones.filter((_, j) => j !== i) })}>
                Remove zone
              </button>
            </div>
          </div>
          <PointTable label={`zone ${z.id}`} points={z.polygon} onChange={(polygon) => setZone(i, { polygon })} />
        </fieldset>
      ))}
    </div>
  );
}

function SaveStep({ draft, origin, onSaved }: { draft: RoomGeometry; origin: Origin; onSaved: (r: RoomGeometry) => void }) {
  const issues = validateRoom(draft);
  const save = useAction((r: RoomGeometry) => api.saveRoom(r));
  return (
    <div className="stack">
      {origin === 'example' && (
        <Notice kind="warn">
          This draft started from the EXAMPLE geometry. Edit every value to match your real room before saving; the saved
          geometry is labelled USER PROVIDED.
        </Notice>
      )}
      <Notice kind="info">
        Saving replaces the room geometry (PUT /api/room) and marks it USER PROVIDED. Changing walls, nodes, links or zones
        invalidates calibrations and zone models tied to the previous configuration.
      </Notice>
      {issues.errors.length > 0 && (
        <div className="notice notice--error">
          <strong>Fix these before saving:</strong>
          <ul>
            {issues.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {issues.warnings.length > 0 && (
        <div className="notice notice--warn">
          <strong>Warnings:</strong>
          <ul>
            {issues.warnings.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      <button
        type="button"
        className="btn btn--primary"
        disabled={issues.errors.length > 0 || save.busy}
        onClick={async () => {
          const res = await save.run({ ...draft, provenance: 'USER_PROVIDED' });
          if (res) onSaved(res.room);
        }}
      >
        {save.busy ? 'Saving…' : 'Save room geometry'}
      </button>
      <ErrorNotice error={save.error} title="Save rejected" />
      {save.result && (
        <div className="notice notice--ok">
          <strong>Saved.</strong> Geometry is now {save.result.room.provenance.replace('_', ' ')}.
          <div className="mt">
            <strong>Invalidated by this change:</strong>
            <JsonView value={save.result.invalidated} nullLabel="nothing reported" />
          </div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ baseline & walk test

function CalibrationRecordView({ rec }: { rec: CalibrationRecord }) {
  return (
    <div className={`notice ${rec.valid ? 'notice--ok' : 'notice--error'}`}>
      <strong>{rec.valid ? 'Baseline accepted (valid)' : 'Baseline rejected'}</strong>
      <KeyValue
        items={[
          ['calibration id', <code key="id">{rec.calibration_id}</code>],
          ['kind', rec.kind],
          ['created', fmtUnixNs(rec.created_at_unix_ns)],
          ['source', rec.source_mode],
          ['links', rec.link_ids.join(', ') || 'none'],
          ['duration', fmtDuration(rec.duration_s)],
          ['frames / windows', `${rec.frame_count} / ${rec.window_count}`],
          ['reason', rec.invalidated_reason ?? (rec.valid ? '—' : 'no reason given')],
        ]}
      />
      {Object.keys(rec.summary).length > 0 && (
        <details className="details" open={!rec.valid}>
          <summary>Summary and per-link reasons</summary>
          <JsonView value={rec.summary} />
        </details>
      )}
    </div>
  );
}

function BaselineStep({ status }: { status: SystemStatus | null }) {
  const overview = useApi((sig) => api.calibration(sig), [], 1500);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const [selected, setSelected] = useState<string[] | null>(null);
  const start = useAction((ids: string[] | null) => api.baselineStart(ids));
  const stop = useAction(() => api.baselineStop());
  const cancel = useAction(() => api.baselineCancel());
  const links = status?.links.map((l) => l.link_id) ?? [];
  const chosen = selected ?? links;
  const inProgress = overview.data?.in_progress ?? null;
  const cal = status ? calibrationInfo(status) : null;

  return (
    <div className="stack">
      {cal && (
        <Notice kind={cal.valid ? 'ok' : 'warn'}>
          <strong>{cal.label}</strong> — {cal.detail}
        </Notice>
      )}
      <p>
        The quiet baseline records what each link looks like with <strong>nobody in the target room and nothing
        moving</strong>. Motion decisions compare against it. Leave the room (or stay perfectly still outside it) for the whole
        recording; the backend rejects baselines that are too short, too noisy or of low quality.
      </p>
      {!status || status.source_state !== 'RUNNING' ? (
        <Notice kind="warn">A running source is required. Start a Live (or Replay) source on the Dashboard first.</Notice>
      ) : null}
      {links.length > 0 && (
        <fieldset className="fieldset">
          <legend>Links to calibrate</legend>
          {links.map((id) => (
            <label key={id} className="check">
              <input
                type="checkbox"
                checked={chosen.includes(id)}
                onChange={(e) => setSelected(e.target.checked ? [...chosen, id] : chosen.filter((x) => x !== id))}
              />
              <code>{id}</code>
            </label>
          ))}
        </fieldset>
      )}
      <label className="check check--important">
        <input type="checkbox" checked={confirmEmpty} onChange={(e) => setConfirmEmpty(e.target.checked)} />
        <span>{EMPTY_ROOM_CONFIRM_TEXT}</span>
      </label>
      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!confirmEmpty || start.busy || !!inProgress}
          onClick={async () => {
            await start.run(selected === null ? null : chosen);
            overview.reload();
          }}
        >
          Start quiet baseline
        </button>
        <button
          type="button"
          className="btn"
          disabled={stop.busy}
          onClick={async () => {
            await stop.run();
            setConfirmEmpty(false);
            overview.reload();
          }}
        >
          Stop &amp; evaluate
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={cancel.busy}
          onClick={async () => {
            await cancel.run();
            setConfirmEmpty(false);
            overview.reload();
          }}
        >
          Cancel
        </button>
      </div>
      <ErrorNotice error={start.error} title="Baseline not started" />
      <ErrorNotice error={stop.error} title="Stop failed" />
      <ErrorNotice error={cancel.error} title="Cancel failed" />
      {start.result !== undefined && !start.error && inProgress === null && <Notice kind="info">Start requested.</Notice>}
      {inProgress && (
        <div className="notice notice--info">
          <strong>In progress</strong>
          <JsonView value={inProgress} />
        </div>
      )}
      {stop.result && <CalibrationRecordView rec={stop.result} />}

      <h3 className="h3">Calibration history</h3>
      <ErrorNotice error={overview.error} title="Could not load calibration state" />
      {overview.data?.active ? (
        <CalibrationRecordView rec={overview.data.active} />
      ) : (
        <p className="muted">No active calibration.</p>
      )}
      {overview.data && overview.data.history.length > 0 && (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>id</th>
                <th>kind</th>
                <th>created</th>
                <th>valid</th>
                <th>links</th>
                <th>reason</th>
              </tr>
            </thead>
            <tbody>
              {overview.data.history.slice(0, 50).map((r) => (
                <tr key={r.calibration_id}>
                  <td>
                    <code>{r.calibration_id}</code>
                  </td>
                  <td>{r.kind}</td>
                  <td>{fmtUnixNs(r.created_at_unix_ns)}</td>
                  <td>{r.valid ? 'yes' : 'no'}</td>
                  <td>{r.link_ids.join(', ')}</td>
                  <td>{r.invalidated_reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function WalkTestReportView({ report }: { report: WalkTestReport }) {
  return (
    <Card title="Walk test report (per link)" subtitle={report.note}>
      <p className="muted">
        Source {report.source_mode ?? 'unavailable'} · {fmtUnixNs(report.started_at_unix_ns)} → {fmtUnixNs(report.ended_at_unix_ns)}
      </p>
      {report.links.length === 0 ? (
        <p className="muted">No link results.</p>
      ) : (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>link</th>
                <th>detected</th>
                <th title="Heuristic score (unitless), not a probability">max score</th>
                <th>motion window fraction</th>
                <th>windows (motion / undecided / offline)</th>
                <th>reasons</th>
              </tr>
            </thead>
            <tbody>
              {report.links.map((l) => (
                <tr key={l.link_id}>
                  <td>
                    <code>{l.link_id}</code>
                  </td>
                  <td>{l.detected ? <span className="evidence evidence--yes">yes</span> : <span className="evidence evidence--no">no</span>}</td>
                  <td>{fmtNum(l.max_score, 2)}</td>
                  <td>{fmtPercent(l.motion_window_fraction)}</td>
                  <td>
                    {l.windows} ({l.motion_windows} / {l.undecided_windows} / {l.offline_windows})
                  </td>
                  <td>{l.reasons.join('; ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function WalkTestStep({ status }: { status: SystemStatus | null }) {
  const start = useAction(() => api.walkTestStart());
  const stop = useAction(() => api.walkTestStop());
  const [running, setRunning] = useState(false);
  return (
    <div className="stack">
      <p>
        The walk test checks that each link reacts to <strong>one person walking</strong> through the target room after a
        valid baseline. Walk slowly along the room for 30–60 s, then press Stop. The per-link results are measured on this
        session only; they are not a through-wall validation.
      </p>
      {!status?.calibration_valid && <Notice kind="warn">No valid quiet baseline: the walk test results will be UNKNOWN.</Notice>}
      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          disabled={running || start.busy}
          onClick={async () => {
            const r = await start.run();
            if (r !== undefined) setRunning(true);
          }}
        >
          Start walk test
        </button>
        <button
          type="button"
          className="btn"
          disabled={stop.busy}
          onClick={async () => {
            await stop.run();
            setRunning(false);
          }}
        >
          Stop &amp; show results
        </button>
      </div>
      {running && <Notice kind="info">Walk test running — walk through the target room now.</Notice>}
      <ErrorNotice error={start.error} title="Walk test not started" />
      <ErrorNotice error={stop.error} title="Walk test stop failed" />
      {stop.result && <WalkTestReportView report={stop.result} />}
    </div>
  );
}

// ------------------------------------------------------------------ page

export function CalibrationPage({ live }: { live: LiveSnapshot }) {
  const { room, error: roomError, setRoom } = useRoom();
  const [step, setStep] = useState<Step>('room');
  const [draft, setDraft] = useState<RoomGeometry | null>(null);
  const [origin, setOrigin] = useState<Origin>('saved');
  const example = useAction((sig?: AbortSignal) => api.roomExample(sig));
  const status = live.connection === 'open' ? live.status : null;

  // Initialise the draft from the saved geometry once it loads.
  useEffect(() => {
    if (draft === null && room) {
      setDraft(cloneRoom(room));
      setOrigin(room.provenance === 'EXAMPLE' ? 'example' : 'saved');
    }
  }, [room, draft]);

  const geometryStep = step === 'room' || step === 'walls' || step === 'nodes' || step === 'links' || step === 'zones' || step === 'save';

  return (
    <div className="page">
      <Card
        title="Calibration"
        subtitle="Room geometry is entered by you. RoomSense never reconstructs walls or positions from Wi-Fi."
        actions={
          <div className="row">
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => {
                if (draft && !window.confirm('Discard the current draft and start from a blank room?')) return;
                setDraft(blankRoom());
                setOrigin('blank');
              }}
            >
              Start from blank
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={example.busy}
              onClick={async () => {
                if (draft && !window.confirm('Discard the current draft and load the EXAMPLE geometry?')) return;
                const ex = await example.run();
                if (ex) {
                  setDraft(cloneRoom(ex));
                  setOrigin('example');
                }
              }}
            >
              Load EXAMPLE geometry
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={!room}
              onClick={() => {
                if (room) {
                  setDraft(cloneRoom(room));
                  setOrigin(room.provenance === 'EXAMPLE' ? 'example' : 'saved');
                }
              }}
            >
              Reset to saved
            </button>
          </div>
        }
      >
        <nav className="stepper" aria-label="Calibration steps">
          {STEPS.map((s) => (
            <button key={s.id} type="button" className={`stepper__step ${step === s.id ? 'stepper__step--active' : ''}`} aria-current={step === s.id ? 'step' : undefined} onClick={() => setStep(s.id)}>
              {s.label}
            </button>
          ))}
        </nav>
        <ErrorNotice error={roomError} title="Could not load the saved room" />
        <ErrorNotice error={example.error} title="Could not load the EXAMPLE geometry" />
        {draft && geometryStep && (
          <div className={`draft-origin draft-origin--${origin}`}>
            Draft based on:{' '}
            {origin === 'example' ? (
              <strong>EXAMPLE GEOMETRY (sample data — not your room)</strong>
            ) : origin === 'blank' ? (
              <strong>a blank room</strong>
            ) : (
              <strong>the saved USER PROVIDED geometry</strong>
            )}
          </div>
        )}
      </Card>

      {geometryStep && !draft && <p className="muted">Loading room geometry… (or start from blank)</p>}

      {geometryStep && draft && (
        <div className="calib-layout">
          <Card title={STEPS.find((s) => s.id === step)?.label}>
            {step === 'room' && <RoomStep draft={draft} setDraft={setDraft} />}
            {step === 'walls' && <WallsStep draft={draft} setDraft={setDraft} />}
            {step === 'nodes' && <NodesStep draft={draft} setDraft={setDraft} />}
            {step === 'links' && <LinksStep draft={draft} setDraft={setDraft} />}
            {step === 'zones' && <ZonesStep draft={draft} setDraft={setDraft} />}
            {step === 'save' && (
              <SaveStep
                draft={draft}
                origin={origin}
                onSaved={(r) => {
                  setRoom(r);
                  setDraft(cloneRoom(r));
                  setOrigin('saved');
                }}
              />
            )}
          </Card>
          <Card title="Preview">
            <RoomPreview2D room={draft} />
          </Card>
        </div>
      )}

      {step === 'baseline' && (
        <Card title="7 · Quiet baseline">
          <BaselineStep status={status} />
        </Card>
      )}
      {step === 'walk' && (
        <Card title="8 · Walk test">
          <WalkTestStep status={status} />
        </Card>
      )}
    </div>
  );
}
