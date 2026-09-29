"""Tests for the experimental pose-output interface.

The "models" here are FAKE test doubles that return fixed numbers. They are
not pose estimators, and nothing here measures pose accuracy.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import ValidationError

from roomsense.config import REPO_ROOT
from roomsense.inference.pose import (
    PoseGateClosedError,
    PoseModel,
    PoseOutput,
    PoseOutputRejected,
    evaluate_pose_gate,
    run_experimental_inference,
)
from roomsense.schemas import PoseStatus, Provenance, SourceMode

MANIFEST_SHA = "a" * 64


def provenance() -> Provenance:
    return Provenance(
        source_mode=SourceMode.LIVE,
        session_id="test-session",
        link_ids=["tx1->rx1"],
        window_start_unix_ns=1,
        window_end_unix_ns=2,
        window_frame_count=50,
        config_version="cfg-test",
        computed_at_unix_ns=time.time_ns(),
    )


def open_status(model_id: str = "fake-test-model") -> PoseStatus:
    # A hand-built "enabled" status, only to exercise the interface. The real
    # gate never produces this in this release (see test_pose_gate.py).
    return PoseStatus(enabled=True, model_id=model_id, manifest={"model_id": model_id, "manifest_sha256": MANIFEST_SHA})


class FakeModel:
    """Returns fixed keypoints; stands in for a future adapter."""

    def __init__(self, output: Any = None, model_id: str = "fake-test-model") -> None:
        self._output = output if output is not None else {
            "keypoints": [{"name": "head", "x": 0.5, "y": 0.2, "score": 3.1}],
            "coordinate_frame": "fake-normalised-2d",
        }
        self._manifest = {"model_id": model_id}
        self.calls = 0

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    def infer(self, window: Any) -> dict[str, Any]:
        self.calls += 1
        return self._output


def test_fake_model_satisfies_protocol():
    assert isinstance(FakeModel(), PoseModel)


def test_real_gate_status_refuses_inference():
    status = evaluate_pose_gate(REPO_ROOT / "configs" / "pose_model_manifest.json", {})
    model = FakeModel()
    with pytest.raises(PoseGateClosedError, match="capability D is disabled"):
        run_experimental_inference(model, window=None, status=status, provenance=provenance())
    assert model.calls == 0  # the model is never even called


def test_output_is_labelled_experimental_with_provenance():
    out = run_experimental_inference(FakeModel(), window=None, status=open_status(), provenance=provenance())
    assert out.label == "EXPERIMENTAL"
    assert out.display_route == "EXPERIMENTAL_RESEARCH_PANEL"
    assert out.validated_live_view is False
    assert out.model_id == "fake-test-model"
    assert out.manifest_sha256 == MANIFEST_SHA
    assert out.provenance.model_version == "fake-test-model"
    assert "Never shown in the validated live view" in out.disclaimer
    assert out.keypoints[0].score == 3.1  # passed through as a score, not a probability


def test_gate_admitting_another_model_refuses():
    with pytest.raises(PoseGateClosedError, match="admitted model"):
        run_experimental_inference(
            FakeModel(model_id="other"), window=None, status=open_status(), provenance=provenance()
        )


def test_status_without_manifest_hash_refuses():
    status = PoseStatus(enabled=True, model_id="fake-test-model", manifest={"model_id": "fake-test-model"})
    with pytest.raises(PoseGateClosedError, match="manifest hash"):
        run_experimental_inference(FakeModel(), window=None, status=status, provenance=provenance())


@pytest.mark.parametrize(
    "raw",
    [
        {"keypoints": [], "coordinate_frame": "f", "validated_live_view": True},
        {"keypoints": [], "coordinate_frame": "f", "label": "VALIDATED"},
        {"keypoints": [{"name": "head", "x": float("nan")}], "coordinate_frame": "f"},
        {"keypoints": [{"name": "head", "probability": 0.9}], "coordinate_frame": "f"},
        {"keypoints": "not-a-list", "coordinate_frame": "f"},
        {"keypoints": []},
        ["not", "a", "dict"],
    ],
)
def test_model_cannot_relabel_or_emit_malformed_output(raw: Any):
    with pytest.raises(PoseOutputRejected):
        run_experimental_inference(FakeModel(output=raw), window=None, status=open_status(), provenance=provenance())


def test_pose_output_literals_cannot_be_overridden():
    base = dict(
        model_id="m", manifest_sha256=MANIFEST_SHA, coordinate_frame="f", keypoints=[], provenance=provenance()
    )
    PoseOutput(**base)  # defaults are fine
    for bad in ({"label": "VALIDATED"}, {"validated_live_view": True}, {"display_route": "LIVE_VIEW"}, {"disclaimer": "x"}):
        with pytest.raises(ValidationError):
            PoseOutput(**base, **bad)


def test_pose_status_label_is_always_experimental():
    with pytest.raises(ValidationError):
        PoseStatus(label="VALIDATED")  # type: ignore[arg-type]
