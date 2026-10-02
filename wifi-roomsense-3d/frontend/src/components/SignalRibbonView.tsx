/**
 * React wrapper for the 3-D signal ribbon (a chart of RSSI over time).
 */

import { useEffect, useRef, useState } from 'react';
import { type RibbonPoint } from '../lib/ribbon';
import { SignalRibbonScene } from '../scene/SignalRibbonScene';

export function SignalRibbonView({ rssi, noise }: { rssi: RibbonPoint[][]; noise: RibbonPoint[][] }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<SignalRibbonScene | null>(null);
  const [webglError, setWebglError] = useState<string | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    let scene: SignalRibbonScene;
    try {
      scene = new SignalRibbonScene(host);
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
    sceneRef.current?.setData(rssi, noise);
  }, [rssi, noise]);

  return (
    <div className="room-view">
      <div className="room-view__toolbar">
        <span className="muted">Drag to rotate · scroll to zoom · right-drag to pan</span>
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => sceneRef.current?.resetView()}>
          Reset view
        </button>
      </div>
      <div className="room-view__stage ribbon-view__stage">
        {webglError ? (
          <div className="notice notice--error">3-D view unavailable in this browser (WebGL): {webglError}</div>
        ) : (
          <div ref={hostRef} className="room-view__host" />
        )}
        <div className="ribbon-view__caption">
          3-D chart of your signal strength over time: not a picture of your room, not a position.
        </div>
      </div>
    </div>
  );
}
