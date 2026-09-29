"""Builders for zone-estimation (capability C) SOFTWARE tests.

Two kinds of test data live here, and neither says anything about how well
zone estimation works in a real room:

* SIMULATED sessions from :func:`roomsense.acquisition.synthetic.zone_session_scenario`
  (every frame flagged SYNTHETIC). They exercise the dataset/train/evaluate
  code end to end, and a model trained on them must always stay disabled.
* HAND-BUILT fakes: feature vectors with chosen values, a logistic regression
  fitted on random numbers, and a hand-written "passing" report. They exist
  only to test the predictor's gating logic (what must disable, abstain or
  estimate). The "LIVE" source mode on fake provenance is a bookkeeping value,
  not a measurement.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np

from roomsense.acquisition.synthetic import (
    SYNTHETIC_ZONES,
    ZONES_3RX_LINKS,
    ZONES_3RX_POSITIONS,
    generate_frames,
    zone_session_scenario,
)
from roomsense.config import AppConfig
from roomsense.inference.zone.criteria import default_criteria_path, load_criteria
from roomsense.inference.zone.dataset import (
    FEATURE_SET_VERSION,
    SessionSpec,
    ZoneDataset,
    build_dataset,
    feature_names_for,
    feature_row,
)
from roomsense.inference.zone.evaluate import CRITERIA_FILE_CHECKS, ELIGIBILITY_CHECKS, REPORT_VERSION
from roomsense.inference.zone.registry import ModelBinding, ZoneModelRegistry
from roomsense.inference.zone.train import build_pipeline
from roomsense.processing.features import FEATURE_NAMES, FeatureVector
from roomsense.schemas import (
    GeometryProvenance,
    LinkDef,
    NodeRole,
    Provenance,
    RoomGeometry,
    SensorNode,
    SourceMode,
    Vec2,
    Vec3,
    Zone,
)

ZONE_IDS = ("A", "B", "C")
LABELS = ("A", "B", "C", "EMPTY", "OUTSIDE_TARGET_ROOM")
LINK_ORDER = tuple(f"{t}->{r}" for t, r in ZONES_3RX_LINKS)
# 5 s trimmed at each end + 2 s window -> ~32 windows per session. 12.5 Hz keeps
# the test suite fast; it is still >= min_frames_per_window per 2 s window.
SESSION_S = 28.0
RATE_HZ = 12.5
ROUND_SPACING_NS = 3_600 * 1_000_000_000  # one "recording round" per simulated hour
BASE_UNIX_NS = 1_700_000_000_000_000_000
CRITERIA_VERSION = load_criteria(default_criteria_path()).criteria_version
T_END_NS = 5_000_000_000_000  # arbitrary host-monotonic instant for hand-built windows


def make_room(provenance: GeometryProvenance = GeometryProvenance.USER_PROVIDED,
              zones: Sequence[str] = ZONE_IDS) -> RoomGeometry:
    """The simulated 4 x 3.5 m room with zones A/B/C (a TEST FIXTURE).

    It is marked USER_PROVIDED so that, in the synthetic end-to-end test, the
    synthetic data is the reason the model stays disabled.
    """
    nodes = [
        SensorNode(id=nid, role=NodeRole.TX if nid.startswith("tx") else NodeRole.RX, label=nid,
                   position=Vec3(x=p[0], y=p[1], z=p[2]), inside_target_room=True)
        for nid, p in ZONES_3RX_POSITIONS.items()
    ]
    zone_objs = []
    for zid in zones:
        x0, x1, y0, y1 = SYNTHETIC_ZONES[zid]
        zone_objs.append(Zone(id=zid, label=f"Zone {zid}",
                              polygon=[Vec2(x=x0, y=y0), Vec2(x=x1, y=y0), Vec2(x=x1, y=y1), Vec2(x=x0, y=y1)]))
    return RoomGeometry(
        geometry_id="test-room",
        provenance=provenance,
        name="software test room",
        width_m=4.0,
        depth_m=3.5,
        nodes=nodes,
        links=[LinkDef(link_id=f"{t}->{r}", transmitter_id=t, receiver_id=r) for t, r in ZONES_3RX_LINKS],
        zones=zone_objs,
    )


def session_start(round_idx: int, label_idx: int) -> int:
    """Rounds are interleaved: every class once per round, rounds in order."""
    return BASE_UNIX_NS + round_idx * ROUND_SPACING_NS + label_idx * 120 * 1_000_000_000


def synthetic_spec(label: str, round_idx: int, *, seed_base: int = 1000, duration_s: float = SESSION_S,
                   split: str | None = None, declared_synthetic: bool = True, key_prefix: str = "syn",
                   drop: tuple[str, float, float] | None = None) -> SessionSpec:
    """One SIMULATED labelled session. ``drop=(link, t0_s, t1_s)`` removes
    that link's frames in the interval (a simulated receiver outage)."""
    li = LABELS.index(label)
    seed = seed_base + round_idx * 10 + li
    key = f"{key_prefix}_r{round_idx}_{label}"
    start = session_start(round_idx, li)
    scn = zone_session_scenario(label, seed=seed, duration_s=duration_s, rate_hz=RATE_HZ)

    def frames():
        for f in generate_frames(scn, key, start_unix_ns=start):
            if drop is not None and f.link_id == drop[0]:
                t = (f.host_arrival_unix_ns - start) / 1e9
                if drop[1] <= t < drop[2]:
                    continue
            yield f

    return SessionSpec(label=label, session_key=key, frames=frames, synthetic=declared_synthetic, split=split,
                       expected_rate_hz=RATE_HZ)


