/**
 * React wrapper for WaveScene (the SIMULATED Wi-Fi coverage/wave view).
 * Always shows the SIMULATED watermark and the geometry provenance label.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { RoomGeometry, Vec2 } from '../api/types';
import { type PropagationParams, gridRange, predictGrid } from '../lib/propagation';
import { type LaptopMarker, WaveScene } from '../scene/WaveScene';
import { geometryProvenanceLabel } from './RoomView';

export function WaveView({
  room,
  tx,
  txHeight,
  txLabel,
  params,
  laptop,
}: {
  room: RoomGeometry;
  tx: Vec2;
  txHeight: number;
  txLabel: string;
  params: PropagationParams;
  laptop: LaptopMarker | null;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<WaveScene | null>(null);
  const [webglError, setWebglError] = useState<string | null>(null);
  const [animate, setAnimate] = useState(true);
  // Same grid as the scene draws; only used for the legend numbers.
  const range = useMemo(() => gridRange(predictGrid(room, tx, params)), [room, tx, params]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    let scene: WaveScene;
    try {
      scene = new WaveScene(host);
    } catch (err) {
      setWebglError(err instanceof Error ? err.message : String(err));
      return undefined;
    }
    sceneRef.current = scene;
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
    sceneRef.current?.setModel(room, tx, txHeight, txLabel, params, laptop);
  }, [room, tx, txHeight, txLabel, params, laptop, webglError]);

  useEffect(() => {
    sceneRef.current?.setAnimate(animate);
  }, [animate]);

  return (
    <div className="room-view">
      <div className="room-view__toolbar">
        <span className="muted">Drag to rotate · scroll to zoom · right-drag to pan</span>
        <label className="check">
          <input type="checkbox" checked={animate} onChange={(e) => setAnimate(e.target.checked)} /> Animate rings
        </label>
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => sceneRef.current?.resetView()}>
          Reset view
        </button>
      </div>
      <div className="room-view__stage wave-view__stage">
        {webglError ? (
          <div className="stage-overlay stage-overlay--error" role="alert">
            3-D view unavailable: WebGL could not be initialised ({webglError}).
          </div>
        ) : (
          <div ref={hostRef} className="room-view__host" />
        )}
        <div className={`geo-label ${room.provenance === 'USER_PROVIDED' ? 'geo-label--user' : 'geo-label--example'}`}>
          {geometryProvenanceLabel(room)}
          <span className="geo-label__name">{room.name}</span>
        </div>
        <div className="sim-watermark" aria-hidden="true">
          <span>SIMULATED</span>
        </div>
        <div className="wave-legend" aria-label="colour scale of the simulated level">
          <span>predicted {range.hi.toFixed(0)} dBm</span>
          <span className="wave-legend__bar" />
          <span>{range.lo.toFixed(0)} dBm</span>
        </div>
        <div className="ribbon-view__caption">
          Computed from the room layout and assumed wall losses. Not measured. Cannot show people or objects.
        </div>
      </div>
    </div>
  );
}
