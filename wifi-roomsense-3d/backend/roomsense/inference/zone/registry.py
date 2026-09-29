"""On-disk store for zone models: ``<data_dir>/models/<model_id>.{joblib,json}``.

* The ``.joblib`` file holds the fitted scikit-learn pipeline.
* The ``.json`` file holds the **binding** (hardware signature, room hash,
  config version, criteria version, link order, feature names, frozen
  threshold, classes, synthetic flag, SHA-256 of the joblib file) and the
  full evaluation report.

Loading is deliberately narrow:

* Models are only ever loaded from this app's own ``<data_dir>/models``
  directory, by an id matching ``^[A-Za-z0-9_-]{1,64}$``. Symlinks are
  refused.
* The joblib bytes are read once, their SHA-256 is compared with the JSON
  binding, and only then are those same bytes unpickled. A replaced or
  truncated model file is therefore never loaded.
* joblib uses pickle, which can execute code. That is acceptable only
  because these files are written by this app into its own local data
  directory. Never point the registry at files from anywhere else.

``enabled`` in the binding is recomputed on save **and** on load from the
report (:func:`~.evaluate.report_supports_enabled`), so hand-editing the
flag in the JSON cannot enable a model whose report does not pass.
When a :class:`~roomsense.storage.db.Database` is given, a row is also
written to its ``zone_models`` table.

The stored report lists the recordings a model was trained on
(``dataset.sessions[].recording_id`` and the split assignment), so
:meth:`ZoneModelRegistry.delete_models_trained_on` can remove every model
derived from a recording the user deletes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import joblib

from .evaluate import json_safe, report_supports_enabled

__all__ = [
    "MODEL_FORMAT",
    "MODELS_SUBDIR",
    "MAX_MODEL_BYTES",
    "MAX_BINDING_BYTES",
    "UNAVAILABLE_SIGNATURE",
    "RegistryError",
    "ModelBinding",
    "LoadedModel",
    "ZoneModelRegistry",
]

MODEL_FORMAT = "roomsense-zone-model-v1"
MODELS_SUBDIR = "models"
MAX_MODEL_BYTES = 64 * 1024 * 1024
MAX_BINDING_BYTES = 16 * 1024 * 1024
# Stored when no hardware signature was available; never equals a real one.
UNAVAILABLE_SIGNATURE = "unavailable"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RegistryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ModelBinding:
    """What a stored model is valid for. Compared field by field at runtime."""

    model_id: str
    created_at_unix_ns: int
    hardware_signature: str
    room_config_hash: str
    config_version: str
    criteria_version: str
    link_order: tuple[str, ...]
    feature_names: tuple[str, ...]
    feature_set_version: str
    threshold: float
    classes: tuple[str, ...]
    zone_ids: tuple[str, ...]
    synthetic_data_used: bool
    enabled: bool
    joblib_sha256: str
    joblib_bytes: int
    report: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    enabled_reasons: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("report")
        for k in ("link_order", "feature_names", "classes", "zone_ids", "enabled_reasons"):
            d[k] = list(d[k])
        return d


@dataclass(frozen=True)
class LoadedModel:
    binding: ModelBinding
    pipeline: Any


def _require(d: dict[str, Any], key: str, typ: type | tuple[type, ...]) -> Any:
    if key not in d:
        raise RegistryError("BINDING_INVALID", f"binding field {key!r} missing")
    v = d[key]
    if typ in (int, float) or typ == (int, float):
        if isinstance(v, bool):
            raise RegistryError("BINDING_INVALID", f"binding field {key!r} has the wrong type")
    if not isinstance(v, typ):
        raise RegistryError("BINDING_INVALID", f"binding field {key!r} has the wrong type")
    return v


def _training_recording_ids(report: dict[str, Any]) -> set[str]:
    """Recording ids (session keys) a training report says it used, in any split."""
    ids: set[str] = set()
    dataset = report.get("dataset")
    if isinstance(dataset, dict) and isinstance(dataset.get("sessions"), list):
        for sess in dataset["sessions"]:
            if isinstance(sess, dict):
                for key in ("recording_id", "session_key"):
                    if isinstance(sess.get(key), str):
                        ids.add(sess[key])
    splits = report.get("splits")
    if isinstance(splits, dict) and isinstance(splits.get("assignment"), dict):
        ids.update(k for k in splits["assignment"] if isinstance(k, str))
    return ids


def _binding_mentions_recording(raw: bytes, recording_id: str) -> bool:
    """True if the model file ``raw`` (binding + report JSON) was trained on
    ``recording_id``. A file that cannot be parsed, or whose report has no
    session lists, is matched on the recording id as an exact JSON string
    anywhere in it, so a damaged file never hides a model."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        doc = None
    report = doc.get("report") if isinstance(doc, dict) else None
    if isinstance(report, dict) and ("dataset" in report or "splits" in report):
        return recording_id in _training_recording_ids(report)
    return json.dumps(recording_id).encode("utf-8") in raw


