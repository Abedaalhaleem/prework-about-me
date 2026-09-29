"""Parser and FrameBuilder tests.

Inputs are the upstream esp-csi documentation fixtures (not measured by this
project) and hand-built lines. The tests check parsing and bookkeeping only;
they say nothing about sensing accuracy.
"""

from __future__ import annotations

import zlib

import pytest

from roomsense.acquisition.parser import (
    CsiLineRecord,
    DiagnosticLine,
    FrameBuilder,
    HelloRecord,
    ParseError,
    StatRecord,
    parse_line,
)
from roomsense.schemas import InputFormat, SourceMode
from tests.acq_helpers import (
    C5C6_FIXTURE,
    CLASSIC_FIXTURE,
    CLASSIC_HEADER_FIXTURE,
    GOLDEN_RSCSI,
    OTHER_MAC,
    STA_MAC,
    TX_MAC,
    c5c6_line,
    classic_line,
    classic_receiver,
    default_values,
    fixture_lines,
    hello_line,
    int8_hex,
    rs_receiver,
    rscsi_body,
    rscsi_line,
    stat_line,
    with_crc,
)

CLASSIC = InputFormat.UPSTREAM_CLASSIC_V1
C5C6 = InputFormat.UPSTREAM_C5C6_V1
RS = InputFormat.ROOMSENSE_RSCSI_V1
LIM = {"max_line_bytes": 8192, "max_csi_values": 612}
SEC_BELOW_LAYOUT = "classic.lltf_only.sec_below.total128.LLTF64"
SEC_NONE_LAYOUT = "classic.lltf_only.sec_none.total128.LLTF64"


def parse(line, fmt=CLASSIC, **kw):
    args = dict(LIM)
    args.update(kw)
    return parse_line(line, fmt, **args)


def code_of(line, fmt=CLASSIC, **kw) -> str:
    with pytest.raises(ParseError) as ei:
        parse(line, fmt, **kw)
    return ei.value.code


def builder(rx, **kw) -> FrameBuilder:
    return FrameBuilder(rx, session_id="test-session", source_mode=SourceMode.LIVE, **kw)


def build(b: FrameBuilder, line: str, fmt=CLASSIC):
    rec = parse(line, fmt)
    assert isinstance(rec, CsiLineRecord)
    return b.build(rec, host_monotonic_ns=1_000, host_unix_ns=2_000, raw_line=line)


# ---------------------------------------------------------------------------
# Upstream fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chip", ["esp32", "esp32s3"])
def test_all_classic_fixture_lines_parse_into_frames(chip):
    lines = fixture_lines(CLASSIC_FIXTURE)
    assert len(lines) == 14
    b = builder(classic_receiver(chip=chip))
    frames = [build(b, ln) for ln in lines]
    for i, f in enumerate(frames):
        assert f.layout_id == SEC_BELOW_LAYOUT
        # sec_below: k = -32..31; valid are +-1..+-26. The first-word
        # positions (k=-32, -31) are guard bands, so all 52 stay valid.
        assert f.first_word_invalid is True
        assert "FIRST_WORD_INVALID" in f.quality_flags
        assert len(f.valid_subcarriers) == 52
        assert -32 not in f.valid_subcarriers and -31 not in f.valid_subcarriers
        assert f.csi_len == 128 and len(f.raw_csi) == 128
        assert f.transmitter_mac == "94:d9:b3:80:8c:81"
        assert f.channel == 13 and f.secondary_channel == 2
        assert f.bandwidth_mhz == 40  # cwb column is 1
        assert f.rx_state is None  # the upstream rx_state column repeats sig_mode
        assert f.noise_floor_dbm == -93
        assert f.frame_counter == i
        assert f.source_mode == SourceMode.LIVE
        assert f.link_id == "tx1->rx1"
    assert frames[0].raw_csi[:4] == (67, 48, 4, 0)
    # The README lines are a contiguous run of ids 0..13.
    assert all("COUNTER_GAP" not in f.quality_flags for f in frames)


