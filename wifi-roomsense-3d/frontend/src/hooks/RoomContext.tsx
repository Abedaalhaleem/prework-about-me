import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, errorMessage } from '../api/client';
import type { RoomGeometry } from '../api/types';

interface RoomContextValue {
  /** Geometry from GET /api/room (USER_PROVIDED, or the backend's EXAMPLE). */
  room: RoomGeometry | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  /** Replace after a successful PUT /api/room. */
  setRoom: (room: RoomGeometry) => void;
}

const RoomCtx = createContext<RoomContextValue | null>(null);

export function RoomProvider({ children, backendConnected }: { children: ReactNode; backendConnected: boolean }) {
  const [room, setRoomState] = useState<RoomGeometry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    api
      .room(ctrl.signal)
      .then((r) => {
        setRoomState(r);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setError(errorMessage(err));
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
    // Refetch when the backend comes (back) online.
  }, [tick, backendConnected]);

  const value = useMemo<RoomContextValue>(
    () => ({ room, error, loading, reload, setRoom: setRoomState }),
    [room, error, loading, reload],
  );
  return <RoomCtx.Provider value={value}>{children}</RoomCtx.Provider>;
}

export function useRoom(): RoomContextValue {
  const v = useContext(RoomCtx);
  if (!v) throw new Error('useRoom must be used inside <RoomProvider>');
  return v;
}