def _str_list(d: dict[str, Any], key: str) -> tuple[str, ...]:
    v = _require(d, key, list)
    if not all(isinstance(x, str) for x in v):
        raise RegistryError("BINDING_INVALID", f"binding field {key!r} must be a list of strings")
    return tuple(v)


class ZoneModelRegistry:
    """Save/list/load zone models. Thread-safe."""

    def __init__(self, data_dir: Path | str, db: Any | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.db = db
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- paths
    @property
    def models_dir(self) -> Path:
        return (self.data_dir / MODELS_SUBDIR).resolve()

    def _paths(self, model_id: str) -> tuple[Path, Path]:
        if not isinstance(model_id, str) or _ID_RE.fullmatch(model_id) is None:
            raise RegistryError("INVALID_MODEL_ID", "model id must match ^[A-Za-z0-9_-]{1,64}$")
        base = self.models_dir
        pj, pm = base / f"{model_id}.json", base / f"{model_id}.joblib"
        for p in (pj, pm):
            if p.parent != base:
                raise RegistryError("INVALID_MODEL_ID", "model path escapes the models directory")
        return pj, pm

    @staticmethod
    def _read_bounded(path: Path, limit: int) -> bytes:
        if path.is_symlink():
            raise RegistryError("MODEL_FILE_INVALID", f"{path.name} is a symlink; refusing to read it")
        if not path.is_file():
            raise RegistryError("MODEL_NOT_FOUND", f"{path.name} does not exist")
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise RegistryError("MODEL_FILE_INVALID", f"{path.name} is larger than {limit} bytes")
        return data

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    # ------------------------------------------------------------------ save
    def save(
        self,
        pipeline: Any,
        *,
        hardware_signature: str | None,
        room_config_hash: str,
        config_version: str,
        criteria_version: str,
        link_order: tuple[str, ...] | list[str],
        feature_names: tuple[str, ...] | list[str],
        feature_set_version: str,
        threshold: float,
        classes: tuple[str, ...] | list[str],
        zone_ids: tuple[str, ...] | list[str],
        synthetic_data_used: bool,
        report: dict[str, Any],
        model_id: str | None = None,
        created_at_unix_ns: int | None = None,
    ) -> ModelBinding:
        """Write the pipeline and its binding. Returns the stored binding.

        ``enabled`` is derived from the report here; callers cannot set it.
        """
        with self._lock:
            mid = model_id if model_id is not None else f"zm_{uuid.uuid4().hex[:24]}"
            pj, pm = self._paths(mid)
            if pj.exists() or pm.exists():
                raise RegistryError("MODEL_EXISTS", f"model {mid} already exists")
            buf = io.BytesIO()
            joblib.dump(pipeline, buf)
            blob = buf.getvalue()
            if len(blob) > MAX_MODEL_BYTES:
                raise RegistryError("MODEL_TOO_LARGE", f"serialised model is {len(blob)} bytes")
            created = int(created_at_unix_ns) if created_at_unix_ns is not None else time.time_ns()
            safe_report = json_safe(dict(report))
            safe_report["model_id"] = mid
            ok, why = report_supports_enabled(safe_report)
            enabled = bool(ok and not synthetic_data_used)
            if synthetic_data_used and ok:
                why = ["SYNTHETIC_DATA_NOT_ALLOWED: the model was trained with synthetic sessions"]
            binding = ModelBinding(
                model_id=mid,
                created_at_unix_ns=created,
                hardware_signature=hardware_signature or UNAVAILABLE_SIGNATURE,
                room_config_hash=room_config_hash,
                config_version=config_version,
                criteria_version=criteria_version,
                link_order=tuple(link_order),
                feature_names=tuple(feature_names),
                feature_set_version=feature_set_version,
                threshold=float(threshold),
                classes=tuple(classes),
                zone_ids=tuple(zone_ids),
                synthetic_data_used=bool(synthetic_data_used),
                enabled=enabled,
                joblib_sha256=hashlib.sha256(blob).hexdigest(),
                joblib_bytes=len(blob),
                report=safe_report,
                enabled_reasons=tuple(why),
            )
            doc = {"format": MODEL_FORMAT, "binding": binding.to_json_dict(), "report": safe_report}
            text = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(text) > MAX_BINDING_BYTES:
                raise RegistryError("MODEL_TOO_LARGE", f"binding/report JSON is {len(text)} bytes")
            self.models_dir.mkdir(parents=True, exist_ok=True)
            # Model file first: a JSON without its model is never listed as loadable.
            self._atomic_write(pm, blob)
            self._atomic_write(pj, text)
            try:
                self._record_db(binding)
            except Exception:
                # Keep files and DB consistent: no model without its row.
                for p in (pj, pm):
                    p.unlink(missing_ok=True)
                raise
            return binding

    def _record_db(self, b: ModelBinding) -> None:
        if self.db is None:
            return
        from ...storage.models import ZoneModelRecord  # storage is optional for the registry

        rec = ZoneModelRecord(
            model_id=b.model_id,
            created_at_unix_ns=b.created_at_unix_ns,
            criteria_version=b.criteria_version,
            hardware_signature=b.hardware_signature,
            room_config_hash=b.room_config_hash,
            config_version=b.config_version,
            link_ids=list(b.link_order),
            report={k: b.report.get(k) for k in ("report_version", "enabled", "enabled_reasons", "criteria",
                                                 "criteria_version", "synthetic_data_used", "counts")},
            enabled=b.enabled and not b.synthetic_data_used,
            artifact_relpath=f"{MODELS_SUBDIR}/{b.model_id}.joblib",
            synthetic_data_used=b.synthetic_data_used,
        )
        self.db.add_zone_model(rec)

    # ------------------------------------------------------------------ read
    def load_binding(self, model_id: str) -> ModelBinding:
        """Parse and validate ``<model_id>.json``; ``enabled`` is re-derived."""
        pj, _ = self._paths(model_id)
        raw = self._read_bounded(pj, MAX_BINDING_BYTES)
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RegistryError("BINDING_INVALID", f"{pj.name} is not valid JSON") from exc
        if not isinstance(doc, dict) or doc.get("format") != MODEL_FORMAT:
            raise RegistryError("BINDING_INVALID", f"{pj.name} is not a {MODEL_FORMAT} file")
        b = doc.get("binding")
        report = doc.get("report")
        if not isinstance(b, dict) or not isinstance(report, dict):
            raise RegistryError("BINDING_INVALID", "binding or report missing")
        if _require(b, "model_id", str) != model_id:
            raise RegistryError("BINDING_INVALID", "model id in the JSON does not match its file name")
        synthetic = _require(b, "synthetic_data_used", bool)
        ok, why = report_supports_enabled(report)
        enabled = bool(ok and not synthetic and _require(b, "enabled", bool))
        if not enabled and not why:
            why = ["MODEL_NOT_ENABLED: binding does not enable this model"]
        threshold = float(_require(b, "threshold", (int, float)))
        return ModelBinding(
            model_id=model_id,
            created_at_unix_ns=int(_require(b, "created_at_unix_ns", int)),
            hardware_signature=_require(b, "hardware_signature", str),
            room_config_hash=_require(b, "room_config_hash", str),
            config_version=_require(b, "config_version", str),
            criteria_version=_require(b, "criteria_version", str),
            link_order=_str_list(b, "link_order"),
            feature_names=_str_list(b, "feature_names"),
            feature_set_version=_require(b, "feature_set_version", str),
            threshold=threshold,
            classes=_str_list(b, "classes"),
            zone_ids=_str_list(b, "zone_ids"),
            synthetic_data_used=synthetic,
            enabled=enabled,
            joblib_sha256=_require(b, "joblib_sha256", str),
            joblib_bytes=int(_require(b, "joblib_bytes", int)),
            report=report,
            enabled_reasons=tuple(why),
        )

    def load(self, model_id: str) -> LoadedModel:
        """Binding plus pipeline, after verifying the joblib file's SHA-256."""
        with self._lock:
            binding = self.load_binding(model_id)
            _, pm = self._paths(model_id)
            blob = self._read_bounded(pm, MAX_MODEL_BYTES)
            digest = hashlib.sha256(blob).hexdigest()
            if digest != binding.joblib_sha256 or len(blob) != binding.joblib_bytes:
                raise RegistryError("MODEL_FILE_MISMATCH",
                                    f"{pm.name} does not match the SHA-256 recorded in its binding; not loaded")
            # Unpickle exactly the bytes that were hashed (no second read).
            pipeline = joblib.load(io.BytesIO(blob))
            return LoadedModel(binding=binding, pipeline=pipeline)

    def list_bindings(self) -> tuple[list[ModelBinding], list[str]]:
        """All readable bindings, newest first, plus one error line per
        unreadable file."""
        with self._lock:
            base = self.models_dir
            if not base.is_dir():
                return [], []
            out: list[ModelBinding] = []
            errors: list[str] = []
            for p in sorted(base.glob("*.json")):
                mid = p.name[: -len(".json")]
                if _ID_RE.fullmatch(mid) is None:
                    continue
                try:
                    out.append(self.load_binding(mid))
                except RegistryError as exc:
                    errors.append(f"{exc.code}: {mid}: {exc.detail}")
            out.sort(key=lambda b: (b.created_at_unix_ns, b.model_id), reverse=True)
            return out, errors

    def delete(self, model_id: str) -> bool:
        with self._lock:
            removed = False
            for p in self._paths(model_id):
                if p.exists() or p.is_symlink():
                    p.unlink()
                    removed = True
            if self.db is not None and self.db.delete_zone_model(model_id):
                removed = True
            return removed

    def models_trained_on(self, recording_id: str) -> list[str]:
        """Ids of stored models whose training data (any split) included
        ``recording_id``, read from each model's JSON file (see
        :func:`_binding_mentions_recording`)."""
        if not isinstance(recording_id, str) or _ID_RE.fullmatch(recording_id) is None:
            raise RegistryError("INVALID_RECORDING_ID", "recording id must match ^[A-Za-z0-9_-]{1,64}$")
        with self._lock:
            base = self.models_dir
            if not base.is_dir():
                return []
            out: list[str] = []
            for p in sorted(base.glob("*.json")):
                mid = p.name[: -len(".json")]
                if _ID_RE.fullmatch(mid) is None:
                    continue
                try:
                    raw = self._read_bounded(p, MAX_BINDING_BYTES)
                except RegistryError:
                    continue  # a symlink or an oversized file: never written or loaded by this app
                if _binding_mentions_recording(raw, recording_id):
                    out.append(mid)
            return out

    def delete_models_trained_on(self, recording_id: str) -> list[str]:
        """Delete (files and DB rows) every model trained on ``recording_id``;
        returns their ids. A model derived from data the user deleted must
        not survive it. Call :meth:`ZonePredictor.refresh` afterwards."""
        with self._lock:
            ids = self.models_trained_on(recording_id)
            for mid in ids:
                self.delete(mid)
            return ids
