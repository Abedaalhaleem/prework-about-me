"""'My Wi-Fi signal' (RSSI of this computer's own Wi-Fi): parsers, monitor and API.

The OS outputs below are hand-written in the documented formats; no real
Wi-Fi interface exists in the test environment. These tests check parsing,
gaps and privacy, not radio behaviour.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.api_helpers import _no_api_token_in_env, api_client, make_cfg  # noqa: F401 - autouse fixture
from roomsense.hostwifi import (
    NOTE,
    HostWifiMonitor,
    WifiSample,
    parse_netsh,
    parse_proc_net_wireless,
    parse_system_profiler,
)

SYSTEM_PROFILER = """Wi-Fi:

      Software Versions:
          CoreWLAN: 16.0 (1657)
      Interfaces:
        en0:
          Card Type: Wi-Fi  (0x14E4, 0x4387)
          Status: Connected
          Current Network Information:
            HomeNet-Secret:
              PHY Mode: 802.11ax
              Channel: 36 (5GHz, 80MHz)
              Country Code: US
              Network Type: Infrastructure
              Security: WPA2 Personal
              Signal / Noise: -56 dBm / -94 dBm
              Transmit Rate: 864
              MCS Index: 9
          Other Local Wi-Fi Networks:
            Neighbour:
              Signal / Noise: -80 dBm / -95 dBm
"""

PROC_NET_WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0
"""

NETSH = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    State                  : connected
    SSID                   : HomeNet-Secret
    Radio type             : 802.11ax
    Channel                : 6
    Receive rate (Mbps)    : 286.8
    Transmit rate (Mbps)   : 286.8
    Signal                 : 85%
"""


def test_system_profiler_uses_only_the_current_network() -> None:
    s = parse_system_profiler(SYSTEM_PROFILER, 1)
    assert (s.rssi_dbm, s.noise_dbm, s.tx_rate_mbps, s.channel, s.connected) == (-56.0, -94.0, 864.0, 36, True)


def test_system_profiler_without_a_current_network_is_not_connected() -> None:
    s = parse_system_profiler("Wi-Fi:\n  Status: Off\n", 1)
    assert s.connected is False and s.rssi_dbm is None


def test_proc_net_wireless_level_and_unavailable_noise() -> None:
    s = parse_proc_net_wireless(PROC_NET_WIRELESS, 1)
    assert s.rssi_dbm == -56.0 and s.noise_dbm is None  # -256 means "not reported"


def test_netsh_reports_percent_not_dbm() -> None:
    s = parse_netsh(NETSH, 1)
    assert s.signal_percent == 85.0 and s.rssi_dbm is None and s.channel == 6


class FakeReader:
    name = "fake"
    interval_s = 0.2

    def __init__(self, fail_every: int = 0) -> None:
        self.n = 0
        self.fail_every = fail_every

    def read(self) -> WifiSample:
        self.n += 1
        if self.fail_every and self.n % self.fail_every == 0:
            raise OSError("read failed")
        return WifiSample(t_unix_ns=time.time_ns(), rssi_dbm=-50.0 - self.n % 3, noise_dbm=-92.0)


def _wait_for(mon: HostWifiMonitor, n: int, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = mon.snapshot()
        if len(snap["series"]["t"]) >= n:
            return snap
        time.sleep(0.05)
    raise AssertionError("monitor produced too few samples")


def test_monitor_samples_and_failed_reads_are_gaps_not_values() -> None:
    mon = HostWifiMonitor(reader_factory=lambda: (FakeReader(fail_every=3), ""))
    try:
        mon.start()
        snap = _wait_for(mon, 6)
    finally:
        mon.stop()
    assert snap["available"] and snap["method"] == "fake" and snap["note"] == NOTE
    rssi = snap["series"]["rssi_dbm"]
    assert None in rssi  # every third read failed -> a gap
    assert all(v is None or -53.0 <= v <= -50.0 for v in rssi)
    assert snap["errors"] >= 1 and "OSError" in snap["last_error"]
    assert not mon.running


def test_availability_is_unknown_before_the_first_start() -> None:
    mon = HostWifiMonitor(reader_factory=lambda: (None, "no Wi-Fi interface"))
    snap = mon.snapshot()
    assert snap["available"] is None and snap["reason"] is None and not snap["running"]


def test_unavailable_reader_reports_why_and_does_not_start() -> None:
    mon = HostWifiMonitor(reader_factory=lambda: (None, "no Wi-Fi interface"))
    snap = mon.start()
    assert snap["available"] is False and snap["reason"] == "no Wi-Fi interface" and not snap["running"]
    assert snap["series"]["t"] == []


def test_api_never_returns_network_names(tmp_path) -> None:
    with api_client(make_cfg(tmp_path)) as client:
        assert isinstance(client, TestClient)
        client.app.state.host_wifi = HostWifiMonitor(reader_factory=lambda: (FakeReader(), ""))
        r = client.post("/api/host-wifi/start")
        assert r.status_code == 200 and r.json()["running"] is True
        time.sleep(0.6)
        body = client.get("/api/host-wifi?seconds=60").json()
        client.post("/api/host-wifi/stop")
    text = str(body)
    assert "HomeNet" not in text and "ssid" not in text.lower() and "bssid" not in text.lower()
    assert body["series"]["t"] and body["latest"]["rssi_dbm"] is not None
    assert "not CSI" in body["note"]


class _Seq:
    def __init__(self, name: str, interval: float, samples: list[WifiSample]) -> None:
        self.name, self.interval_s, self._samples, self.calls = name, interval, samples, 0

    def read(self) -> WifiSample:
        self.calls += 1
        return self._samples[min(self.calls - 1, len(self._samples) - 1)]


def test_mac_reader_switches_to_system_profiler_when_corewlan_reports_nothing() -> None:
    from roomsense.hostwifi import MacReader

    empty = WifiSample(t_unix_ns=1, connected=False)
    good = WifiSample(t_unix_ns=2, rssi_dbm=-60.0, noise_dbm=-90.0)
    fast = _Seq("fast", 0.5, [empty])
    slow = _Seq("slow", 3.0, [good])
    r = MacReader(fast, slow)
    assert [r.read().connected for _ in range(2)] == [False, False]
    assert r.read() == good and r.name == "slow" and r.interval_s == 3.0
    assert r.read() == good and fast.calls == 3


def test_mac_reader_keeps_corewlan_when_neither_has_a_signal() -> None:
    from roomsense.hostwifi import MacReader

    empty = WifiSample(t_unix_ns=1, connected=False)
    r = MacReader(_Seq("fast", 0.5, [empty]), _Seq("slow", 3.0, [empty]))
    for _ in range(5):
        s = r.read()
        assert s.connected is False and s.rssi_dbm is None  # never a made-up value
    assert r.name == "fast"
