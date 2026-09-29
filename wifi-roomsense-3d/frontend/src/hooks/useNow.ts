import { useEffect, useState } from 'react';

/**
 * Re-render every `intervalMs` with the current client time. Used so that
 * measurement ages keep growing between status messages (a frozen age would
 * make old data look fresh).
 */
export function useNow(intervalMs = 500): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