def test_gain_compensation_depends_on_declared_chip():
    ln = fixture_lines(CLASSIC_FIXTURE)[1]
    s3 = build(builder(classic_receiver(chip="esp32s3")), ln)
    assert s3.values_are_gain_compensated is True
    assert "GAIN_COMPENSATED_UPSTREAM" in s3.quality_flags
    e32 = build(builder(classic_receiver(chip="esp32")), ln)
    assert e32.values_are_gain_compensated is False
    assert "GAIN_COMPENSATED_UPSTREAM" not in e32.quality_flags
    unknown = build(builder(classic_receiver(chip=None)), ln)
    assert unknown.values_are_gain_compensated is None
    assert unknown.layout_id is None and "UNKNOWN_LAYOUT" in unknown.quality_flags
    assert unknown.device.identity_source == "unavailable" and unknown.device.chip is None


def test_classic_header_line_is_diagnostic():
    rec = parse(CLASSIC_HEADER_FIXTURE.read_text().strip())
    assert isinstance(rec, DiagnosticLine)
    assert rec.text.startswith("type,")


def test_c5c6_fixture_parses_but_layout_is_unknown():
    line = fixture_lines(C5C6_FIXTURE)[0]
    rec = parse(line, C5C6)
    assert isinstance(rec, CsiLineRecord)
    assert rec.csi_len == 256 and len(rec.values) == 256
    assert rec.sig_mode is None and rec.cwb is None and rec.secondary_channel is None and rec.stbc is None
    assert rec.rx_state is None and rec.bb_format == 0
    assert rec.noise_floor == -96 and rec.fft_gain == 32 and rec.agc_gain == 4
    for chip, ltf in [("esp32c6", None), ("esp32c5", "c5_default")]:
        rx = classic_receiver(chip=chip, input_format=C5C6, ltf_config=ltf)
        f = builder(rx).build(rec, host_monotonic_ns=1, host_unix_ns=2)
        assert f.layout_id is None and f.valid_subcarriers is None
        assert "UNKNOWN_LAYOUT" in f.quality_flags
        assert f.values_are_gain_compensated is True
        assert f.bandwidth_mhz is None


def test_c6_documented_length_rejected_without_assumption_and_flagged_with_it():
    line = c5c6_line(default_values(106))
    plain = build(builder(classic_receiver(chip="esp32c6", input_format=C5C6, ltf_config="c5_default")), line, C5C6)
    assert plain.layout_id is None and "UNKNOWN_LAYOUT" in plain.quality_flags
    rx = classic_receiver(chip="esp32c6", input_format=C5C6, ltf_config="c5_default",
                          allow_undocumented_layout_assumption=True)
    assumed = build(builder(rx), line, C5C6)
    assert assumed.layout_id is not None and assumed.layout_id.endswith(".assumed")
    assert "UNDOCUMENTED_LAYOUT_ASSUMPTION" in assumed.quality_flags
    c5 = build(builder(classic_receiver(chip="esp32c5", input_format=C5C6, ltf_config="c5_default")), line, C5C6)
    assert c5.layout_id == "c5.c5_default.total106.LLTF53"
    assert "UNDOCUMENTED_LAYOUT_ASSUMPTION" not in c5.quality_flags


# ---------------------------------------------------------------------------
# Malformed upstream lines
# ---------------------------------------------------------------------------


def test_valid_hand_built_classic_line_parses():
    rec = parse(classic_line())
    assert isinstance(rec, CsiLineRecord)
    assert rec.values == tuple(default_values())


