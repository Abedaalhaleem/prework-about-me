/**
 * Settings: the optional API token for LAN mode. It is kept in
 * sessionStorage only (cleared when the tab closes), never logged, never put
 * in a URL, and never shown back in full.
 */

import { useState } from 'react';
import { clearToken, readToken, saveToken } from '../api/client';
import { Card, ErrorNotice, Notice } from '../components/ui';

export function SettingsPage() {
  const [value, setValue] = useState('');
  const [hasToken, setHasToken] = useState(() => readToken() !== null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  return (
    <div className="page">
      <Card
        title="API token"
        subtitle="Needed whenever the server was started with ROOMSENSE_API_TOKEN set (always the case in LAN mode, and then on 127.0.0.1 too). If the server runs without a token, leave this empty."
      >
        <div className="stack">
          <Notice kind="info">
            The token is stored in this tab's sessionStorage only and is sent as an <code>Authorization: Bearer</code> header
            (and as a WebSocket subprotocol, since browsers cannot set headers on WebSockets). It is never logged or put in a
            URL. Never expose RoomSense to the internet.
          </Notice>
          <p>
            Current: <strong>{hasToken ? 'a token is set for this session' : 'no token'}</strong>
          </p>
          <label className="field">
            New token
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                setSaved(false);
              }}
            />
          </label>
          <div className="row">
            <button
              type="button"
              className="btn btn--primary"
              disabled={value.trim().length === 0}
              onClick={() => {
                try {
                  saveToken(value);
                  setValue('');
                  setHasToken(true);
                  setSaved(true);
                  setError(null);
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              Use token for this session
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!hasToken}
              onClick={() => {
                clearToken();
                setHasToken(false);
                setSaved(false);
              }}
            >
              Clear token
            </button>
          </div>
          {saved && <Notice kind="ok">Token set. The live connection reconnects with it.</Notice>}
          <ErrorNotice error={error} title="Could not store the token" />
        </div>
      </Card>
      <Card title="Privacy">
        <ul className="compact-list">
          <li>All data stays on this computer. The UI talks only to the local backend under /api.</li>
          <li>Use RoomSense only in spaces you control, with everyone present informed and agreeing.</li>
          <li>No identification, people counting or continuous tracking is performed.</li>
        </ul>
      </Card>
    </div>
  );
}
