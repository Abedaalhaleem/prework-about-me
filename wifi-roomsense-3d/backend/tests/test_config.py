"""Configuration: the shipped example config and the API-token rules."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from roomsense.api.app import create_app
from roomsense.api.security import StartupRefused, check_bind_allowed
from roomsense.config import (
    API_TOKEN_ENV,
    EXAMPLE_CONFIG_PATH,
    ApiTokenInvalid,
    AppConfig,
    api_token,
    api_token_problem,
    load_config,
)
from tests.api_helpers import TOKEN, _no_api_token_in_env, make_cfg  # noqa: F401  (autouse fixture)

# ---------------------------------------------------------------------------
# configs/roomsense.example.toml
# ---------------------------------------------------------------------------


def test_example_config_loads_and_configures_no_receiver() -> None:
    """The example is copied verbatim by scripts/setup.*: it must load, and its
    sample ports must not look configured (ports are never guessed)."""
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert cfg.acquisition.receivers == []
    assert cfg.server.host == "127.0.0.1" and cfg.server.allow_non_loopback is False
    assert cfg.config_version() == AppConfig().config_version()  # it documents the defaults


def _uncomment_receiver_blocks(text: str) -> str:
    """Remove the leading '# ' from every commented [[acquisition.receivers]] block."""
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.startswith("# [[acquisition.receivers]]"):
            in_block = True
        elif in_block and not re.match(r"^# [a-z_]+ = ", line):
            in_block = False
        out.append(line[2:] if in_block else line)
    return "\n".join(out) + "\n"


def test_example_receiver_blocks_are_valid_once_uncommented() -> None:
    text = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
    assert "NO RECEIVER IS CONFIGURED YET" in text
    cfg = AppConfig.model_validate(tomllib.loads(_uncomment_receiver_blocks(text)))
    rx = {r.receiver_id: r for r in cfg.acquisition.receivers}
    assert set(rx) == {"rx1", "rx_router"}
    assert rx["rx1"].port == "/dev/ttyUSB0" and rx["rx1"].input_format.value == "roomsense-rscsi-v1"
    assert rx["rx_router"].declared_chip == "esp32s3" and rx["rx_router"].ltf_config == "lltf_only"


def test_setup_scripts_copy_the_example_and_check_it() -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sh = (scripts / "setup.sh").read_text(encoding="utf-8")
    ps1 = (scripts / "setup.ps1").read_text(encoding="utf-8")
    assert 'cp "$REPO/configs/roomsense.example.toml" "$TMP_CFG"' in sh
    assert "Copy-Item -LiteralPath (Join-Path $Repo 'configs\\roomsense.example.toml')" in ps1
    for script in (sh, ps1):
        assert "load_config(sys.argv[1]); sys.exit(0 if not c.acquisition.receivers else 1)" in script


# ---------------------------------------------------------------------------
# ROOMSENSE_API_TOKEN
# ---------------------------------------------------------------------------

BAD_TOKENS = ["", "   ", "\t", "xq7Zk9", "0123456789abcde", " 0123456789abcdef", "0123456789abcdef\n",
              "0123456789 abcdef", "0123456789abcdef\x01", "töken-0123456789abcdef"]


@pytest.mark.parametrize("bad", BAD_TOKENS)
def test_unusable_tokens_are_named_without_revealing_them(bad: str) -> None:
    problem = api_token_problem(bad)
    assert problem is not None
    assert bad.strip() == "" or bad.strip() not in problem


def test_usable_tokens() -> None:
    for good in (TOKEN, "0123456789abcdef", "A-b_c.d~e+f!g#h$i%j&k'l*m^n`o|p"):
        assert api_token_problem(good) is None


def test_api_token_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert api_token() is None  # not set at all: no token
    monkeypatch.setenv(API_TOKEN_ENV, TOKEN)
    assert api_token() == TOKEN
    monkeypatch.setenv(API_TOKEN_ENV, "xq7Zk9")
    with pytest.raises(ApiTokenInvalid) as ei:
        api_token()
    assert "xq7Zk9" not in str(ei.value) and API_TOKEN_ENV in str(ei.value)


@pytest.mark.parametrize("bad", BAD_TOKENS)
def test_server_refuses_to_start_with_an_unusable_token_even_on_loopback(tmp_path: Path, bad: str,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, bad)
    cfg = make_cfg(tmp_path)
    assert cfg.server.is_loopback()
    with pytest.raises(StartupRefused, match=API_TOKEN_ENV) as ei:
        create_app(cfg)
    assert bad.strip() == "" or bad.strip() not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    with pytest.raises(StartupRefused, match=API_TOKEN_ENV):
        check_bind_allowed(cfg)
    lan = make_cfg(tmp_path, server={"host": "0.0.0.0", "allow_non_loopback": True})
    with pytest.raises(StartupRefused, match=API_TOKEN_ENV):
        create_app(lan)