@pytest.mark.parametrize(
    "line,code",
    [
        (classic_line().replace(",67,1,128,", ",67,128,", 1), "FIELD_COUNT"),  # rx_state column dropped: 24
        (classic_line() + ",1", "FIELD_COUNT"),  # 26 columns
        (classic_line(rssi="abc"), "BAD_INT"),
        (classic_line(rssi="-4.5"), "BAD_INT"),
        (classic_line(rssi="+4"), "BAD_INT"),
        (classic_line(rssi=" -40"), "BAD_INT"),
        (classic_line(rssi=200), "INT_RANGE"),
        (classic_line(channel=300), "INT_RANGE"),
        (classic_line(mac="94:d9:b3:80:8c"), "BAD_MAC"),
        (classic_line(mac="zz:d9:b3:80:8c:81"), "BAD_MAC"),
        (classic_line(values=[1, 2, 3], len=3, data='"[1, 2, 3]"'), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1, 2, 3], len=3, data='"[1,2.5,3]"'), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1], len=1, data="\"[__import__('os').system('x')]\""), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1, 2], len=2, data='"[1,2,]"'), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1, 2], len=2, data='"(1,2)"'), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1, 2], len=2, data='"[0x10,2]"'), "CSI_LIST_SYNTAX"),
        (classic_line(values=[1, 2], len=2, data='"[1,2]"x'), "CSV_SYNTAX"),
        (classic_line(values=[1, 2, 3], len=4), "CSI_LEN_MISMATCH"),
        (classic_line(values=[1, 2, 3], len=2), "CSI_LEN_MISMATCH"),
        (classic_line(values=[1, 2], len=0, data='"[]"'), "EMPTY_CSI"),
        (classic_line(values=[40000, 1], len=2), "INT_RANGE"),
        (classic_line(len=-1), "INT_RANGE"),
        (classic_line(first_word=2), "INT_RANGE"),
    ],
)
def test_malformed_classic_lines(line, code):
    assert code_of(line) == code


def test_eval_injection_in_a_numeric_field_is_rejected():
    assert code_of(classic_line(rssi="__import__('os').system('x')")) in {"BAD_INT", "FIELD_COUNT", "CSV_SYNTAX"}


def test_unicode_is_rejected_as_not_ascii():
    assert code_of(classic_line(mac="94:d9:b3:80:8c:8١")) == "NOT_ASCII"
    assert code_of(classic_line().encode("ascii") + b"\xff") == "NOT_ASCII"
    assert code_of("I (12) wifi: café") == "NOT_ASCII"


def test_control_characters_in_a_record_are_rejected():
    assert code_of(classic_line(rssi="-4\x000")) == "NOT_ASCII"


def test_huge_line_rejected_before_parsing():
    big = "CSI_DATA," + "9" * 10_000
    assert code_of(big) == "LINE_TOO_LONG"
    assert code_of(big.encode("ascii")) == "LINE_TOO_LONG"
    # A non-ASCII huge line is still reported as too long (length check first).
    assert code_of("é" * 9000) == "LINE_TOO_LONG"


def test_line_length_limit_excludes_the_terminator():
    line = classic_line()
    assert isinstance(parse(line + "\r\n", max_line_bytes=len(line)), CsiLineRecord)
    assert code_of(line, max_line_bytes=len(line) - 1) == "LINE_TOO_LONG"


def test_too_many_values_checked_against_max_before_conversion():
    vals = [1] * 700
    assert code_of(classic_line(vals)) == "TOO_MANY_VALUES"
    # Declared len above the limit is rejected even if the list is short.
    assert code_of(classic_line([1, 2], len=1000)) == "TOO_MANY_VALUES"
    # Too many values with a len column that claims fewer: still TOO_MANY_VALUES.
    assert code_of(classic_line(vals, len=128)) == "TOO_MANY_VALUES"


def test_values_are_never_padded_or_truncated():
    vals = default_values(100)
    rec = parse(classic_line(vals))
    assert isinstance(rec, CsiLineRecord)
    assert rec.values == tuple(vals)
    f = builder(classic_receiver()).build(rec, host_monotonic_ns=1, host_unix_ns=1)
    assert f.raw_csi == tuple(vals) and f.layout_id is None


