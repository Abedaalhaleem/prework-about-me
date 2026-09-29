/**
 * React wrapper around the imperative RoomScene: mount, resize, dispose, and
 * the overlays that must always be visible (geometry provenance, simulated
 * watermark, zone gating reason, legend).
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { RoomGeometry } from '../api/types';
import type { LiveSnapshot } from '../api/ws';
import { RoomScene, type LinkVisual, type ViewPreset } from '../scene/RoomScene';
import { SIMULATED_BANNER_TEXT, isSimulated } from '../lib/banner';
import { type LinkFreshness, formatAge, linkDisplayAgeS, linkDisplayState, linkFreshness } from '../lib/freshness';
import { LEGEND_ORDER, linkStyle } from '../lib/linkStyle';
import { zoneDisplayDecision } from '../lib/zone';

export function geometryProvenanceLabel(room: RoomGeometry | null): string {
  if (!room) return 'NO ROOM GEOMETRY LOADED';
  return room.provenance === 'USER_PROVIDED' ? 'USER PROVIDED GEOMETRY — not Wi-Fi reconstructed' : 'EXAMPLE GEOMETRY';
}

export function RoomView({
  room,
  roomError,
  live,
  now,
}: {
  room: RoomGeometry | null;
  roomError: string | null;
  live: LiveSnapshot;
  now: number;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const geoLabelRef = useRef<HTMLDivElement>(null);
  const zoneNoteRef = useRef<HTMLDivElement>(null);
  const redrawRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<RoomScene | null>(null);
  const [webglError, setWebglError] = useState<string | null>(null);
  const [wallOpacity, setWallOpacity] = useState(0.35);
  const [preset, setPreset] = useState<ViewPreset>('iso');
  const [extrude, setExtrude] = useState(false);
  const [redrawRate, setRedrawRate] = useState<number | null>(null);
  const [hiddenLabels, setHiddenLabels] = useState(0);

  const connected = live.connection === 'open' && live.status !== null && live.statusReceivedAtMs !== null;
  const status = connected ? live.status : null;

  // Mount / unmount the three.js scene.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    let scene: RoomScene;
    try {
      scene = new RoomScene(host);
    } catch (err) {
      setWebglError(err instanceof Error ? err.message : String(err));
      return undefined;
    }
    sceneRef.current = scene;
    scene.setRenderRateListener((r) => setRedrawRate(r));
    scene.setLabelCullListener((n) => setHiddenLabels(n));
    // Overlays drawn over the view: scene labels are kept off them.
    scene.setLabelObstacles(
      [geoLabelRef.current, zoneNoteRef.current, redrawRef.current].filter((e): e is HTMLDivElement => e !== null),
    );
    const ro =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver((entries) => {
            const rect = entries[0]?.contentRect;
            if (rect) scene.resize(rect.width, rect.height);
          });
    ro?.observe(host);
    return () => {
      ro?.disconnect();
      scene.dispose();
      sceneRef.current = null;
    };
  }, []);

  useEffect(() => {
    sceneRef.current?.setRoom(room);
  }, [room]);

  useEffect(() => {
    sceneRef.current?.setWallOpacity(wallOpacity);
  }, [wallOpacity, room]);

  useEffect(() => {
    sceneRef.current?.setViewPreset(preset);
  }, [preset]);

  // Per-link visuals: state from the backend, downgraded to STALE/NO_DATA
  // client-side (on the result's own age, not on frame arrivals). The age in
  // the label is the older of the state age and the measurement age, so a
  // STALE or OFFLINE label never looks fresher than its data.
  const visuals: LinkVisual[] = useMemo(() => {
    if (!room) return [];
    const fresh: Map<string, LinkFreshness> =
      status && live.statusReceivedAtMs !== null ? linkFreshness(status, live.statusReceivedAtMs, now) : new Map();
    return room.links.map((def) => {
      const act = status?.activity.find((a) => a.link_id === def.link_id);
      const f = fresh.get(def.link_id);
      const state = linkDisplayState(act, f?.stateAgeS ?? null, status?.stale_clear_timeout_s ?? 0, connected, f?.measurementAgeS ?? null);
      const st = linkStyle(state);
      const age = connected ? linkDisplayAgeS(f, state) : null;
      return { linkId: def.link_id, state, label: age !== null ? `${st.short} · ${formatAge(age)}` : st.short };
    });
  }, [room, status, live.statusReceivedAtMs, now, connected]);

  useEffect(() => {
    sceneRef.current?.setLinks(visuals);
  }, [visuals]);

  const zone = useMemo(
    () => zoneDisplayDecision(status, room, live.statusReceivedAtMs, now),
    [status, room, live.statusReceivedAtMs, now],
  );
  useEffect(() => {
    sceneRef.current?.setZoneHighlight(
      zone.show && zone.zoneId ? { zoneId: zone.zoneId, label: zone.zoneLabel ?? zone.zoneId } : null,
      extrude,
    );
  }, [zone.show, zone.zoneId, zone.zoneLabel, extrude]);

  // The footer note is an obstacle for scene labels: re-place them when it changes size.
  useEffect(() => {
    sceneRef.current?.invalidateLabels();
  }, [zone.reason, zone.show, extrude, room]);

  const simulated = status ? isSimulated(status) : false;
  const unplaced = status
    ? status.links.map((l) => l.link_id).filter((id) => !room?.links.some((d) => d.link_id === id))
    : [];

  return (
    <div className="room-view">
      <div className="room-view__toolbar" role="toolbar" aria-label="3-D view controls">
        <div className="segmented" role="group" aria-label="View preset">
          <button type="button" aria-pressed={preset === 'iso'} onClick={() => setPreset('iso')}>
            Isometric
          </button>
          <button type="button" aria-pressed={preset === 'top'} onClick={() => setPreset('top')}>
            Top-down
          </button>
        </div>
        <button type="button" className="btn btn--ghost" onClick={() => sceneRef.current?.resetView()}>
          Reset view
        </button>
        <label className="slider">
          <span>Wall opacity</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={wallOpacity}
            onChange={(e) => setWallOpacity(Number(e.target.value))}
            aria-valuetext={`${Math.round(wallOpacity * 100)} %`}
          />
          <span className="slider__value">{Math.round(wallOpacity * 100)}%</span>
        </label>
        <label className="check" title="Adds a decorative prism above an estimated zone. It is not a measured height.">
          <input type="checkbox" checked={extrude} onChange={(e) => setExtrude(e.target.checked)} />
          <span>Decorative zone extrusion</span>
        </label>
      </div>

      <div className="room-view__stage">
        <div className="room-view__host" ref={hostRef} />

        <div
          ref={geoLabelRef}
          className={`geo-label ${room?.provenance === 'USER_PROVIDED' ? 'geo-label--user' : 'geo-label--example'}`}
        >
          {geometryProvenanceLabel(room)}
          {room && <span className="geo-label__name">{room.name}</span>}
        </div>

        {simulated && (
          <div className="sim-watermark" aria-hidden="true">
            <span>{SIMULATED_BANNER_TEXT}</span>
          </div>
        )}

        {!connected && (
          <div className="stage-overlay stage-overlay--offline" role="status">
            NO CONNECTION TO BACKEND — link states are not shown
          </div>
        )}

        {webglError && (
          <div className="stage-overlay stage-overlay--error" role="alert">
            3-D view unavailable: WebGL could not be initialised ({webglError}). Status, links and plots still work.
          </div>
        )}
        {roomError && !room && (
          <div className="stage-overlay stage-overlay--error" role="alert">
            Room geometry could not be loaded: {roomError}
          </div>
        )}

        <div className="stage-footer">
          <div ref={zoneNoteRef} className={`zone-note ${zone.show ? 'zone-note--estimate' : ''}`}>
            {zone.show ? (
              <>
                <strong>Estimated zone (experimental): {zone.zoneLabel}</strong> — Zone centre is a display anchor, not a
                measured position.
                {extrude && <em> Extrusion: Visualization only — not measured height.</em>}
              </>
            ) : (
              <>{zone.reason}</>
            )}
          </div>
          <div ref={redrawRef} className="redraw-rate" title="How often the 3-D view is redrawn. This is NOT the measurement rate.">
            view redraws: {redrawRate === null ? '—' : `${redrawRate.toFixed(0)}/s`} (display only)
            {hiddenLabels > 0 && (
              <span className="redraw-rate__culled" title="Lower-priority labels (zones, compass, target room) hidden so the others stay readable. Zoom in to see them.">
                {' '}
                · {hiddenLabels} label{hiddenLabels === 1 ? '' : 's'} hidden to avoid overlap
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="room-view__legend">
        <div className="legend-group">
          <span className="legend-title">Link state (per link, not a map of the room)</span>
          {LEGEND_ORDER.map((s) => {
            const st = linkStyle(s);
            return (
              <span key={s} className="legend-chip" title={st.description}>
                <span className={`legend-line ${st.dashed ? 'legend-line--dashed' : ''}`} style={{ color: st.css }} />
                {st.short}
              </span>
            );
          })}
        </div>
        <div className="legend-group">
          <span className="legend-title">Nodes</span>
          <span className="legend-chip">
            <span className="node-glyph node-glyph--tx" /> TX (pyramid)
          </span>
          <span className="legend-chip">
            <span className="node-glyph node-glyph--rx" /> RX (cube)
          </span>
          <span className="legend-chip">
            <span className="node-glyph node-glyph--router" /> Router (puck)
          </span>
        </div>
        {unplaced.length > 0 && (
          <div className="legend-group legend-group--warn">
            Not drawn (not placed in the room geometry): {unplaced.map((id) => <code key={id}>{id}</code>)}
          </div>
        )}
      </div>
    </div>
  );
}
