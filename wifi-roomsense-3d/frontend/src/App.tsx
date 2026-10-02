/**
 * App shell: permanent status header + simple tab navigation (no router).
 * The selected tab is kept in the URL hash so reloads return to it.
 */

import { useEffect, useState } from 'react';
import { useLiveStatus } from './api/ws';
import { StatusHeader } from './components/StatusHeader';
import { RoomProvider } from './hooks/RoomContext';
import { useNow } from './hooks/useNow';
import { CalibrationPage } from './pages/CalibrationPage';
import { CapabilitiesPage } from './pages/CapabilitiesPage';
import { DashboardPage } from './pages/DashboardPage';
import { HardwarePage } from './pages/HardwarePage';
import { RecordingsPage } from './pages/RecordingsPage';
import { SettingsPage } from './pages/SettingsPage';
import { ValidationPage } from './pages/ValidationPage';
import { WavesPage } from './pages/WavesPage';
import { WifiSignalPage } from './pages/WifiSignalPage';

const TABS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'calibration', label: 'Calibration' },
  { id: 'recordings', label: 'Recordings & Replay' },
  { id: 'validation', label: 'Validation' },
  { id: 'capabilities', label: 'Capabilities & Research' },
  { id: 'hardware', label: 'Hardware' },
  { id: 'mywifi', label: 'My Wi-Fi signal' },
  { id: 'waves', label: 'Wi-Fi waves (simulated)' },
  { id: 'settings', label: 'Settings' },
] as const;

type TabId = (typeof TABS)[number]['id'];

function tabFromHash(): TabId {
  const h = window.location.hash.replace(/^#\/?/, '');
  return (TABS.find((t) => t.id === h)?.id ?? 'dashboard') as TabId;
}

export function App() {
  const live = useLiveStatus();
  const now = useNow(500);
  const [tab, setTab] = useState<TabId>(tabFromHash);

  useEffect(() => {
    const onHash = (): void => setTab(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const go = (id: TabId): void => {
    if (window.location.hash !== `#${id}`) window.location.hash = id;
    setTab(id);
  };

  const connected = live.connection === 'open' && live.status !== null;

  return (
    <RoomProvider backendConnected={connected}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <StatusHeader live={live} now={now} onOpenCapabilities={() => go('capabilities')} />
      <nav className="tabs" aria-label="Pages">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`tabs__tab ${tab === t.id ? 'tabs__tab--active' : ''}`}
            aria-current={tab === t.id ? 'page' : undefined}
            onClick={() => go(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <main id="main" className="main">
        {tab === 'dashboard' && <DashboardPage live={live} now={now} />}
        {tab === 'mywifi' && <WifiSignalPage />}
        {tab === 'waves' && <WavesPage />}
        {tab === 'calibration' && <CalibrationPage live={live} />}
        {tab === 'recordings' && <RecordingsPage live={live} />}
        {tab === 'validation' && <ValidationPage live={live} />}
        {tab === 'capabilities' && <CapabilitiesPage live={live} />}
        {tab === 'hardware' && <HardwarePage live={live} />}
        {tab === 'settings' && <SettingsPage />}
      </main>
      <footer className="footer">
        WiFi RoomSense 3D · local only · experimental. A 3-D interface is not proof of 3-D reconstruction: walls, zones and
        nodes are user-entered; only per-link activity is measured.
      </footer>
    </RoomProvider>
  );
}
