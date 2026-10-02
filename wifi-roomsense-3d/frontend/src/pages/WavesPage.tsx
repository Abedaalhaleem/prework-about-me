/**
 * Wi-Fi waves (SIMULATED): a 3-D picture of how Wi-Fi would spread through
 * the room layout under a simple textbook model, with the walls it passes
 * through. Optionally compared with this computer's real measured RSSI at a
 * spot the user marks. The model measures nothing and cannot show people or
 * objects; it is kept off the dashboard and away from the measured 3-D room.
 */

import { useMemo, useState } from 'react';
import { api } from '../api/client';
import type { Vec2 } from '../api/types';
import { NumInput } from '../components/fields';
import { Card, ErrorNotice, KeyValue, Notice, Unavailable } from '../components/ui';
import { WaveView } from '../components/WaveView';
import { useAction, useApi } from '../hooks/useApi';
import { useRoom } from '../hooks/RoomContext';
import type { HostWifiSnapshot } from '../lib/hostWifi';
import { DEFAULT_PARAMS, type PropagationParams, predictDbm, wallLossDb } from '../lib/propagation';
import { freshMeasuredDbm, waveSources } from '../lib/waves';

const CUSTOM = '__custom__';

function fmtDb(v: number): string {
  return `${v.toFixed(0)} dBm`;
}

