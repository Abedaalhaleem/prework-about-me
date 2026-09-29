"""HTTP API: every error answers {"detail": "CODE: text", "code": "CODE"}.

Request-validation errors (422) add ``errors`` (type/loc/msg only) and never
echo the rejected input values back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import roomsense.api.routes_source as routes_source
import roomsense.api.routes_status as routes_status
from roomsense.api.app import error_body, validation_error_body
from roomsense.runtime import AppRuntime
from tests.api_helpers import _no_api_token_in_env, api_client, make_cfg  # noqa: F401  (autouse fixture)

SECRET = "PRIVATE-VALUE-7f3a9c"  # made-up marker: must never come back in an error body


def _assert_uniform(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert isinstance(body["detail"], str) and isinstance(body["code"], str)
    assert body["code"] == code
    assert body["detail"].startswith(f"{code}: ")
    return body


def test_validation_errors_have_code_summary_and_no_input_values(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/simulation",
                   json={"scenario": "demo_walk", "acknowledge_simulated": True, "extra": SECRET})
        body = _assert_uniform(r, 422, "VALIDATION_ERROR")
        assert set(body) == {"detail", "code", "errors"}
        assert "body.extra" in body["detail"]
        assert body["errors"] and all(set(e) == {"type", "loc", "msg"} for e in body["errors"])
        assert body["errors"][0]["loc"] == ["body", "extra"] and body["errors"][0]["type"] == "extra_forbidden"
        assert SECRET not in r.text  # the rejected value is not echoed

        # Nested field paths are named in the summary.
        r = c.post("/api/recordings/start", json={"consent": {"all_participants_consented": True,
                                                              "participant_count": 0, "purpose": SECRET},
                                                  "label": "x"})
        body = _assert_uniform(r, 422, "VALIDATION_ERROR")
        assert "body.consent.participant_count" in body["detail"]
        assert SECRET not in r.text

        # Query parameters and malformed JSON use the same shape.
        body = _assert_uniform(c.get("/api/signal", params={"link_id": "x", "seconds": 0}), 422, "VALIDATION_ERROR")
        assert "query.seconds" in body["detail"]
        r = c.post("/api/source/replay", content="{not json " + SECRET, headers={"Content-Type": "application/json"})
        _assert_uniform(r, 422, "VALIDATION_ERROR")
        assert SECRET not in r.text
        # Only a literal JSON true acknowledges simulated data.
        r = c.post("/api/source/simulation", json={"scenario": "demo_walk", "acknowledge_simulated": "true"})
        body = _assert_uniform(r, 422, "VALIDATION_ERROR")
        assert "body.acknowledge_simulated" in body["detail"]


def test_framework_errors_are_uniform(tmp_path: Path) -> None:
    with api_client(make_cfg(tmp_path), frontend_dist=tmp_path / "no-dist") as c:
        _assert_uniform(c.get("/api/does-not-exist"), 404, "NOT_FOUND")
        r = c.post("/api/status")
        _assert_uniform(r, 405, "METHOD_NOT_ALLOWED")
        assert "GET" in r.headers["allow"]
        # A refused CORS preflight answers JSON too.
        pre = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        r = c.options("/api/source/stop", headers=pre)
        _assert_uniform(r, 400, "CORS_REJECTED")
        assert "access-control-allow-origin" not in r.headers


def test_unknown_api_route_behind_the_spa_fallback_is_uniform(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>UI</title>", encoding="utf-8")
    with api_client(make_cfg(tmp_path), frontend_dist=dist) as c:
        body = _assert_uniform(c.get("/api/nope"), 404, "NOT_FOUND")
        assert body["detail"] == "NOT_FOUND: unknown API route"


def test_route_errors_carry_their_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_source, "list_serial_ports", lambda: [])
    with api_client(make_cfg(tmp_path)) as c:
        r = c.post("/api/source/live", json={"receivers": [{"receiver_id": "rx1", "port": "/dev/sda"}]})
        _assert_uniform(r, 422, "PORT_NOT_AVAILABLE")
        # Refusals from the runtime keep their shape.
        _assert_uniform(c.post("/api/source/live", json={}), 409, "NO_RECEIVERS_CONFIGURED")

    def broken() -> list[dict[str, object]]:
        raise OSError("no backend")

    monkeypatch.setattr(routes_status, "list_serial_ports", broken)
    with api_client(make_cfg(tmp_path)) as c:
        body = _assert_uniform(c.get("/api/serial/ports"), 503, "SERIAL_ENUMERATION_FAILED")
        assert body["detail"] == "SERIAL_ENUMERATION_FAILED: OSError"


def test_not_ready_is_uniform(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    rt = AppRuntime(cfg)
    with api_client(cfg, rt) as c:
        assert c.get("/api/status").status_code == 200
        rt.shutdown()  # the lifespan shutdown later is a no-op
        _assert_uniform(c.get("/api/status"), 503, "NOT_READY")
        _assert_uniform(c.post("/api/source/stop"), 503, "NOT_READY")


def test_error_body_helpers() -> None:
    assert error_body(404, "Not Found") == {"detail": "NOT_FOUND: Not Found", "code": "NOT_FOUND"}
    assert error_body(422, "PORT_NOT_AVAILABLE: /dev/x") == {"detail": "PORT_NOT_AVAILABLE: /dev/x",
                                                              "code": "PORT_NOT_AVAILABLE"}
    assert error_body(413, "too big")["code"] == "REQUEST_ENTITY_TOO_LARGE"
    assert error_body(599, "odd") == {"detail": "HTTP_599: odd", "code": "HTTP_599"}
    body = validation_error_body([
        {"type": "missing", "loc": ("body", "label"), "msg": "Field required", "input": {"x": SECRET}},
        {"type": "greater_than", "loc": ("query", "seconds"), "msg": "Input should be greater than 0",
         "input": SECRET, "ctx": {"gt": 0}},
        {"type": "value_error", "loc": ("body",), "msg": "Value error, bad", "input": SECRET,
         "ctx": {"error": ValueError(SECRET)}},
        {"type": "missing", "loc": ("body", "purpose"), "msg": "Field required", "input": None},
    ])
    assert body["code"] == "VALIDATION_ERROR"
    assert body["detail"] == ("VALIDATION_ERROR: body.label: Field required; query.seconds: Input should be greater "
                              "than 0; body: Value error, bad (+1 more)")
    assert len(body["errors"]) == 4 and all(set(e) == {"type", "loc", "msg"} for e in body["errors"])
    assert SECRET not in json.dumps(body)
