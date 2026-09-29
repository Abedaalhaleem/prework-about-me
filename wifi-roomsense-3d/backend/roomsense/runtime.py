"""Application runtime: owns every long-lived component and wires them together.

:class:`AppRuntime` is the single object the HTTP API, the WebSocket and the
CLI talk to. It owns the SQLite database, the acquisition manager, the
processing engine, the recorder, the zone predictor, the pose gate and the
room geometry.

Threads and data flow
---------------------
* The :class:`~roomsense.acquisition.manager.AcquisitionManager` calls the
  runtime's consumer (:meth:`AppRuntime._consume`) synchronously on the
  source's thread. The consumer only enqueues into a bounded queue
  (``acquisition.frame_queue_size``). For a LIVE source it never waits: if
  the queue is full the *newest* event is dropped and counted per link
  (``HOST_QUEUE_DROPS`` in :attr:`SystemStatus.notes`). For replay and
  simulation, which read from a file or a generator and lose nothing by
  waiting, it waits at most ``NON_LIVE_PUT_TIMEOUT_S`` before dropping and
  counting in the same way.
* ONE processing thread drains the queue: frames go to the engine and, while
  recording, to the recorder; link events are also recorded. It calls
  ``engine.step`` at a sub-hop cadence on the measurement timeline, persists
  every :class:`ActivityResult` to ``activity_log`` in batches (one row per
  result, pruned to a bounded size), watches calibration integrity and runs
  the zone predictor (``DISABLED`` unless a model passed its criteria).
* API threads call the control methods (``start_live`` ... ``shutdown``),
  which are serialised by one lock.

Honesty rules enforced here
---------------------------
* The live path uses only :class:`LiveSerialSource`. There is no code path
  from a live failure to replay or simulation: a lost board shows as
  ``DISCONNECTED`` / ``SENSOR_OFFLINE``.
* Frames that do not belong to the active session and mode never reach the
  engine or the recorder. While LIVE, frames flagged SYNTHETIC or REPLAYED are
  rejected and counted.
* ``simulated`` is true for SIMULATION and for replays of simulated data.
* Calibrations record their source mode, hardware signature, room hash and
  config version. A baseline recorded in SIMULATION is never applied to a
  LIVE session (and the detector refuses such a mix anyway).
* Status never presents results of a previous session.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from . import SCHEMA_VERSION, __version__
from .acquisition.base import EndOfStream, FrameEvent, FrameSource, LinkEvent, SourceEvent
from .acquisition.manager import AcquisitionManager
from .acquisition.replay_source import ReplaySource
from .acquisition.serial_source import LiveSerialSource
from .acquisition.synthetic import (
    SIMULATED_BANNER,
    SyntheticScenario,
    SyntheticSource,
    builtin_scenarios,
    with_seed,
)
from .capabilities import (
    UNSUPPORTED_CAPABILITIES,
    CapabilityContext,
    EvidenceError,
    build_capabilities,
    load_verification_evidence,
)
from .config import REPO_ROOT, AppConfig, ReceiverConfig
from .inference.pose.gate import evaluate_pose_gate, runtime_hardware_profile
from .inference.zone.criteria import CriteriaError, current_criteria_version, default_criteria_path
from .inference.zone.dataset import DatasetError, SessionSpec
from .inference.zone.predictor import ZonePredictor, runtime_inputs
from .inference.zone.registry import RegistryError, ZoneModelRegistry
from .inference.zone.train import TrainingRefused, run_training
from .processing.baseline import Baseline
from .processing.pipeline import ProcessingEngine
from .recording_format import RecordingFormatError
from .room import example_room, load_room_detailed, save_room
from .schemas import (
    SOURCE_MODE_BANNER,
    ActivityResult,
    ActivityState,
    CalibrationKind,
    CalibrationRecord,
    CapabilityStatus,
    CsiFrame,
    GeometryProvenance,
    InputFormat,
    PoseStatus,
    Provenance,
    QualityFlag,
    RoomGeometry,
    SourceMode,
    SourceState,
    SystemStatus,
    UnsupportedCapability,
    WalkTestLinkReport,
    WalkTestReport,
    ZonePrediction,
    ZoneState,
)
from .storage.db import Database, StorageError, activity_entry_from_result
from .storage.exports import ExportError, export_recording
from .storage.models import (
    CONSENT_STATEMENTS,
    ConsentRecord,
    EventKind,
    LabeledEvent,
    RecordingInfo,
    RecordingStatus,
    ValidationRun,
    ValidationRunStatus,
    new_id,
    validate_id,
)
from .storage.recordings import (
    RecordingRefused,
    Recorder,
    delete_recording,
    recording_file_for_read,
)
from .validation.protocol import get_scenario
from .validation.report import build_validation_report

__all__ = [
    "DB_FILENAME",
    "LOCK_FILENAME",
    "NON_LIVE_PUT_TIMEOUT_S",
    "ACTIVITY_LOG_MAX_ROWS",
    "THROUGH_WALL_CRITERIA_PATH",
    "OperationRefused",
    "AppRuntime",
]

log = logging.getLogger(__name__)

DB_FILENAME = "roomsense.sqlite3"
LOCK_FILENAME = ".roomsense.lock"
THROUGH_WALL_CRITERIA_PATH = REPO_ROOT / "configs" / "through_wall_criteria.toml"

# Replay/simulation may wait this long for queue space before an event is
# dropped (and counted). Live sources never wait.
NON_LIVE_PUT_TIMEOUT_S = 1.0
# engine.step runs whenever the clock advanced by hop_s * STEP_FRACTION, so a
# window is computed at most that much later than it became due.
STEP_FRACTION = 0.2
PROCESS_BATCH = 256
IDLE_WAIT_S = 0.05
ACTIVITY_FLUSH_INTERVAL_S = 1.0
ACTIVITY_FLUSH_ROWS = 256
ACTIVITY_PENDING_MAX = 20_000
ACTIVITY_LOG_MAX_ROWS = 1_000_000
ACTIVITY_PRUNE_INTERVAL_S = 600.0
RECORDER_CHECK_INTERVAL_S = 1.0
REPORT_TTL_S = 30.0
POSE_TTL_S = 5.0
ZONE_STATUS_TTL_S = 2.0
CRITERIA_TTL_S = 10.0
HARDWARE_TTL_S = 30.0
THREAD_JOIN_TIMEOUT_S = 5.0
MAX_DROP_KEYS = 64
MAX_EVENTS_RETURNED = 10_000

_OTHER_KEY = "(other)"
_EOS_KEY = "(end of stream)"


class OperationRefused(Exception):
    """A control operation was refused. ``code`` is machine readable,
    ``http_status`` is what the API returns."""

    def __init__(self, code: str, detail: str, http_status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.http_status = http_status


_RECORDING_REFUSED_STATUS = {
    "ALREADY_RECORDING": 409,
    "CONSENT_INVALID": 422,
    "INVALID_ARGUMENT": 422,
    "QUOTA_EXCEEDED": 409,
    "RECORDING_ACTIVE": 409,
}


def _refused_from_recording(exc: RecordingRefused) -> OperationRefused:
    return OperationRefused(exc.code, exc.detail, _RECORDING_REFUSED_STATUS.get(exc.code, 409))


# ---------------------------------------------------------------------------
# Data-directory lock: one runtime (hence one Recorder) per data directory
# ---------------------------------------------------------------------------


class _DataDirLock:
    """Exclusive, non-blocking lock on ``<data_dir>/.roomsense.lock``.

    The recorder marks recordings left in RECORDING by another process as
    interrupted when it starts, so two runtimes on one data directory would
    corrupt each other's bookkeeping. The OS releases the lock if the process
    dies.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / LOCK_FILENAME
        self._fh: Any = open(self.path, "a+b")  # noqa: SIM115 - held for the runtime's lifetime
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows only
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise OperationRefused(
                "DATA_DIR_LOCKED",
                f"another RoomSense process is using {data_dir}; stop it first (scripts/stop.sh)",
                409,
            ) from exc

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


# ---------------------------------------------------------------------------
# Small cache helper
# ---------------------------------------------------------------------------


@dataclass
class _Cached:
    value: Any = None
    at_ns: int = 0
    key: Any = None
    valid: bool = False

    def get(self, key: Any, ttl_s: float) -> tuple[bool, Any]:
        if self.valid and self.key == key and (time.monotonic_ns() - self.at_ns) / 1e9 <= ttl_s:
            return True, self.value
        return False, None

    def put(self, key: Any, value: Any) -> Any:
        self.value, self.key, self.at_ns, self.valid = value, key, time.monotonic_ns(), True
        return value

    def clear(self) -> None:
        self.valid = False


