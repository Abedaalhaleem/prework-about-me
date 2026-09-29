/**
 * Top-down 2-D preview of a RoomGeometry draft (north up, 1 m grid).
 * It shows exactly what the user typed; nothing is inferred.
 */

import { useEffect, useRef } from 'react';
import type { RoomGeometry, Vec2 } from '../api/types';
import { pointAlongWall, polygonCentroid, roomBounds, wallSpans } from '../lib/coords';

const C = {
  bg: '#0f151c',
  grid: 'rgba(148,163,184,0.13)',
  gridText: '#7b8494',
  wall: '#b8c6d9',
  door: '#e0b050',
  target: '#ffd43b',
  zone: 'rgba(116,143,252,0.18)',
  zoneLine: '#748ffc',
  zoneOutside: 'rgba(134,142,150,0.14)',
  link: 'rgba(233,236,239,0.45)',
  tx: '#c678dd',
  rx: '#e9ecef',
  router: '#69db7c',
  text: '#dfe5ee',
};

export function RoomPreview2D({ room, height = 320 }: { room: RoomGeometry; height?: number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return undefined;
    const paint = (): void => {
      const width = Math.max(200, wrap.clientWidth);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = C.bg;
      ctx.fillRect(0, 0, width, height);

      let b;
      try {
        b = roomBounds(room);
      } catch {
        return;
      }
      if (![b.minX, b.maxX, b.minY, b.maxY].every(Number.isFinite)) {
        ctx.fillStyle = C.text;
        ctx.fillText('Enter valid numbers to see the preview', 12, 20);
        return;
      }
      const pad = 28;
      const spanX = Math.max(0.5, b.maxX - b.minX);
      const spanY = Math.max(0.5, b.maxY - b.minY);
      const scale = Math.min((width - 2 * pad) / spanX, (height - 2 * pad) / spanY);
      const ox = pad + ((width - 2 * pad) - spanX * scale) / 2;
      const oy = pad + ((height - 2 * pad) - spanY * scale) / 2;
      // North up: larger y is higher on screen.
      const X = (x: number): number => ox + (x - b.minX) * scale;
      const Y = (y: number): number => oy + (b.maxY - y) * scale;
      const poly = (pts: readonly Vec2[]): void => {
        ctx.beginPath();
        pts.forEach((p, i) => (i === 0 ? ctx.moveTo(X(p.x), Y(p.y)) : ctx.lineTo(X(p.x), Y(p.y))));
        ctx.closePath();
      };

      // 1 m grid
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.fillStyle = C.gridText;
      ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
      for (let gx = Math.ceil(b.minX); gx <= b.maxX; gx++) {
        ctx.beginPath();
        ctx.moveTo(X(gx) + 0.5, Y(b.minY));
        ctx.lineTo(X(gx) + 0.5, Y(b.maxY));
        ctx.stroke();
        ctx.fillText(`${gx}`, X(gx) - 3, Y(b.minY) + 12);
      }
      for (let gy = Math.ceil(b.minY); gy <= b.maxY; gy++) {
        ctx.beginPath();
        ctx.moveTo(X(b.minX), Y(gy) + 0.5);
        ctx.lineTo(X(b.maxX), Y(gy) + 0.5);
        ctx.stroke();
        ctx.fillText(`${gy}`, X(b.minX) - 14, Y(gy) + 3);
      }
      ctx.fillText('x (m) → east', X(b.maxX) - 70, Y(b.minY) + 24);
      ctx.fillText('y (m) ↑ north', X(b.minX) - 20, Y(b.maxY) - 10);

      // Zones
      room.zones.forEach((z) => {
        if (z.polygon.length < 3) return;
        poly(z.polygon);
        ctx.fillStyle = z.kind === 'OUTSIDE_TARGET_ROOM' ? C.zoneOutside : C.zone;
        ctx.fill();
        ctx.strokeStyle = C.zoneLine;
        ctx.setLineDash([4, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        const c = polygonCentroid(z.polygon);
        if (c) {
          ctx.fillStyle = C.text;
          ctx.textAlign = 'center';
          ctx.fillText(z.label || z.id, X(c.x), Y(c.y));
          ctx.textAlign = 'start';
        }
      });

      // Target room outline
      if (room.target_room_polygon.length >= 3) {
        poly(room.target_room_polygon);
        ctx.strokeStyle = C.target;
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      // Walls with door gaps
      room.walls.forEach((w) => {
        const spans = wallSpans(w, room.doors);
        ctx.strokeStyle = C.wall;
        ctx.lineWidth = Math.max(2, w.thickness_m * scale);
        ctx.lineCap = 'butt';
        spans.solid.forEach(([a, bb]) => {
          const p0 = pointAlongWall(w, a);
          const p1 = pointAlongWall(w, bb);
          ctx.beginPath();
          ctx.moveTo(X(p0.x), Y(p0.y));
          ctx.lineTo(X(p1.x), Y(p1.y));
          ctx.stroke();
        });
        ctx.strokeStyle = C.door;
        ctx.lineWidth = 1.5;
        spans.openings.forEach((o) => {
          const p0 = pointAlongWall(w, o.from);
          const p1 = pointAlongWall(w, o.to);
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(X(p0.x), Y(p0.y));
          ctx.lineTo(X(p1.x), Y(p1.y));
          ctx.stroke();
          ctx.setLineDash([]);
        });
      });

      // Links
      const nodes = new Map(room.nodes.map((n) => [n.id, n]));
      ctx.strokeStyle = C.link;
      ctx.lineWidth = 1.5;
      room.links.forEach((l) => {
        const a = nodes.get(l.transmitter_id);
        const c = nodes.get(l.receiver_id);
        if (!a || !c) return;
        ctx.beginPath();
        ctx.moveTo(X(a.position.x), Y(a.position.y));
        ctx.lineTo(X(c.position.x), Y(c.position.y));
        ctx.stroke();
      });

      // Nodes: TX triangle, RX square, ROUTER circle
      room.nodes.forEach((n) => {
        if (!Number.isFinite(n.position.x) || !Number.isFinite(n.position.y)) return;
        const x = X(n.position.x);
        const y = Y(n.position.y);
        ctx.beginPath();
        if (n.role === 'TX') {
          ctx.fillStyle = C.tx;
          ctx.moveTo(x, y - 7);
          ctx.lineTo(x + 6.5, y + 5);
          ctx.lineTo(x - 6.5, y + 5);
          ctx.closePath();
        } else if (n.role === 'ROUTER') {
          ctx.fillStyle = C.router;
          ctx.arc(x, y, 6, 0, Math.PI * 2);
        } else {
          ctx.fillStyle = C.rx;
          ctx.rect(x - 5.5, y - 5.5, 11, 11);
        }
        ctx.fill();
        ctx.fillStyle = C.text;
        ctx.fillText(`${n.role} ${n.label || n.id} (z ${Number.isFinite(n.position.z) ? n.position.z : '?'} m)`, x + 9, y - 8);
      });
    };
    paint();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(paint);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [room, height]);

  return (
    <div className="preview2d" ref={wrapRef}>
      <canvas ref={canvasRef} role="img" aria-label="Top-down preview of the room draft (north up, 1 m grid)" />
      <div className="preview2d__caption">
        Top-down preview · north up · 1 m grid · {room.provenance === 'EXAMPLE' ? 'EXAMPLE GEOMETRY' : 'USER PROVIDED GEOMETRY'}
      </div>
    </div>
  );
}