def test_trailing_crlf_is_stripped():
    rec = parse(classic_line() + "\r\n")
    assert isinstance(rec, CsiLineRecord)


def test_c5c6_wrong_column_count():
    assert code_of(classic_line(), C5C6) == "FIELD_COUNT"
    assert code_of(c5c6_line(), CLASSIC) == "FIELD_COUNT"


def test_diagnostic_lines_are_sanitised_and_hint_at_misconfiguration():
    rec = parse("\x1b[0;32mI (345) wifi:connected\x1b[0m\x07")
    assert isinstance(rec, DiagnosticLine)
    assert rec.text == "I (345) wifi:connected?"
    hinted = parse(rscsi_line(), CLASSIC)
    assert isinstance(hinted, DiagnosticLine) and hinted.looks_like == RS.value
    hinted2 = parse(classic_line(), RS)
    assert isinstance(hinted2, DiagnosticLine) and hinted2.looks_like == "esp-csi-upstream"


def test_synthetic_format_is_not_a_line_format():
    assert code_of("CSI_DATA,1", InputFormat.SYNTHETIC_V1) == "UNSUPPORTED_FORMAT"


# ---------------------------------------------------------------------------
# RoomSense RSCSI / RSHELLO / RSSTAT
# ---------------------------------------------------------------------------


def test_rscsi_round_trip_with_crc_built_in_the_test():
    vals = [-128, -1, 0, 1, 127] + default_values(123)
    body = rscsi_body(vals, rec_seq=4294967295, tx_seq=17, drops=3, rx_timestamp_us=4294967000)
    line = f"{body}*{zlib.crc32(body.encode('ascii')) & 0xFFFFFFFF:08X}"
    rec = parse(line, RS)
    assert isinstance(rec, CsiLineRecord)
    assert rec.values == tuple(vals)
    assert rec.seq == 4294967295 and rec.tx_seq == 17 and rec.drops == 3
    assert rec.timestamp_us == 4294967000
    assert rec.mac == TX_MAC and rec.rssi == -42 and rec.channel == 6
    assert rec.bb_format is None and rec.agc_gain is None and rec.fft_gain is None  # NA -> None
    assert rec.first_word_invalid is False and rec.rx_state == 0


def test_rscsi_crc_and_version_errors():
    good = rscsi_line()
    body, crc = good.rsplit("*", 1)
    bad_crc = f"{body}*{(int(crc, 16) ^ 1):08X}"
    assert code_of(bad_crc, RS) == "CRC_MISMATCH"
    assert code_of(body, RS) == "MISSING_CRC"
    assert code_of(f"{body}*{crc.lower()}" if crc.upper() != crc.lower() else f"{body}*ZZZZZZZZ", RS) == "MISSING_CRC"
    assert code_of(f"{body}*{crc[:6]}", RS) == "MISSING_CRC"
    # Interleaved/truncated line: CRC no longer matches.
    assert code_of(good[:40] + good[60:], RS) == "CRC_MISMATCH"
    assert code_of(with_crc(rscsi_body(version=2)), RS) == "UNSUPPORTED_VERSION"
    assert code_of(with_crc(rscsi_body() + ",extra"), RS) == "FIELD_COUNT"


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"csi_hex": int8_hex(default_values()).upper()}, "BAD_HEX"),
        ({"csi_hex": "zz" * 128}, "BAD_HEX"),
        ({"len": 127}, "CSI_LEN_MISMATCH"),
        ({"len": 0, "csi_hex": ""}, "EMPTY_CSI"),
        ({"len": 700, "csi_hex": "00" * 700}, "TOO_MANY_VALUES"),
        ({"src_mac": "1a-00-00-00-00-01"}, "BAD_MAC"),
        ({"rec_seq": "NA"}, "BAD_INT"),
        ({"rec_seq": 4294967296}, "INT_RANGE"),
        ({"rssi": "1e3"}, "BAD_INT"),
        ({"sig_mode": 4}, "INT_RANGE"),
    ],
)
def test_rscsi_field_errors(overrides, code):
    assert code_of(with_crc(rscsi_body(**overrides)), RS) == code