@dataclass
class _WalkTest:
    session_id: str
    source_mode: SourceMode
    started_at_unix_ns: int


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


class AppRuntime:
    """See the module docstring. All public methods are thread-safe."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        serial_factory: Callable[..., Any] | None = None,
        clock: Callable[[], int] | None = None,
        background_processing: bool = True,
    ) -> None:
        self.cfg = cfg
        self._serial_factory = serial_factory
        self._clock: Callable[[], int] = clock if clock is not None else time.monotonic_ns
        self._unix_clock: Callable[[], int] = time.time_ns
        self._background = background_processing
        self.started_at_unix_ns = self._unix_clock()
        self._started_mono = time.monotonic()
        self.config_version = cfg.config_version()

        self.data_dir = cfg.storage.resolved_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._dir_lock = _DataDirLock(self.data_dir)
        try:
            self.db = Database(self.data_dir / DB_FILENAME)
        except BaseException:
            self._dir_lock.release()
            raise

        # Locks. Order when nested: _control_lock -> _proc_lock -> _state_lock.
        # _q_lock and _stats_lock are leaves (nothing is acquired under them).
        self._control_lock = threading.RLock()
        self._proc_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._q_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._report_lock = threading.Lock()
        self._train_lock = threading.Lock()
        self._hw_lock = threading.Lock()

        try:
            self.recorder = Recorder(self.db, self.data_dir, cfg.storage, on_auto_stop=self._on_recording_auto_stop)
            self.engine = ProcessingEngine(cfg, clock_ns=self._clock, clock_unix_ns=self._unix_clock)
            self.manager = AcquisitionManager(cfg)
            self.manager.add_consumer(self._consume)
            loaded = load_room_detailed(cfg)
            self._room: RoomGeometry = loaded.room
            self._room_error: str | None = loaded.error
            self.registry = ZoneModelRegistry(self.data_dir, self.db)
            self._zone_criteria_path = default_criteria_path(cfg)
            self.predictor = ZonePredictor(self.registry, cfg, room=self._room,
                                           criteria_path=self._zone_criteria_path)
            self._pose_manifest_path = cfg.resolve_path(cfg.pose.manifest_file)
        except BaseException:
            self.db.close()
            self._dir_lock.release()
            raise

        # Event queue and its accounting.
        self._q: queue.Queue[tuple[int, SourceEvent]] = queue.Queue(maxsize=cfg.acquisition.frame_queue_size)
        self._gen = 0
        self._accepting = False
        self._q_live = False
        self._queue_drops: Counter[str] = Counter()
        self._queue_drops_total = 0
        self._discarded_on_switch = 0

        # Per-session state (reset on every source switch).
        self._session_id: str | None = None
        self._mode: SourceMode | None = None
        self._source_info: dict[str, Any] = {}
        self._data_clock_ns = 0
        self._first_data_ns: int | None = None
        self._last_step_ns: int | None = None
        self._session_live_valid_frames = 0
        self._mode_guard_rejects = 0
        self._active_calibration: CalibrationRecord | None = None
        self._calibration_note: str | None = None
        self._calibration_started_unix_ns: int | None = None
        self._walk: _WalkTest | None = None
        self._last_walk_report: WalkTestReport | None = None
        self._zone_pred: tuple[str | None, int, ZonePrediction] | None = None
        self._end_of_stream: str | None = None

        # Runtime-lifetime state.
        self._live_valid_frames_total = 0
        self._pending_activity: list[ActivityResult] = []
        self._activity_dropped = 0
        self._activity_errors = 0
        self._last_flush_mono = time.monotonic()
        self._last_prune_mono = 0.0
        self._last_recorder_check = time.monotonic()
        self._processing_errors = 0
        self._last_processing_error: str | None = None
        self._recording_note: str | None = None
        self._room_note: str | None = None

        self._report_cache = _Cached()
        self._pose_cache = _Cached()
        self._zone_status_cache = _Cached()
        self._criteria_cache = _Cached()
        self._hw_cache = _Cached()
        self._evidence: dict[str, Any] = {}
        self._evidence_mtime: float | None = None
        self._evidence_error: str | None = None
        self._reload_evidence()

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._closing = False
        self._closed = False
        hop_ns = int(round(cfg.processing.hop_s * 1e9))
        self._step_interval_ns = max(1, int(hop_ns * STEP_FRACTION))

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the processing thread (idempotent)."""
        with self._control_lock:
            self._ensure_open()
            if self._started:
                return
            self._started = True
            try:
                self.db.prune_activity_log(ACTIVITY_LOG_MAX_ROWS)
            except StorageError:
                log.exception("could not prune the activity log")
            self._last_prune_mono = time.monotonic()
            if self._background:
                self._thread = threading.Thread(target=self._run, name="roomsense-processing", daemon=True)
                self._thread.start()

    def __enter__(self) -> "AppRuntime":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise OperationRefused("SHUTTING_DOWN", "the runtime is shutting down", 503)

    def shutdown(self) -> None:
        """Safe, idempotent shutdown: stop the source (closes serial ports),
        finalise any recording, stop the processing thread, close the DB."""
        with self._control_lock:
            if self._closed:
                return
            self._closing = True
            with self._q_lock:
                self._accepting = False
                self._gen += 1
            try:
                self.manager.stop()
            except Exception:
                log.exception("error stopping the source during shutdown")
            try:
                self.recorder.stop("application shutdown")
            except Exception:
                log.exception("error finalising the recording during shutdown")
            self._stop_evt.set()
            t = self._thread
            if t is not None and t is not threading.current_thread():
                t.join(THREAD_JOIN_TIMEOUT_S)
                if t.is_alive():
                    log.warning("processing thread did not stop within %.1f s", THREAD_JOIN_TIMEOUT_S)
            with self._proc_lock:
                self._drain_queue()
                self._flush_activity(force=True)
                self._end_session_locked("application shutdown")
                self._closed = True
                try:
                    self.db.close()
                except Exception:
                    log.exception("error closing the database")
            self._dir_lock.release()
            log.info("runtime shut down")

    # ------------------------------------------------------------------ consumer
    def _consume(self, ev: SourceEvent) -> None:
        """AcquisitionManager consumer. Runs on the SOURCE thread: enqueue only."""
        with self._q_lock:
            accepting, gen, live = self._accepting, self._gen, self._q_live
        if not accepting:
            with self._stats_lock:
                self._discarded_on_switch += 1
            return
        item = (gen, ev)
        if live:
            # A live reader must never wait: the OS serial buffer would overflow
            # and the loss would be invisible. Drop the newest and count it.
            try:
                self._q.put_nowait(item)
            except queue.Full:
                self._count_drop(ev)
            return
        deadline = time.monotonic() + NON_LIVE_PUT_TIMEOUT_S
        while True:
            try:
                self._q.put(item, timeout=0.05)
                return
            except queue.Full:
                if not self._accepting or gen != self._gen or self._closing:
                    # The source is being switched or stopped; not a processing backlog.
                    with self._stats_lock:
                        self._discarded_on_switch += 1
                    return
                if time.monotonic() >= deadline:
                    self._count_drop(ev)
                    return

    def _count_drop(self, ev: SourceEvent) -> None:
        if isinstance(ev, FrameEvent):
            key = ev.frame.link_id
        elif isinstance(ev, LinkEvent):
            key = ev.link_id or _OTHER_KEY
        else:
            key = _EOS_KEY
        with self._stats_lock:
            if key not in self._queue_drops and len(self._queue_drops) >= MAX_DROP_KEYS:
                key = _OTHER_KEY
            self._queue_drops[key] += 1
            self._queue_drops_total += 1
            n = self._queue_drops_total
        if n == 1 or n % 1000 == 0:
            log.warning("host event queue full; dropped the newest event", extra={"link_id": key, "dropped_total": n})

    def queue_drops(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._queue_drops)

    # ------------------------------------------------------------------ processing
    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._process_batch(PROCESS_BATCH, IDLE_WAIT_S)
            except Exception as exc:  # the processing thread must never die silently
                self._processing_errors += 1
                self._last_processing_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if self._processing_errors == 1 or self._processing_errors % 100 == 0:
                    log.exception("processing iteration failed (%d times)", self._processing_errors)
                time.sleep(0.01)

    def pump(self, max_events: int | None = None) -> int:
        """Process queued events now, in the calling thread.

        For runtimes created with ``background_processing=False`` (tests and
        offline tools). Returns the number of events taken off the queue.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("pump() cannot be used while the background processing thread runs")
        return self._process_batch(max_events if max_events is not None else 10 * PROCESS_BATCH, 0.0)

    def _process_batch(self, max_events: int, block_s: float) -> int:
        items: list[tuple[int, SourceEvent]] = []
        try:
            if block_s > 0:
                items.append(self._q.get(timeout=block_s))
            while len(items) < max_events:
                items.append(self._q.get_nowait())
        except queue.Empty:
            pass
        with self._proc_lock:
            if self._closed or self._closing:
                return len(items)
            for gen, ev in items:
                if gen != self._gen:
                    with self._stats_lock:
                        self._discarded_on_switch += 1
                    continue
                self._handle_event(ev)
            if self._session_id is not None:
                self._maybe_step()
            self._periodic()
        return len(items)

    def _now_ns(self) -> int:
        """Processing time: the host clock, or the newest data timestamp when a
        non-realtime simulation runs ahead of it. Data timestamps are host
        arrival times stamped when the data arrived, so for live, replay and
        real-time simulation this is simply the host clock."""
        return max(self._clock(), self._data_clock_ns)

    @staticmethod
    def _event_time(ev: SourceEvent) -> int | None:
        if isinstance(ev, FrameEvent):
            return ev.frame.host_arrival_monotonic_ns
        if isinstance(ev, LinkEvent):
            return ev.host_monotonic_ns
        return None

    def _frame_allowed(self, frame: CsiFrame) -> bool:
        mode = self._mode
        if mode is None or frame.session_id != self._session_id or frame.source_mode != mode:
            return False
        if mode == SourceMode.LIVE:
            flags = frame.quality_flags
            if (QualityFlag.SYNTHETIC.value in flags or QualityFlag.REPLAYED.value in flags
                    or frame.input_format == InputFormat.SYNTHETIC_V1
                    or frame.device.identity_source == "synthetic"):
                return False
        return True

    def _handle_event(self, ev: SourceEvent) -> None:
        ts = self._event_time(ev)
        if ts is not None and ts > self._data_clock_ns:
            self._data_clock_ns = ts
            if self._first_data_ns is None:
                self._first_data_ns = ts
        # Let time pass up to this event first, so gaps (e.g. a disconnect)
        # are stepped through before the event changes the link's state.
        self._maybe_step()
        if isinstance(ev, FrameEvent):
            frame = ev.frame
            if not self._frame_allowed(frame):
                self._mode_guard_rejects += 1
                return
            if frame.source_mode == SourceMode.LIVE and frame.layout_id is not None:
                self._session_live_valid_frames += 1
                self._live_valid_frames_total += 1
            self.engine.on_event(ev)
            self.recorder.write(frame)
        elif isinstance(ev, LinkEvent):
            self.engine.on_event(ev)
            try:
                self.recorder.write_event(ev)
            except (TypeError, ValueError):
                log.debug("link event not recordable", exc_info=True)
        elif isinstance(ev, EndOfStream):
            self.engine.on_event(ev)
            self._end_of_stream = ev.reason
            # Report the end at once, even if the data timeline ran ahead of the clock.
            self._last_step_ns = None
            self._maybe_step()

    def _maybe_step(self) -> None:
        if self._session_id is None:
            return
        now = self._now_ns()
        if self._last_step_ns is not None and now - self._last_step_ns < self._step_interval_ns:
            return
        self._last_step_ns = now
        results = self.engine.step(now)
        if results:
            self._pending_activity.extend(results)
            if len(self._pending_activity) > ACTIVITY_PENDING_MAX:
                # Only reachable if the database keeps failing; counted and reported.
                excess = len(self._pending_activity) - ACTIVITY_PENDING_MAX
                del self._pending_activity[:excess]
                self._activity_dropped += excess
            self._update_zone(now)
        self._check_calibration_integrity()

    def _periodic(self) -> None:
        self._flush_activity()
        mono = time.monotonic()
        if mono - self._last_recorder_check >= RECORDER_CHECK_INTERVAL_S:
            self._last_recorder_check = mono
            try:
                self.recorder.check_limits()
            except Exception:
                log.exception("recorder limit check failed")
        if mono - self._last_prune_mono >= ACTIVITY_PRUNE_INTERVAL_S:
            self._last_prune_mono = mono
            try:
                self.db.prune_activity_log(ACTIVITY_LOG_MAX_ROWS)
            except StorageError:
                log.exception("could not prune the activity log")

    def _flush_activity(self, force: bool = False) -> None:
        if not self._pending_activity:
            return
        mono = time.monotonic()
        if not force and len(self._pending_activity) < ACTIVITY_FLUSH_ROWS and (
                mono - self._last_flush_mono < ACTIVITY_FLUSH_INTERVAL_S):
            return
        batch = self._pending_activity
        self._pending_activity = []
        self._last_flush_mono = mono
        try:
            self.db.add_activity_many([activity_entry_from_result(r) for r in batch])
        except (StorageError, ValidationError, ValueError) as exc:
            self._activity_errors += 1
            self._activity_dropped += len(batch)
            if self._activity_errors == 1 or self._activity_errors % 100 == 0:
                log.warning("could not write %d activity row(s): %s", len(batch), type(exc).__name__)
        except Exception:
            self._activity_errors += 1
            self._activity_dropped += len(batch)
            log.exception("unexpected error writing the activity log")

    def _drain_queue(self) -> int:
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                break
        if n:
            with self._stats_lock:
                self._discarded_on_switch += n
        return n

    # ------------------------------------------------------------------ zone
    def _criteria_version(self) -> str | None:
        hit, val = self._criteria_cache.get("v", CRITERIA_TTL_S)
        if hit:
            return val
        return self._criteria_cache.put("v", current_criteria_version(self._zone_criteria_path))

    def _combined_provenance(self, latest: Mapping[str, ActivityResult]) -> Provenance | None:
        session, mode = self._session_id, self._mode
        results = [r for r in latest.values() if r.provenance.session_id == session]
        if session is None or mode is None or not results:
            return None
        starts = [r.provenance.window_start_unix_ns for r in results if r.provenance.window_start_unix_ns is not None]
        ends = [r.provenance.window_end_unix_ns for r in results if r.provenance.window_end_unix_ns is not None]
        ages = [r.provenance.measurement_age_s for r in results]
        cal = self._active_calibration
        return Provenance(
            source_mode=mode,
            session_id=session,
            link_ids=sorted(r.link_id for r in results),
            window_start_unix_ns=min(starts) if starts else None,
            window_end_unix_ns=max(ends) if ends else None,
            window_frame_count=sum(r.provenance.window_frame_count for r in results),
            config_version=self.config_version,
            calibration_id=None if cal is None else cal.calibration_id,
            computed_at_unix_ns=self._unix_clock(),
            measurement_age_s=None if any(a is None for a in ages) else max(ages),  # type: ignore[type-var]
        )

    def _update_zone(self, now_ns: int) -> None:
        session = self._session_id
        try:
            latest = self.engine.latest()
            prov = self._combined_provenance(latest)
            feats, states, quality = runtime_inputs(self.engine)
            pred = self.predictor.predict(
                feats, states, self._room.config_hash(), self.engine.hardware_signature(), self.config_version,
                self._criteria_version(), prov, room=self._room, link_quality=quality, now_ns=now_ns,
            )
        except Exception as exc:  # a zone failure must never stop motion processing
            log.exception("zone predictor failed")
            pred = ZonePrediction(state=ZoneState.DISABLED,
                                  reasons=[f"ZONE_PREDICTOR_ERROR: {type(exc).__name__}"])
        with self._state_lock:
            self._zone_pred = (session, time.monotonic_ns(), pred)

    def zone_status(self) -> dict[str, Any]:
        """``{state, reasons, model_id, criteria_version, criteria, report}``."""
        key = (self._room.config_hash(), self.engine.hardware_signature(), self.config_version)
        hit, val = self._zone_status_cache.get(key, ZONE_STATUS_TTL_S)
        if hit:
            return val
        try:
            st = self.predictor.status(room_hash=key[0], hardware_signature=key[1], config_version=key[2])
        except Exception as exc:
            log.exception("zone status failed")
            st = {"state": ZoneState.DISABLED.value, "reasons": [f"ZONE_STATUS_ERROR: {type(exc).__name__}"],
                  "model_id": None, "criteria_version": None, "criteria": None, "report": None}
        return self._zone_status_cache.put(key, st)

    def _current_zone(self, zone_st: Mapping[str, Any]) -> ZonePrediction:
        session = self._session_id
        with self._state_lock:
            stored = self._zone_pred
        base_reasons = [str(r) for r in zone_st.get("reasons", [])]
        if session is None:
            return ZonePrediction(state=ZoneState.DISABLED,
                                  reasons=["NO_SOURCE: no source selected", *base_reasons],
                                  criteria_version=zone_st.get("criteria_version"))
        if stored is None or stored[0] != session:
            if zone_st.get("state") != "ENABLED":
                return ZonePrediction(state=ZoneState.DISABLED, reasons=base_reasons,
                                      criteria_version=zone_st.get("criteria_version"))
            return ZonePrediction(state=ZoneState.ABSTAIN, model_id=zone_st.get("model_id"),
                                  reasons=["NOT_COMPUTED_YET: no processed window in this session yet"],
                                  criteria_version=zone_st.get("criteria_version"))
        _, at_ns, pred = stored
        age_s = (time.monotonic_ns() - at_ns) / 1e9
        if pred.state == ZoneState.ESTIMATE and age_s > self.cfg.acquisition.stale_after_s:
            # Never present an old estimate as current.
            return ZonePrediction(state=ZoneState.ABSTAIN, model_id=pred.model_id,
                                  criteria_version=pred.criteria_version, provenance=pred.provenance,
                                  reasons=[f"ZONE_STALE: newest zone decision is {age_s:.1f} s old"])
        return pred

    def train_zone_model(self, sessions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Train and evaluate a zone model on recorded, labelled sessions.

        CPU bound (call it from a worker thread). The model is saved even if it
        fails its criteria, but it is only *enabled* if every criterion passes
        on held-out sessions with no synthetic data.
        """
        self._ensure_open()
        if not self._train_lock.acquire(blocking=False):
            raise OperationRefused("TRAINING_IN_PROGRESS", "a zone model is already being trained", 409)
        try:
            try:
                specs = [SessionSpec(label=str(s["label"]), recording_id=str(s["recording_id"]),
                                     split=s.get("split")) for s in sessions]
            except (KeyError, TypeError, DatasetError) as exc:
                raise OperationRefused("INVALID_SESSIONS", str(exc)[:500], 422) from exc
            try:
                trained, binding = run_training(specs, self.cfg, self._room, data_dir=self.data_dir, db=self.db,
                                                criteria_path=self._zone_criteria_path, registry=self.registry)
            except TrainingRefused as exc:
                raise OperationRefused(exc.code, exc.detail, 409) from exc
            except (DatasetError, CriteriaError) as exc:
                raise OperationRefused(type(exc).__name__.upper(), str(exc)[:500], 422) from exc
            except RegistryError as exc:
                raise OperationRefused(exc.code, exc.detail, 500) from exc
            self.predictor.refresh()
            self._zone_status_cache.clear()
            return {
                "model_id": None if binding is None else binding.model_id,
                "enabled": bool(trained.enabled),
                "synthetic_data_used": bool(trained.synthetic_data_used),
                "binding": None if binding is None else binding.to_json_dict(),
                "report": trained.report,
            }
        finally:
            self._train_lock.release()

    # ------------------------------------------------------------------ source control
    def _activate(self, mode: SourceMode, prefix: str, build: Callable[[str], FrameSource],
                  info: dict[str, Any]) -> None:
        with self._control_lock:
            self._ensure_open()
            session_id = new_id(prefix)
            try:
                source = build(session_id)
            except OperationRefused:
                raise
            except (ValueError, RecordingFormatError, OSError) as exc:
                raise OperationRefused("INVALID_SOURCE", f"{type(exc).__name__}: {str(exc)[:300]}", 422) from exc
            self._stop_current_locked(f"switching to {mode.value}")
            with self._proc_lock:
                self._drain_queue()
                self.engine.reset(session_id, mode)
                with self._state_lock:
                    self._session_id = session_id
                    self._mode = mode
                    self._source_info = dict(info)
                    self._reset_session_state_locked()
                try:
                    self.db.ensure_session(session_id, mode)
                except (StorageError, ValueError):
                    log.exception("could not record session %s", session_id)
            with self._q_lock:
                self._gen += 1
                self._accepting = True
                self._q_live = mode == SourceMode.LIVE
            try:
                self.manager.start(source)
            except Exception as exc:
                raise OperationRefused("SOURCE_START_FAILED", f"{type(exc).__name__}: {str(exc)[:300]}", 500) from exc
            log.info("source started", extra={"mode": mode.value, "session_id": session_id})

    def _reset_session_state_locked(self) -> None:
        self._data_clock_ns = 0
        self._first_data_ns = None
        self._last_step_ns = None
        self._session_live_valid_frames = 0
        self._mode_guard_rejects = 0
        self._active_calibration = None
        self._calibration_note = None
        self._calibration_started_unix_ns = None
        self._walk = None
        self._zone_pred = None
        self._end_of_stream = None

    def _stop_current_locked(self, reason: str) -> None:
        """Stop source, then recording; discard queued events; close the session."""
        with self._q_lock:
            self._accepting = False
            self._gen += 1
        self.manager.stop()
        try:
            self.recorder.stop(f"source stopped ({reason})")
        except Exception:
            log.exception("error stopping the recording")
        with self._proc_lock:
            self._drain_queue()
            self._flush_activity(force=True)
            self._end_session_locked(reason)
            with self._state_lock:
                self._session_id = None
                self._mode = None
                self._source_info = {}
                self._reset_session_state_locked()
            self.engine.cancel_baseline()

    def _end_session_locked(self, reason: str) -> None:
        sid = self._session_id
        if sid is None or self.db.closed:
            return
        try:
            for run in self.db.list_validation_runs(session_id=sid, limit=None):
                if run.status == ValidationRunStatus.RUNNING:
                    # A run whose session ended without an explicit stop is incomplete.
                    self.db.end_validation_run(run.run_id, status=ValidationRunStatus.ABORTED)
                    self._report_cache.clear()
            self.db.end_session(sid)
        except (StorageError, ValueError):
            log.exception("could not close session %s (%s)", sid, reason)

    def start_live(self, receivers: Sequence[ReceiverConfig] | None = None) -> SystemStatus:
        """Start the LIVE source. There is no fallback to any other source."""
        rx = list(receivers) if receivers else list(self.cfg.acquisition.receivers)
        if not rx:
            raise OperationRefused(
                "NO_RECEIVERS_CONFIGURED",
                "no receivers configured: add [[acquisition.receivers]] to configs/roomsense.toml or pass receivers",
                409,
            )
        ids = [r.receiver_id for r in rx]
        if len(set(ids)) != len(ids):
            raise OperationRefused("INVALID_RECEIVERS", "receiver_id values must be unique", 422)
        acq, storage, factory = self.cfg.acquisition, self.cfg.storage, self._serial_factory

        def build(session_id: str) -> FrameSource:
            return LiveSerialSource(rx, acq, storage, session_id, serial_factory=factory)

        info = {"receivers": [{"receiver_id": r.receiver_id, "port": r.port, "link_id": r.link_id,
                               "input_format": r.input_format.value} for r in rx]}
        self._activate(SourceMode.LIVE, "live", build, info)
        return self.build_status()

    def start_replay(self, recording_id: str, speed: float = 1.0) -> SystemStatus:
        try:
            validate_id(recording_id, "recording_id")
        except ValueError as exc:
            raise OperationRefused("INVALID_ARGUMENT", str(exc), 422) from exc
        if not (0 < float(speed) <= 1000):
            raise OperationRefused("INVALID_ARGUMENT", "speed must be in (0, 1000]", 422)
        info = self.db.get_recording(recording_id)
        if info is not None and info.status == RecordingStatus.RECORDING:
            raise OperationRefused("RECORDING_ACTIVE", "stop that recording before replaying it", 409)
        try:
            path = recording_file_for_read(self.data_dir, recording_id)
        except FileNotFoundError as exc:
            raise OperationRefused("RECORDING_NOT_FOUND", f"recording {recording_id} not found", 404) from exc
        except ValueError as exc:
            raise OperationRefused("INVALID_ARGUMENT", str(exc), 422) from exc

        def build(session_id: str) -> FrameSource:
            return ReplaySource(path, session_id=session_id, speed=float(speed))

        self._activate(SourceMode.REPLAY, "replay", build, {"recording_id": recording_id, "speed": float(speed)})
        return self.build_status()

    def start_simulation(
        self,
        scenario: str | SyntheticScenario,
        seed: int | None = None,
        acknowledge_simulated: bool = False,
        *,
        speed: float = 1.0,
        realtime: bool = True,
    ) -> SystemStatus:
        """Start a SIMULATION source. Refused unless ``acknowledge_simulated`` is True.

        ``realtime=False`` generates as fast as processing allows, with data
        timestamps on the simulated timeline (tests and offline checks).
        """
        if acknowledge_simulated is not True:
            raise OperationRefused(
                "SIMULATION_NOT_ACKNOWLEDGED",
                "simulation produces synthetic data, not measurements; set acknowledge_simulated to true",
                422,
            )
        if isinstance(scenario, SyntheticScenario):
            scn = scenario
        else:
            scn_map = builtin_scenarios()
            if scenario not in scn_map:
                raise OperationRefused("UNKNOWN_SCENARIO", f"unknown scenario {scenario!r}; "
                                       f"available: {sorted(scn_map)}", 404)
            scn = scn_map[scenario]
        if seed is not None:
            scn = with_seed(scn, int(seed))
        if not (0 < float(speed) <= 1000):
            raise OperationRefused("INVALID_ARGUMENT", "speed must be in (0, 1000]", 422)

        def build(session_id: str) -> FrameSource:
            return SyntheticSource(scn, session_id=session_id, realtime=realtime, speed=float(speed))

        self._activate(SourceMode.SIMULATION, "sim", build,
                       {"scenario": scn.name, "seed": scn.seed, "speed": float(speed), "realtime": realtime})
        return self.build_status()

    def stop_source(self) -> SystemStatus:
        with self._control_lock:
            self._ensure_open()
            self._stop_current_locked("stopped by operator")
        return self.build_status()

    # ------------------------------------------------------------------ properties
    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def mode(self) -> SourceMode | None:
        return self._mode

    def data_elapsed_s(self) -> float | None:
        """Seconds of data processed in this session on the data timeline
        (newest data timestamp minus the first one); None before any data."""
        first = self._first_data_ns
        return None if first is None else (self._data_clock_ns - first) / 1e9

    # ------------------------------------------------------------------ calibration
    def _require_running(self) -> tuple[str, SourceMode]:
        sid, mode = self._session_id, self._mode
        if sid is None or mode is None:
            raise OperationRefused("NO_SOURCE", "start a source first", 409)
        return sid, mode

    def start_baseline(self, link_ids: Sequence[str] | None = None, *, confirm_room_empty: bool = False) -> dict[str, Any]:
        if confirm_room_empty is not True:
            raise OperationRefused(
                "ROOM_NOT_CONFIRMED_EMPTY",
                "confirm that the target room is empty and nothing moves near the sensors (confirm_room_empty: true)",
                422,
            )
        with self._control_lock:
            self._ensure_open()
            _, mode = self._require_running()
            state = self.manager.source_state()
            if state != SourceState.RUNNING:
                raise OperationRefused("SOURCE_NOT_RUNNING", f"source is {state.value}; no data is flowing", 409)
            try:
                self.engine.start_baseline(list(link_ids) if link_ids else None)
            except ValueError as exc:
                raise OperationRefused("CALIBRATION_REFUSED", str(exc), 409) from exc
            with self._state_lock:
                self._calibration_started_unix_ns = self._unix_clock()
                self._calibration_note = None
            return {"started": True, "source_mode": mode.value, "progress": self.engine.calibration_progress()}

    def stop_baseline(self) -> CalibrationRecord:
        with self._control_lock:
            self._ensure_open()
            sid, mode = self._require_running()
            if not self.engine.is_calibrating():
                raise OperationRefused("NOT_CALIBRATING", "no quiet-baseline recording is running", 409)
            progress = self.engine.calibration_progress()
            simulated = self._is_simulated()
            results = self.engine.stop_baseline()
            baselines: dict[str, Baseline] = {lid: b for lid, (b, _) in results.items() if b is not None}
            cal_ids = {b.calibration_id for b in baselines.values()}
            cal_id = next(iter(cal_ids)) if len(cal_ids) == 1 else new_id("cal")
            valid = bool(results) and len(baselines) == len(results)
            per_link: dict[str, Any] = {}
            for lid, (b, reasons) in results.items():
                prog = progress.get(lid, {})
                per_link[lid] = {
                    "accepted": b is not None,
                    "reasons": list(reasons),
                    "windows_used": prog.get("windows_used") if b is None else b.n_windows,
                    "duration_s": prog.get("covered_s") if b is None else b.duration_s,
                    "frames_accepted": prog.get("frames_accepted"),
                    "windows_skipped": prog.get("windows_skipped"),
                    "baseline_summary": None if b is None else b.summary,
                }
            rejected = [f"{lid}: {'; '.join(v['reasons']) or 'rejected'}" for lid, v in per_link.items()
                        if not v["accepted"]]
            record = CalibrationRecord(
                calibration_id=cal_id,
                kind=CalibrationKind.QUIET_BASELINE,
                created_at_unix_ns=self._unix_clock(),
                session_id=sid,
                source_mode=mode,
                link_ids=sorted(results),
                hardware_signature=self.engine.hardware_signature() or "unavailable",
                room_config_hash=self._room.config_hash(),
                processing_config_version=self.config_version,
                duration_s=float(max((v["duration_s"] or 0.0) for v in per_link.values()) if per_link else 0.0),
                frame_count=int(sum(int(v["frames_accepted"] or 0) for v in per_link.values())),
                window_count=int(sum(int(v["windows_used"] or 0) for v in per_link.values())),
                valid=valid,
                invalidated_reason=None if valid else ("BASELINE_REJECTED: " + " | ".join(rejected))[:500],
                summary={
                    "per_link": per_link,
                    "simulated_data": simulated,
                    "room_provenance": GeometryProvenance(self._room.provenance).value,
                    "started_at_unix_ns": self._calibration_started_unix_ns,
                    "note": ("Recorded on SIMULATED data: valid only for simulated sessions; never applied to "
                             "measured data.") if simulated else "Quiet baseline recorded by the operator.",
                },
            )
            try:
                self.db.add_calibration(record, {lid: b.to_dict() for lid, b in baselines.items()})
            except (StorageError, ValueError):
                log.exception("could not store calibration %s", cal_id)
            if valid and self._calibration_applicable(record, mode):
                self.engine.set_baselines(baselines, cal_id)
                with self._state_lock:
                    self._active_calibration = record
                    self._calibration_note = None
            else:
                with self._state_lock:
                    self._calibration_note = record.invalidated_reason or "calibration not applied"
            return record

    @staticmethod
    def _calibration_applicable(record: CalibrationRecord, mode: SourceMode) -> bool:
        """Only a calibration recorded in the same source mode applies, and one
        made on simulated data (SIMULATION, or a replay of simulated data) is
        never applied to a LIVE session. The detector refuses such a mix too."""
        rec_sim = record.source_mode == SourceMode.SIMULATION or bool(record.summary.get("simulated_data"))
        if mode == SourceMode.LIVE and rec_sim:
            return False
        return record.source_mode == mode

    def cancel_baseline(self) -> dict[str, Any]:
        with self._control_lock:
            self._ensure_open()
            was = self.engine.is_calibrating()
            self.engine.cancel_baseline()
            return {"cancelled": was}

    def invalidate_calibration(self, reason: str) -> dict[str, Any]:
        reason = (reason or "").strip()
        if not reason:
            raise OperationRefused("INVALID_ARGUMENT", "a reason is required", 422)
        text = f"INVALIDATED_BY_OPERATOR: {reason[:400]}"
        with self._control_lock:
            self._ensure_open()
            cal = self._active_calibration
            self.engine.invalidate_baselines(text)
            invalidated: list[str] = []
            if cal is not None and self.db.invalidate_calibration(cal.calibration_id, text):
                invalidated.append(cal.calibration_id)
            with self._state_lock:
                self._active_calibration = None
                self._calibration_note = text
            return {"invalidated": invalidated, "reason": text}

    def _check_calibration_integrity(self) -> None:
        """Notice baselines the engine dropped (hardware/layout change) and
        record the invalidation. The engine never re-applies them."""
        cal = self._active_calibration
        if cal is None:
            return
        status = self.engine.baseline_status()
        lost = [(lid, status[lid]) for lid in cal.link_ids if lid in status and not status[lid]["has_baseline"]]
        if not lost:
            return
        reason = next((str(s["invalid_reason"]) for _, s in lost if s.get("invalid_reason")),
                      f"BASELINE_DROPPED: baseline no longer applied on {lost[0][0]}")
        if reason.startswith(("HARDWARE_SIGNATURE_CHANGED", "LAYOUT_CHANGED")):
            try:
                self.db.invalidate_calibration(cal.calibration_id, reason[:500])
            except (StorageError, ValueError):
                log.exception("could not invalidate calibration %s", cal.calibration_id)
        with self._state_lock:
            self._active_calibration = None
            self._calibration_note = reason
        log.warning("calibration no longer valid", extra={"calibration_id": cal.calibration_id, "reason": reason})

    def calibration_overview(self) -> dict[str, Any]:
        try:
            history = [s.record for s in self.db.list_calibrations(limit=50)]
        except StorageError:
            history = []
        return {
            "active": self._active_calibration,
            "history": history,
            "in_progress": self.engine.calibration_progress() if self.engine.is_calibrating() else None,
            "walk_test_active": self._walk is not None,
            "last_walk_test": self._last_walk_report,
            "note": self._calibration_note,
        }

    def start_walk_test(self, link_ids: Sequence[str] | None = None) -> dict[str, Any]:
        with self._control_lock:
            self._ensure_open()
            sid, mode = self._require_running()
            self.engine.start_walk_test(list(link_ids) if link_ids else None)
            with self._state_lock:
                self._walk = _WalkTest(sid, mode, self._unix_clock())
            return {"started": True, "note": WalkTestReport.model_fields["note"].default}

    def stop_walk_test(self) -> WalkTestReport:
        with self._control_lock:
            self._ensure_open()
            walk = self._walk
            if walk is None:
                raise OperationRefused("NO_WALK_TEST", "no walk test is running", 409)
            raw = self.engine.stop_walk_test()
            links = [WalkTestLinkReport(link_id=lid, **vals) for lid, vals in sorted(raw.items())]
            report = WalkTestReport(session_id=walk.session_id, source_mode=walk.source_mode,
                                    started_at_unix_ns=walk.started_at_unix_ns,
                                    ended_at_unix_ns=self._unix_clock(), links=links)
            with self._state_lock:
                self._walk = None
                self._last_walk_report = report
            return report

    # ------------------------------------------------------------------ recordings
    @staticmethod
    def _consent_from(consent: ConsentRecord | Mapping[str, Any]) -> ConsentRecord:
        try:
            if isinstance(consent, ConsentRecord):
                return ConsentRecord.model_validate(consent.model_dump())
            data = dict(consent)
            version = str(data.get("statement_version") or "")
            if version not in CONSENT_STATEMENTS:
                raise ValueError(f"unknown consent statement version {version!r}; expected one of "
                                 f"{sorted(CONSENT_STATEMENTS)}")
            # The server fills in the statement text from its own table, so a
            # client cannot record agreement to a different text.
            data["statement_text"] = CONSENT_STATEMENTS[version]
            return ConsentRecord.model_validate(data)
        except (ValidationError, ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                err = exc.errors()[0] if exc.errors() else {}
                msg = f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', str(exc))}"
            else:
                msg = str(exc)
            raise OperationRefused("CONSENT_INVALID", f"recording refused without valid consent: {msg}", 422) from exc

    def start_recording(
        self,
        *,
        consent: ConsentRecord | Mapping[str, Any],
        label: str,
        scenario: str | None = None,
        notes: str | None = None,
        max_seconds: float | None = None,
    ) -> RecordingInfo:
        with self._control_lock:
            self._ensure_open()
            sid, mode = self._require_running()
            record = self._consent_from(consent)
            original: SourceMode | None = mode
            if mode == SourceMode.REPLAY:
                desc = self._describe()
                orig = desc.get("original_source_mode")
                try:
                    original = SourceMode(orig) if orig else None
                except ValueError:
                    original = None
                if desc.get("simulated"):
                    original = SourceMode.SIMULATION
            active = self.manager.active
            links = active.link_ids() if active is not None else []
            try:
                info = self.recorder.start(
                    consent=record, label=label, scenario=scenario, session_id=sid, source_mode=mode,
                    link_ids=links, config_version=self.config_version, notes=notes, max_seconds=max_seconds,
                    original_source_mode=original,
                )
            except RecordingRefused as exc:
                raise _refused_from_recording(exc) from exc
            with self._state_lock:
                self._recording_note = None
            return info

    def stop_recording(self) -> RecordingInfo:
        with self._control_lock:
            self._ensure_open()
            info = self.recorder.stop("stopped by operator")
            if info is None:
                last = self.recorder.last_finished
                hint = f" (the last recording {last.recording_id} ended: {last.stop_reason})" if last else ""
                raise OperationRefused("NOT_RECORDING", "no recording is active" + hint, 409)
            return info

    def _on_recording_auto_stop(self, info: RecordingInfo) -> None:
        with self._state_lock:
            self._recording_note = (f"RECORDING_STOPPED: {info.recording_id} ended with status "
                                    f"{info.status.value} ({info.stop_reason})")

    def list_recordings(self, limit: int = 1000) -> list[RecordingInfo]:
        rows = self.db.list_recordings(limit=limit)
        active = self.recorder.active
        if active is None:
            return rows
        return [active if r.recording_id == active.recording_id else r for r in rows]

    def delete_recording(self, recording_id: str) -> bool:
        with self._control_lock:
            self._ensure_open()
            active = self.recorder.active
            if active is not None and active.recording_id == recording_id:
                raise OperationRefused("RECORDING_ACTIVE", "stop the recording before deleting it", 409)
            if self._mode == SourceMode.REPLAY and self._source_info.get("recording_id") == recording_id:
                raise OperationRefused("RECORDING_IN_USE", "this recording is being replayed; stop the source first",
                                       409)
            try:
                removed = delete_recording(self.db, self.data_dir, recording_id)
            except RecordingRefused as exc:
                raise _refused_from_recording(exc) from exc
            except ValueError as exc:
                raise OperationRefused("INVALID_ARGUMENT", str(exc), 422) from exc
            if not removed:
                raise OperationRefused("RECORDING_NOT_FOUND", f"recording {recording_id} not found", 404)
            self._report_cache.clear()
            return True

    def export_recording(self, recording_id: str) -> Path:
        try:
            validate_id(recording_id, "recording_id")
        except ValueError as exc:
            raise OperationRefused("INVALID_ARGUMENT", str(exc), 422) from exc
        if self.db.get_recording(recording_id) is None:
            raise OperationRefused("RECORDING_NOT_FOUND", f"recording {recording_id} not found", 404)
        try:
            return export_recording(self.db, self.data_dir, recording_id)
        except FileNotFoundError as exc:
            raise OperationRefused("RECORDING_NOT_FOUND", str(exc)[:300], 404) from exc
        except ExportError as exc:
            raise OperationRefused("EXPORT_REFUSED", str(exc)[:300], 409) from exc
        except (ValueError, RecordingFormatError) as exc:
            raise OperationRefused("EXPORT_REFUSED", str(exc)[:300], 422) from exc

    # ------------------------------------------------------------------ events and validation
    def add_event(self, *, label: str, kind: EventKind | str, t_unix_ns: int | None = None,
                  notes: str | None = None) -> LabeledEvent:
        with self._control_lock:
            self._ensure_open()
            sid, _ = self._require_running()
            active = self.recorder.active
            try:
                ev = LabeledEvent(session_id=sid, recording_id=None if active is None else active.recording_id,
                                  t_unix_ns=self._unix_clock() if t_unix_ns is None else int(t_unix_ns),
                                  kind=EventKind(kind), label=label, notes=notes)
            except (ValidationError, ValueError) as exc:
                raise OperationRefused("INVALID_EVENT", str(exc).splitlines()[0][:300], 422) from exc
            self.db.add_event(ev)
            self._report_cache.clear()
            return ev

    def list_events(self, *, session_id: str | None = None, limit: int = 500) -> list[LabeledEvent]:
        sid = session_id if session_id is not None else self._session_id
        rows = self.db.list_events(session_id=sid, limit=MAX_EVENTS_RETURNED)
        return rows[-max(1, min(int(limit), MAX_EVENTS_RETURNED)):]

    def start_validation_run(self, *, scenario_id: str, placement: str = "", wall_description: str = "",
                             channel: int | None = None, conditions: str = "", notes: str = "") -> ValidationRun:
        with self._control_lock:
            self._ensure_open()
            sid, mode = self._require_running()
            if get_scenario(scenario_id) is None:
                raise OperationRefused("UNKNOWN_SCENARIO", f"unknown validation scenario {scenario_id!r}", 422)
            for run in self.db.list_validation_runs(session_id=sid, limit=None):
                if run.status == ValidationRunStatus.RUNNING:
                    raise OperationRefused("VALIDATION_RUN_ACTIVE", f"run {run.run_id} is still running; stop it first",
                                           409)
            active = self.recorder.active
            src = self.manager.active
            try:
                run = ValidationRun(
                    scenario_id=scenario_id, session_id=sid,
                    recording_id=None if active is None else active.recording_id,
                    source_mode=mode, started_at_unix_ns=self._unix_clock(), placement=placement,
                    wall_description=wall_description, channel=channel, conditions=conditions, notes=notes,
                    link_ids=[] if src is None else src.link_ids(),
                )
            except ValidationError as exc:
                raise OperationRefused("INVALID_RUN", str(exc).splitlines()[0][:300], 422) from exc
            self.db.add_validation_run(run)
            self._report_cache.clear()
            return run

    def stop_validation_run(self, run_id: str) -> ValidationRun:
        with self._control_lock:
            self._ensure_open()
            run = self.db.get_validation_run(run_id)
            if run is None:
                raise OperationRefused("RUN_NOT_FOUND", f"validation run {run_id} not found", 404)
            if run.status != ValidationRunStatus.RUNNING:
                raise OperationRefused("RUN_NOT_RUNNING", f"validation run {run_id} is {run.status.value}", 409)
            # Persist every decision made so far before the run is closed.
            with self._proc_lock:
                self._flush_activity(force=True)
            ended = self.db.end_validation_run(run_id, status=ValidationRunStatus.COMPLETE)
            self._report_cache.clear()
            assert ended is not None
            return ended

    def list_validation_runs(self) -> list[ValidationRun]:
        return self.db.list_validation_runs(limit=1000)

    def validation_report(self, *, force: bool = False) -> dict[str, Any]:
        """The through-wall report, cached for ``REPORT_TTL_S`` (it scans the activity log)."""
        with self._report_lock:
            if not force:
                hit, val = self._report_cache.get("r", REPORT_TTL_S)
                if hit:
                    return val
            with self._proc_lock:
                self._flush_activity(force=True)
            report = build_validation_report(self.db, THROUGH_WALL_CRITERIA_PATH)
            return self._report_cache.put("r", report)

    # ------------------------------------------------------------------ room
    def room(self) -> RoomGeometry:
        return self._room

    def put_room(self, geometry: RoomGeometry) -> dict[str, Any]:
        """Save the user's room (always USER_PROVIDED). A changed room hash
        invalidates calibrations bound to another room and the active baseline."""
        with self._control_lock:
            self._ensure_open()
            old_hash = self._room.config_hash()
            try:
                new_hash = save_room(self.cfg, geometry)
            except (ValueError, ValidationError, OSError) as exc:
                raise OperationRefused("ROOM_INVALID", str(exc).splitlines()[0][:300], 422) from exc
            room = geometry.model_copy(update={"provenance": GeometryProvenance.USER_PROVIDED})
            try:
                self.db.add_room_version(room)
            except (StorageError, ValueError):
                log.exception("could not store the room version")
            invalidated: list[str] = []
            engine_baselines_invalidated = False
            calibration_cancelled = False
            reason = f"ROOM_CHANGED: room geometry changed ({old_hash} -> {new_hash}); recalibrate"
            if new_hash != old_hash:
                for sc in self.db.list_calibrations(valid_only=True, limit=None):
                    if sc.record.room_config_hash != new_hash and self.db.invalidate_calibration(
                            sc.record.calibration_id, reason):
                        invalidated.append(sc.record.calibration_id)
                if self.engine.is_calibrating():
                    self.engine.cancel_baseline()
                    calibration_cancelled = True
                if self._active_calibration is not None:
                    self.engine.invalidate_baselines(reason)
                    engine_baselines_invalidated = True
                with self._state_lock:
                    self._active_calibration = None
                    self._calibration_note = reason
            with self._state_lock:
                self._room = room
                self._room_error = None
            self.predictor.set_room(room)
            self._zone_status_cache.clear()
            bindings, _ = self.registry.list_bindings()
            stale_models = [b.model_id for b in bindings if b.room_config_hash != new_hash]
            return {
                "room": room,
                "invalidated": {
                    "room_hash_changed": new_hash != old_hash,
                    "previous_room_hash": old_hash,
                    "room_hash": new_hash,
                    "calibrations": invalidated,
                    "active_baselines_invalidated": engine_baselines_invalidated,
                    "calibration_in_progress_cancelled": calibration_cancelled,
                    "zone_models_not_matching_room": stale_models,
                },
            }

    @staticmethod
    def example_room() -> RoomGeometry:
        return example_room()

    # ------------------------------------------------------------------ pose / hardware
    def _runtime_hw(self, links: Sequence[Any]) -> dict[str, Any]:
        if self._mode != SourceMode.LIVE:
            # Only live hardware can satisfy the gate's hardware requirements.
            return dict(runtime_hardware_profile(layout_id=None, packet_format=None, measured_rate_hz=None,
                                                 links=None))
        layouts = {ls.layout_id for ls in links if ls.layout_id}
        rates = [ls.acquisition_rate_hz for ls in links]
        fmts = {r.get("input_format") for r in self._source_info.get("receivers", [])}
        return dict(runtime_hardware_profile(
            layout_id=next(iter(layouts)) if len(layouts) == 1 else None,
            packet_format=next(iter(fmts)) if len(fmts) == 1 else None,
            measured_rate_hz=None if not rates or any(r is None for r in rates) else float(min(rates)),
            links=sum(1 for ls in links if ls.connected) or None,
        ))

    def pose_status(self, links: Sequence[Any] | None = None) -> PoseStatus:
        if links is None:
            links = self.manager.link_statuses(self._clock())
        rt_hw = self._runtime_hw(links)
        key = tuple(sorted((k, str(v)) for k, v in rt_hw.items()))
        hit, val = self._pose_cache.get(key, POSE_TTL_S)
        if hit:
            return val
        try:
            st = evaluate_pose_gate(self._pose_manifest_path, rt_hw, data_dir=self.data_dir)
        except Exception as exc:  # the gate stays closed on any failure
            log.exception("pose gate evaluation failed")
            st = PoseStatus(enabled=False, missing_requirements=[f"GATE_ERROR: {type(exc).__name__}"])
        return self._pose_cache.put(key, st)

    def hardware_report(self, *, refresh: bool = False) -> dict[str, Any]:
        """Non-destructive host inspection (cached ~30 s). Raises
        ``OperationRefused(HARDWARE_MODULE_UNAVAILABLE, 503)`` if the
        inspection module cannot be imported; no data is ever made up."""
        with self._hw_lock:
            if not refresh:
                hit, val = self._hw_cache.get("hw", HARDWARE_TTL_S)
                if hit:
                    return {**val, "cache_age_s": round((time.monotonic_ns() - self._hw_cache.at_ns) / 1e9, 1)}
            try:
                from . import hardware
            except Exception as exc:  # pragma: no cover - depends on the other module being present
                raise OperationRefused("HARDWARE_MODULE_UNAVAILABLE",
                                       f"hardware inspection module unavailable ({type(exc).__name__})", 503) from exc
            report = hardware.inspect_host()
            if "assessment" not in report:
                report["assessment"] = hardware.assess(report)
            self._hw_cache.put("hw", report)
            return {**report, "cache_age_s": 0.0}

    # ------------------------------------------------------------------ signals
    def signal_snapshot(self, link_id: str, seconds: float = 60.0) -> dict[str, Any]:
        if self._session_id is None:
            raise OperationRefused("NO_SOURCE", "no source selected", 404)
        if link_id not in self.engine.link_ids():
            raise OperationRefused("UNKNOWN_LINK", f"link {link_id!r} has no data in this session", 404)
        return self.engine.signal_snapshot(link_id, seconds)

    # ------------------------------------------------------------------ status
    def _describe(self) -> dict[str, Any]:
        src = self.manager.active
        if src is None:
            return {}
        try:
            d = src.describe()
            return d if isinstance(d, dict) else {}
        except Exception:
            log.exception("source describe() failed")
            return {}

    def _is_simulated(self, desc: Mapping[str, Any] | None = None) -> bool:
        if self._mode == SourceMode.SIMULATION:
            return True
        d = self._describe() if desc is None else desc
        return bool(d.get("simulated"))

    def _reload_evidence(self) -> None:
        from .capabilities import DEFAULT_EVIDENCE_PATH

        try:
            mtime = DEFAULT_EVIDENCE_PATH.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime == self._evidence_mtime:
            return
        self._evidence_mtime = mtime
        try:
            self._evidence = load_verification_evidence()
            self._evidence_error = None
        except EvidenceError as exc:
            self._evidence = {}
            self._evidence_error = f"VERIFICATION_EVIDENCE_UNAVAILABLE: {str(exc)[:200]}"

    def _baseline_valid(self) -> bool:
        cal = self._active_calibration
        if cal is None or cal.session_id != self._session_id:
            return False
        status = self.engine.baseline_status()
        return all(status.get(lid, {}).get("has_baseline") for lid in cal.link_ids)

    def _calibration_detail(self) -> str:
        if self.engine.is_calibrating():
            prog = self.engine.calibration_progress()
            parts = [f"{lid}: {p.get('windows_used', 0)} windows, {p.get('covered_s', 0.0):.0f} s"
                     for lid, p in prog.items()]
            return "CALIBRATING: recording a quiet baseline (" + "; ".join(parts) + "). Keep the room empty."
        cal = self._active_calibration
        if cal is not None and self._baseline_valid():
            age = max(0.0, (self._unix_clock() - cal.created_at_unix_ns) / 1e9)
            sim = " on SIMULATED data" if cal.summary.get("simulated_data") else ""
            return (f"Quiet baseline {cal.calibration_id} recorded {age:.0f} s ago{sim} "
                    f"({cal.window_count} windows, links {', '.join(cal.link_ids)}).")
        if self._calibration_note:
            return self._calibration_note
        if self._session_id is None:
            return "No source selected."
        return "NO_BASELINE: no valid quiet baseline in this session; record one with the room empty."

    def _source_detail(self, desc: Mapping[str, Any]) -> str | None:
        detail = self.manager.detail
        mode = self._mode
        info = self._source_info
        if mode == SourceMode.LIVE:
            rx = ", ".join(f"{r['receiver_id']} on {r['port']}" for r in info.get("receivers", []))
            text = f"LIVE serial receivers: {rx}"
        elif mode == SourceMode.REPLAY:
            text = (f"Replay of recording {info.get('recording_id')} (originally "
                    f"{desc.get('original_source_mode') or 'UNKNOWN'}) at {info.get('speed', 1.0):g}x")
        elif mode == SourceMode.SIMULATION:
            text = f"{SIMULATED_BANNER}. Scenario {info.get('scenario')} (seed {info.get('seed')})"
        else:
            return detail
        return f"{text}. {detail}" if detail else text

    def _notes(self, desc: Mapping[str, Any], simulated: bool) -> list[str]:
        notes: list[str] = []
        mode = self._mode
        if simulated:
            notes.append("SIMULATED_DATA: generated by a toy model in software; not a measurement of any room.")
        if mode == SourceMode.REPLAY:
            speed = float(self._source_info.get("speed", 1.0))
            notes.append("RECORDED_REPLAY: frames are re-played from a file, not measured now.")
            if speed != 1.0:
                notes.append(f"REPLAY_SPEED: replaying at {speed:g}x compresses the timeline; windows, packet "
                             "rates and hold times differ from the original. Use 1x for evaluation.")
        with self._stats_lock:
            drops = dict(self._queue_drops)
        for key, n in sorted(drops.items()):
            notes.append(f"HOST_QUEUE_DROPS: {n} event(s) for {key} dropped because processing fell behind "
                         f"(queue size {self.cfg.acquisition.frame_queue_size}).")
        if self._mode_guard_rejects:
            notes.append(f"FRAMES_REJECTED_SOURCE_MISMATCH: {self._mode_guard_rejects} frame(s) did not match the "
                         "active session/source mode and were discarded.")
        if self._activity_dropped:
            notes.append(f"ACTIVITY_LOG_INCOMPLETE: {self._activity_dropped} decision(s) could not be written to "
                         "the activity log.")
        if self._processing_errors:
            notes.append(f"PROCESSING_ERRORS: {self._processing_errors} ({self._last_processing_error}).")
        if self._background and self._started and (self._thread is None or not self._thread.is_alive()) \
                and not self._closing:
            notes.append("PROCESSING_STOPPED: the processing thread is not running; results are not current.")
        if self._end_of_stream and mode is not None:
            notes.append(f"END_OF_STREAM: {self._end_of_stream}")
        if self._room.provenance == GeometryProvenance.EXAMPLE:
            notes.append("ROOM_EXAMPLE: the room shown is the shipped EXAMPLE geometry, not your room.")
        if self._room_error:
            notes.append(self._room_error)
        if self._recording_note:
            notes.append(self._recording_note)
        if self._evidence_error:
            notes.append(self._evidence_error)
        return notes

    @staticmethod
    def _localization_text(zone: ZonePrediction) -> str:
        if zone.state == ZoneState.ESTIMATE:
            return (f"EXPERIMENTAL ESTIMATE: {zone.zone_label or zone.zone_id} (the zone centre is a display "
                    "anchor, not a measured position)")
        reasons = list(zone.reasons)
        if zone.state == ZoneState.DISABLED:
            if any(r.startswith(("NO_ENABLED_MODEL", "MODEL_NOT_ENABLED")) for r in reasons):
                return "DISABLED: no zone model has passed the predefined criteria"
            first = next((r for r in reasons if not r.startswith("NO_SOURCE")), reasons[0] if reasons else "")
            return f"DISABLED: {first}" if first else "DISABLED"
        return f"ABSTAIN: {reasons[0]}" if reasons else "ABSTAIN"

    def build_status(self) -> SystemStatus:
        """Snapshot of everything the UI shows. Cheap enough for the WS push rate."""
        self._reload_evidence()
        now_mono = self._clock()
        mode = self._mode
        session = self._session_id
        desc = self._describe()
        simulated = self._is_simulated(desc)
        state = self.manager.source_state()
        links = self.manager.link_statuses(now_mono)
        activity = sorted(
            (r for r in self.engine.latest().values() if session is not None and r.provenance.session_id == session),
            key=lambda r: r.link_id,
        )
        zone_st = self.zone_status()
        zone = self._current_zone(zone_st)
        if simulated and zone.state != ZoneState.DISABLED:
            # Defence in depth: the predictor refuses SIMULATION provenance, and a
            # replay of simulated data must never yield a zone output either.
            zone = ZonePrediction(state=ZoneState.DISABLED, criteria_version=zone.criteria_version,
                                  reasons=["SIMULATED_SOURCE: zone estimation never runs on simulated data "
                                           "(including replays of simulated recordings)"])
        pose = self.pose_status(links)
        try:
            report = self.validation_report()
            through_wall = str(report.get("through_wall_status", "UNVERIFIED"))
            tw_detail = report.get("explanation")
        except Exception as exc:
            log.exception("validation report unavailable")
            through_wall, tw_detail = "UNVERIFIED", f"validation report unavailable ({type(exc).__name__})"
        if through_wall not in ("UNVERIFIED", "VALIDATED", "NOT_DISTINGUISHABLE"):
            through_wall = "UNVERIFIED"
        baseline_valid = self._baseline_valid()
        cal_detail = self._calibration_detail()
        ctx = CapabilityContext(
            source_mode=mode,
            source_state=state,
            live_frames_with_valid_layout=self._session_live_valid_frames if mode == SourceMode.LIVE else 0,
            connected_live_links=sum(1 for ls in links if ls.connected) if mode == SourceMode.LIVE else 0,
            baseline_valid=baseline_valid,
            calibration_detail=cal_detail,
            zone_enabled=zone_st.get("state") == "ENABLED" and not simulated,
            zone_reasons=[str(r) for r in zone_st.get("reasons", [])] + (
                ["SIMULATED_SOURCE: the data is simulated; zone estimation stays off"] if simulated else []),
            pose_status=pose,
            through_wall_status=through_wall,
            evidence=self._evidence,
        )
        capabilities: list[CapabilityStatus] = build_capabilities(ctx)
        rec = self.recorder.active
        return SystemStatus(
            server_time_unix_ns=self._unix_clock(),
            source_mode=mode,
            source_banner=SOURCE_MODE_BANNER[mode] if mode is not None else "NO SOURCE",
            simulated=simulated,
            source_state=state,
            source_detail=self._source_detail(desc),
            session_id=session,
            hardware_required=self._live_valid_frames_total == 0,
            capabilities=capabilities,
            links=links,
            activity=activity,
            zone=zone,
            pose=pose,
            calibration=self._active_calibration,
            calibration_valid=baseline_valid,
            calibration_detail=cal_detail,
            localization_status=self._localization_text(zone),
            through_wall_status=through_wall,  # type: ignore[arg-type]
            through_wall_detail=tw_detail,
            recording_active=rec is not None,
            recording_id=None if rec is None else rec.recording_id,
            stale_clear_timeout_s=self.cfg.detection.clear_stale_after_s,
            unsupported_capabilities=[UnsupportedCapability(**c) for c in UNSUPPORTED_CAPABILITIES],
            notes=self._notes(desc, simulated),
        )

    def health(self, bind_host: str | None = None) -> dict[str, Any]:
        return {
            "status": "ok" if not self._closing else "shutting_down",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "uptime_s": round(time.monotonic() - self._started_mono, 3),
            "source_state": self.manager.source_state().value,
            "bind_host": bind_host if bind_host is not None else self.cfg.server.host,
        }

    # ------------------------------------------------------------------ misc helpers for tests/CLI
    def activity_history(self, link_id: str, seconds: float = 600.0) -> list[ActivityResult]:
        """Results of the current session for one link (newest last)."""
        sid = self._session_id
        return [r for r in self.engine.history(link_id, seconds) if r.provenance.session_id == sid]

    def link_states(self) -> dict[str, ActivityState]:
        sid = self._session_id
        return {lid: ActivityState(r.state) for lid, r in self.engine.latest().items()
                if r.provenance.session_id == sid}