export function WavesPage() {
  const { room, error: roomError } = useRoom();
  const sources = useMemo(() => waveSources(room), [room]);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [custom, setCustom] = useState<Vec2>({ x: 1, y: 1 });
  const [txPower, setTxPower] = useState(DEFAULT_PARAMS.txPowerDbm);
  const [freq, setFreq] = useState<2.4 | 5>(DEFAULT_PARAMS.freqGhz);
  const [showLaptop, setShowLaptop] = useState(false);
  const [laptopPos, setLaptopPos] = useState<Vec2>({ x: 2, y: 2 });

  const hostState = useApi<HostWifiSnapshot>((sig) => api.hostWifi(10, sig), [], 2000);
  const startHost = useAction(api.hostWifiStart);
  const measured = freshMeasuredDbm(hostState.data);

  // A choice that no longer exists (room edited) falls back to the first source.
  const selectedId =
    sourceId === CUSTOM || sources.some((n) => n.id === sourceId) ? (sourceId as string) : (sources[0]?.id ?? CUSTOM);
  const node = sources.find((n) => n.id === selectedId) ?? null;
  const txX = node ? node.position.x : custom.x;
  const txY = node ? node.position.y : custom.y;
  const tx = useMemo<Vec2>(() => ({ x: txX, y: txY }), [txX, txY]);
  const txHeight = node ? node.position.z : 1.0;
  const txLabel = node ? `${node.label} (${node.role})` : 'your spot (custom)';
  const params = useMemo<PropagationParams>(
    () => ({ txPowerDbm: txPower, freqGhz: freq, pathLossExponent: DEFAULT_PARAMS.pathLossExponent }),
    [txPower, freq],
  );
  const lx = laptopPos.x;
  const ly = laptopPos.y;
  const laptop = useMemo(
    () => (showLaptop && Number.isFinite(lx) && Number.isFinite(ly) ? { position: { x: lx, y: ly }, text: 'your computer (you placed it)' } : null),
    [showLaptop, lx, ly],
  );
  const inputsValid =
    Number.isFinite(tx.x) && Number.isFinite(tx.y) && Number.isFinite(txPower) && txPower >= -10 && txPower <= 36;
  const prediction = room && laptop && inputsValid ? predictDbm(tx, laptop.position, room.walls, room.doors, params) : null;

  return (
    <div className="page">
      <div className="sim-banner" role="note">
        <span className="sim-banner__text">SIMULATED WAVES</span>
        <span className="sim-banner__sub">a model of your room layout, not a measurement</span>
      </div>
      <Notice kind="warn">
        <strong>What this is:</strong> a picture of how Wi-Fi would roughly spread from the source through the walls you
        entered, using a standard textbook model (distance loss plus a loss for every wall crossed). It is computed, not
        measured. Real Wi-Fi also bounces off furniture and walls, which this ignores. <strong>It cannot show people,
        objects or furniture, and no ordinary router or laptop can produce pictures of what is behind a wall.</strong>{' '}
        Videos that claim this either use special research hardware with multiple antennas and cameras for training, or
        show illustrations.
      </Notice>
      <ErrorNotice error={roomError} title="Could not load the room layout" />
      {!room && !roomError && <p className="muted">Loading room layout…</p>}
      {room && (
        <Card
          title="Wi-Fi waves (simulated)"
          subtitle={
            room.provenance === 'EXAMPLE'
              ? 'Using the EXAMPLE room. Enter your own walls, materials and router position on the Calibration page to see your home.'
              : 'Using the room layout you entered on the Calibration page.'
          }
        >
          <div className="form-grid">
            <label className="field">
              Wi-Fi source
              <select value={selectedId} aria-label="Wi-Fi source" onChange={(e) => setSourceId(e.target.value)}>
                {sources.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.label} ({n.role}) at x {n.position.x}, y {n.position.y}
                  </option>
                ))}
                <option value={CUSTOM}>Custom spot (type x, y)</option>
              </select>
            </label>
            {selectedId === CUSTOM && (
              <>
                <label className="field">
                  Source x (m)
                  <NumInput value={custom.x} ariaLabel="source x" onChange={(x) => setCustom((c) => ({ ...c, x }))} />
                </label>
                <label className="field">
                  Source y (m)
                  <NumInput value={custom.y} ariaLabel="source y" onChange={(y) => setCustom((c) => ({ ...c, y }))} />
                </label>
              </>
            )}
            <label className="field">
              Band
              <select value={String(freq)} aria-label="band" onChange={(e) => setFreq(e.target.value === '5' ? 5 : 2.4)}>
                <option value="2.4">2.4 GHz</option>
                <option value="5">5 GHz</option>
              </select>
            </label>
            <label className="field">
              Assumed transmit power (dBm, typical 15–23)
              <NumInput value={txPower} ariaLabel="transmit power" onChange={setTxPower} min={-10} max={36} />
            </label>
          </div>
          {!inputsValid && <Notice kind="error">Enter a valid source position and a transmit power between -10 and 36 dBm.</Notice>}
          {inputsValid && (
            <div className="mt">
              <WaveView room={room} tx={tx} txHeight={txHeight} txLabel={txLabel} params={params} laptop={laptop} />
            </div>
          )}
        </Card>
      )}

      {room && (
        <Card title="What the waves pass through (your layout)" subtitle="Assumed one-wall losses from the material you typed. Real values vary a lot.">
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Wall</th>
                  <th>Material (as entered)</th>
                  <th>Assumed loss per crossing</th>
                </tr>
              </thead>
              <tbody>
                {room.walls.map((w) => (
                  <tr key={w.id}>
                    <td>{w.id}</td>
                    <td>{w.material ?? <Unavailable label="not entered (assumed unknown)" />}</td>
                    <td>{wallLossDb(w.material, freq)} dB</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">An open doorway counts as about 1 dB. Brighter blue = stronger predicted signal.</p>
        </Card>
      )}

      {room && (
        <Card title="Compare with your computer's real signal" subtitle="The only measured number on this page.">
          <label className="check">
            <input type="checkbox" checked={showLaptop} onChange={(e) => setShowLaptop(e.target.checked)} /> Mark where my
            computer is (in the room&apos;s metres)
          </label>
          {showLaptop && (
            <>
              <div className="form-grid mt">
                <label className="field">
                  Computer x (m)
                  <NumInput value={laptopPos.x} ariaLabel="computer x" onChange={(x) => setLaptopPos((p) => ({ ...p, x }))} />
                </label>
                <label className="field">
                  Computer y (m)
                  <NumInput value={laptopPos.y} ariaLabel="computer y" onChange={(y) => setLaptopPos((p) => ({ ...p, y }))} />
                </label>
              </div>
              <ErrorNotice error={hostState.error ?? startHost.error} title="Could not read this computer's Wi-Fi signal" />
              <KeyValue
                items={[
                  ['Model predicts here (SIMULATED)', prediction ? `${fmtDb(prediction.dbm)} (${prediction.wallsCrossed} wall(s) in between)` : <Unavailable />],
                  [
                    'Your computer measures now',
                    measured !== null ? (
                      fmtDb(measured)
                    ) : (
                      <span>
                        <Unavailable label="no fresh reading" />{' '}
                        {hostState.data?.available !== false && !hostState.data?.running && (
                          <button
                            type="button"
                            className="btn btn--primary btn--sm"
                            onClick={() => void startHost.run().then(hostState.reload)}
                            disabled={startHost.busy}
                          >
                            Start reading my signal
                          </button>
                        )}
                      </span>
                    ),
                  ],
                  ['Difference', prediction && measured !== null ? `${(measured - prediction.dbm).toFixed(0)} dB` : <Unavailable />],
                ]}
              />
              <p className="muted">
                Differences of 10–20 dB are normal: the model does not know your router&apos;s real power, its antennas,
                furniture, reflections or exact wall materials. The measurement is only valid if the source above is the
                router your computer is connected to.
              </p>
            </>
          )}
        </Card>
      )}
    </div>
  );
}