def test_rscsi_uppercase_mac_is_normalised():
    rec = parse(rscsi_line(src_mac=TX_MAC.upper()), RS)
    assert isinstance(rec, CsiLineRecord) and rec.mac == TX_MAC


def test_rshello_and_rsstat_parse():
    h = parse(hello_line(build_id="NA", channel="NA"), RS)
    assert isinstance(h, HelloRecord)
    assert h.chip == "esp32s3" and h.sta_mac == STA_MAC and h.ltf_config == "lltf_only"
    assert h.build_id is None and h.channel is None and h.tx_mac_filter == TX_MAC
    s = parse(stat_line(), RS)
    assert isinstance(s, StatRecord)
    assert s.dropped_queue_full == 2 and s.tx_ok is None and s.uptime_ms == 123456
    assert code_of(hello_line(fw_name="bad name!"), RS) == "BAD_TEXT"
    assert code_of(stat_line(free_heap=-1), RS) == "INT_RANGE"


def test_golden_rscsi_lines_from_firmware_formatter():
    if not GOLDEN_RSCSI.exists():
        pytest.skip(
            f"{GOLDEN_RSCSI} not present yet (produced from the firmware formatter by the firmware work "
            "package); nothing to check"
        )
    lines = [ln for ln in GOLDEN_RSCSI.read_text(encoding="ascii").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines, "golden file is empty"
    records = [parse(ln, RS) for ln in lines]
    csi = [r for r in records if isinstance(r, CsiLineRecord)]
    assert csi, "golden file contains no RSCSI records"
    for r in records:
        assert not isinstance(r, DiagnosticLine), f"golden line parsed as diagnostic: {r}"


# Protocol field name (as in docs/SERIAL_PROTOCOL.md) -> CsiLineRecord attribute.
_RSCSI_ATTR = {"rec_seq": "seq", "src_mac": "mac", "rx_timestamp_us": "timestamp_us", "len": "csi_len", "csi": "values"}
_META_KEYS = {"name", "synthetic", "line", "note", "tag"}


def test_golden_lines_match_firmware_formatter_expectations_field_by_field():
    expected_path = GOLDEN_RSCSI.with_name("golden_rscsi_expected.jsonl")
    if not expected_path.exists():
        pytest.skip(f"{expected_path} not present; produced by the firmware work package")
    import json

    cases = [json.loads(ln) for ln in expected_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert cases
    for case in cases:
        rec = parse(case["line"], RS)
        expected_type = {"RSCSI": CsiLineRecord, "RSHELLO": HelloRecord, "RSSTAT": StatRecord}[case["tag"]]
        assert isinstance(rec, expected_type), case["name"]
        for key, want in case.items():
            if key in _META_KEYS:
                continue
            if case["tag"] == "RSCSI" and key == "version":
                assert want == 1  # CsiLineRecord is format-neutral; the parser only accepts v1
                continue
            attr = _RSCSI_ATTR.get(key, key) if case["tag"] == "RSCSI" else key
            got = getattr(rec, attr)
            if attr == "values":
                got, want = list(got), list(want)
            elif attr == "first_word_invalid" and want is not None:
                want = bool(want)
            assert got == want, f"{case['name']}: {key} expected {want!r}, parsed {got!r}"


def test_golden_lines_build_frames_across_counter_wrap():
    if not GOLDEN_RSCSI.exists():
        pytest.skip(f"{GOLDEN_RSCSI} not present")
    lines = [ln for ln in GOLDEN_RSCSI.read_text(encoding="ascii").splitlines() if ln.strip() and not ln.startswith("#")]
    by_seq = {}
    for ln in lines:
        rec = parse(ln, RS)
        if isinstance(rec, CsiLineRecord) and rec.mac == "02:11:22:33:44:55":
            by_seq[rec.seq] = ln
    if not {4294967294, 4294967295, 0} <= set(by_seq):
        pytest.skip("golden file no longer contains the rec_seq wrap sequence")
    b = builder(rs_receiver(declared_chip="esp32", ltf_config="lltf_only", transmitter_mac="02:11:22:33:44:55"))
    frames = [build(b, by_seq[s], RS) for s in (4294967294, 4294967295, 0)]
    assert [f.frame_counter_unwrapped for f in frames] == [4294967294, 4294967295, 4294967296]
    assert "COUNTER_ROLLOVER" in frames[2].quality_flags
    assert "COUNTER_RESET" not in frames[2].quality_flags
    assert "TIMESTAMP_ROLLOVER" in frames[1].quality_flags
    # No RSHELLO fed to this builder: RSCSI layouts come only from firmware hello.
    assert all("UNKNOWN_LAYOUT" in f.quality_flags for f in frames)


# ---------------------------------------------------------------------------
# FrameBuilder
# ---------------------------------------------------------------------------


def test_rscsi_frame_needs_hello_for_layout_then_uses_firmware_identity():
    b = builder(rs_receiver(declared_chip="esp32s3"))
    before = build(b, rscsi_line(), RS)
    assert before.layout_id is None and "UNKNOWN_LAYOUT" in before.quality_flags
    assert before.device.identity_source == "user_config" and before.device.chip == "esp32s3"
    assert before.values_are_gain_compensated is False
    hello = parse(hello_line(), RS)
    assert b.on_hello(hello) == []
    after = build(b, rscsi_line(rec_seq=101), RS)
    assert after.layout_id == SEC_NONE_LAYOUT
    assert after.device.identity_source == "firmware_hello"
    assert after.device.firmware_name == "roomsense_csi_rx" and after.device.station_mac == STA_MAC
    assert after.transmitter_counter == 5000 and after.frame_counter == 101
    assert len(after.valid_subcarriers) == 52
    assert after.bandwidth_mhz == 20 and after.rx_state == 0
    assert after.quality_flags == ()


def test_first_word_invalid_removes_the_first_two_positions():
    b = builder(rs_receiver())
    b.on_hello(parse(hello_line(), RS))
    f = build(b, rscsi_line(first_word_invalid=1), RS)
    # sec_none: buffer positions 0,1 are k=0 (DC, never valid) and k=1.
    assert f.first_word_invalid is True
    assert 1 not in f.valid_subcarriers and len(f.valid_subcarriers) == 51
    assert "FIRST_WORD_INVALID" in f.quality_flags


def test_hello_change_notices():
    b = builder(rs_receiver(declared_chip="esp32", transmitter_mac="1a:00:00:00:00:02", ltf_config="lltf_htltf_stbc"))
    notices = b.on_hello(parse(hello_line(), RS))
    assert set(notices) == {"DECLARED_CHIP_MISMATCH", "TX_MAC_FILTER_MISMATCH", "LTF_CONFIG_MISMATCH"}
    notices = b.on_hello(parse(hello_line(channel=11, fw_version="0.2.0"), RS))
    assert "IDENTITY_CHANGED" in notices
    assert set(b.last_identity_changes) == {"channel", "fw_version"}
    assert b.on_hello(parse(hello_line(channel=11, fw_version="0.2.0"), RS)).count("IDENTITY_CHANGED") == 0


def test_transmitter_mac_filter():
    b = builder(classic_receiver(transmitter_mac=TX_MAC.upper()))
    assert build(b, classic_line()).transmitter_mac == TX_MAC
    with pytest.raises(ParseError) as ei:
        build(b, classic_line(mac=OTHER_MAC))
    assert ei.value.code == "MAC_NOT_CONFIGURED"
    # Without a configured MAC the firmware's hello filter is used.
    b2 = builder(rs_receiver())
    b2.on_hello(parse(hello_line(), RS))
    with pytest.raises(ParseError):
        build(b2, rscsi_line(src_mac=OTHER_MAC), RS)


def test_quality_flags_from_values_and_metadata():
    b = builder(rs_receiver())
    b.on_hello(parse(hello_line(), RS))
    zero = build(b, rscsi_line([0] * 128, rec_seq=1, drops=0), RS)
    assert "ALL_ZERO_CSI" in zero.quality_flags
    sat = build(b, rscsi_line([127] + [1] * 127, rec_seq=2, drops=0), RS)
    assert "SATURATED_VALUES" in sat.quality_flags
    drops = build(b, rscsi_line(rec_seq=5, drops=4, rx_state=3), RS)
    assert {"FIRMWARE_QUEUE_DROPS", "RX_STATE_ERROR", "COUNTER_GAP"} <= set(drops.quality_flags)
    assert drops.firmware_drop_count == 4
    no_ts = build(b, rscsi_line(rec_seq=6, drops=4, rx_timestamp_us="NA"), RS)
    assert "DEVICE_TIMESTAMP_UNAVAILABLE" in no_ts.quality_flags and no_ts.device_timestamp_us is None
    no_host = b.build(parse(rscsi_line(rec_seq=7, drops=4), RS), host_monotonic_ns=None, host_unix_ns=None)
    assert "HOST_TIMESTAMP_UNAVAILABLE" in no_host.quality_flags


def test_gain_compensated_int16_values_are_not_flagged_saturated():
    f = build(builder(classic_receiver(chip="esp32s3")), classic_line([127, -128] + [300] * 126))
    assert "SATURATED_VALUES" not in f.quality_flags
    g = build(builder(classic_receiver(chip="esp32")), classic_line([127] + [3] * 127))
    assert "SATURATED_VALUES" in g.quality_flags


def test_counter_and_timestamp_unwrapping_in_builder():
    b = builder(classic_receiver())
    f1 = build(b, classic_line(id=-2, local_timestamp=-100))  # %d of large unsigned values
    f2 = build(b, classic_line(id=-1, local_timestamp=-50))
    f3 = build(b, classic_line(id=0, local_timestamp=10))
    assert f1.frame_counter == 2**32 - 2
    assert f3.frame_counter_unwrapped == 2**32
    assert "COUNTER_ROLLOVER" in f3.quality_flags and "TIMESTAMP_ROLLOVER" in f3.quality_flags
    assert f3.device_timestamp_unwrapped_us == 2**32 + 10
    f4 = build(b, classic_line(id=0, local_timestamp=20))  # reboot: counter restarts
    assert "COUNTER_RESET" not in f2.quality_flags
    assert f4.frame_counter_unwrapped == f3.frame_counter_unwrapped  # duplicate id does not advance


def test_raw_line_handling():
    ln = classic_line()
    kept = build(builder(classic_receiver(), max_raw_line_chars=50), ln)
    assert kept.raw_line == ln[:50]
    dropped = build(builder(classic_receiver(), keep_raw_lines=False), ln)
    assert dropped.raw_line is None


def test_frame_round_trips_through_record():
    f = build(builder(classic_receiver()), fixture_lines(CLASSIC_FIXTURE)[0])
    from roomsense.schemas import CsiFrame

    assert CsiFrame.from_record(f.to_record()) == f


def test_rejected_mac_lines_do_not_advance_counters():
    b = builder(classic_receiver(transmitter_mac=TX_MAC))
    build(b, classic_line(id=10))
    with pytest.raises(ParseError):
        build(b, classic_line(id=500, mac=OTHER_MAC))
    f = build(b, classic_line(id=11))
    assert f.frame_counter_unwrapped == 11 and "COUNTER_GAP" not in f.quality_flags