def synthetic_specs(rounds: Sequence[int] = range(5), labels: Sequence[str] = LABELS, **kw) -> list[SessionSpec]:
    return [synthetic_spec(lbl, r, **kw) for r in rounds for lbl in labels]


@lru_cache(maxsize=4)
def cached_dataset(rounds: tuple[int, ...] = (0, 1, 2, 3, 4), seed_base: int = 1000) -> ZoneDataset:
    """Built once per test run: 5 rounds x 5 classes of SIMULATED sessions."""
    crit = load_criteria()
    return build_dataset(synthetic_specs(rounds, seed_base=seed_base), AppConfig(),
                         trim_session_edges_s=crit.trim_session_edges_s, allowed_labels=LABELS)


# ---------------------------------------------------------------------------
# Hand-built fakes for predictor gating tests
# ---------------------------------------------------------------------------

# Chosen amplitude-variation levels per class and link (FAKE numbers).
_CLASS_LEVELS: dict[str, tuple[float, float, float]] = {
    "A": (0.30, 0.02, 0.02),
    "B": (0.02, 0.30, 0.02),
    "C": (0.02, 0.02, 0.30),
    "EMPTY": (0.01, 0.01, 0.01),
    "OUTSIDE_TARGET_ROOM": (0.10, 0.10, 0.10),
}


def make_fv(link_id: str, level: float, *, t_end_ns: int = T_END_NS, n_subcarriers: int = 52,
            t_unix_end_ns: int | None = None, names: tuple[str, ...] = FEATURE_NAMES) -> FeatureVector:
    """A hand-built FeatureVector whose every per-subcarrier value is ``level``
    (cv) / ``level / 2`` (tdiff)."""
    cv = np.full(n_subcarriers, level)
    td = np.full(n_subcarriers, level / 2)
    valid = np.ones(n_subcarriers, dtype=bool)
    return FeatureVector(
        link_id=link_id,
        t_end_ns=t_end_ns,
        names=names,
        values=np.array([level, level / 2]) if len(names) == 2 else np.full(len(names), level),
        per_subcarrier={"amp_cv_median": cv, "amp_tdiff_median": td},
        profile=np.full(n_subcarriers, 20.0),
        k=np.arange(n_subcarriers, dtype=np.int32),
        t_start_ns=t_end_ns - 2_000_000_000,
        valid=valid,
        n_frames=50,
        n_valid_subcarriers=n_subcarriers,
        layout_id="test-layout",
        t_unix_end_ns=t_unix_end_ns,
    )


def features_for(label: str, *, links: Sequence[str] = LINK_ORDER, t_end_ns: int = T_END_NS,
                 skew_ns: dict[str, int] | None = None) -> dict[str, FeatureVector]:
    levels = _CLASS_LEVELS[label]
    skew = skew_ns or {}
    return {lid: make_fv(lid, levels[i], t_end_ns=t_end_ns + skew.get(lid, 0))
            for i, lid in enumerate(LINK_ORDER) if lid in links}


def fake_pipeline(*, n_features: int | None = None, classes: Sequence[str] = LABELS, seed: int = 0):
    """A logistic-regression pipeline fitted on RANDOM numbers around the fake
    class levels. ``n_features`` != 30 builds a deliberately wrong-shaped model."""
    rng = np.random.default_rng(seed)
    X, y = [], []
    for c in classes:
        row, _ = feature_row(features_for(c), LINK_ORDER)
        assert row is not None
        for _ in range(40):
            X.append(row + rng.normal(0, 0.05, row.size))
            y.append(c)
    Xa = np.asarray(X)
    if n_features is not None:
        Xa = Xa[:, :n_features] if n_features <= Xa.shape[1] else np.hstack(
            [Xa, rng.normal(0, 1, (Xa.shape[0], n_features - Xa.shape[1]))])
    pipe = build_pipeline(Xa.shape[1])
    pipe.fit(Xa, np.asarray(y))
    return pipe


def passing_report(criteria_version: str = CRITERIA_VERSION) -> dict:
    """HAND-BUILT FAKE report in which every check passes (gating tests only)."""
    return {
        "report_version": REPORT_VERSION,
        "note": "HAND-BUILT FAKE REPORT FOR SOFTWARE GATING TESTS - NOT A MEASUREMENT",
        "enabled": True,
        "synthetic_data_used": False,
        "criteria_version": criteria_version,
        "criteria": [{"name": n, "source": "fake", "comparator": "==", "threshold": None, "measured": 1,
                      "passed": True, "detail": "fake"} for n in CRITERIA_FILE_CHECKS + ELIGIBILITY_CHECKS],
    }


def save_fake_model(
    registry: ZoneModelRegistry,
    *,
    room: RoomGeometry | None = None,
    hardware_signature: str = "hw-fake-0001",
    config_version: str | None = None,
    criteria_version: str = CRITERIA_VERSION,
    report: dict | None = None,
    synthetic_data_used: bool = False,
    threshold: float = 0.5,
    n_features: int | None = None,
    created_at_unix_ns: int | None = None,
) -> ModelBinding:
    room = room or make_room()
    return registry.save(
        fake_pipeline(n_features=n_features),
        hardware_signature=hardware_signature,
        room_config_hash=room.config_hash(),
        config_version=config_version or AppConfig().config_version(),
        criteria_version=criteria_version,
        link_order=LINK_ORDER,
        feature_names=feature_names_for(LINK_ORDER),
        feature_set_version=FEATURE_SET_VERSION,
        threshold=threshold,
        classes=LABELS,
        zone_ids=ZONE_IDS,
        synthetic_data_used=synthetic_data_used,
        report=report if report is not None else passing_report(criteria_version),
        created_at_unix_ns=created_at_unix_ns,
    )


def fake_provenance(mode: SourceMode = SourceMode.LIVE) -> Provenance:
    return Provenance(
        source_mode=mode,
        session_id="sess_fake",
        link_ids=list(LINK_ORDER),
        window_start_unix_ns=None,
        window_end_unix_ns=None,
        window_frame_count=50,
        config_version=AppConfig().config_version(),
        computed_at_unix_ns=1,
    )


def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
