#!/usr/bin/env python3
"""Independent check of the golden protocol lines written by gen_golden.

Uses only the Python standard library (zlib.crc32 is the reference CRC the
protocol is defined against). For every line in golden_rscsi_lines.txt it
checks: the CRC, the '*' + 8 uppercase hex suffix, the field count per tag,
lowercase hex CSI of exactly 2*len characters, and that each field matches the
input values recorded in golden_rscsi_expected.jsonl (NA <-> null).

Usage: verify_golden.py <golden_rscsi_lines.txt> <golden_rscsi_expected.jsonl>
Exit status 0 only if every line passes.
"""

from __future__ import annotations

import json
import re
import sys
import zlib

FIELD_COUNTS = {"RSHELLO": 17, "RSCSI": 31, "RSSTAT": 13}

HELLO_KEYS = [
    "tag", "version", "fw_name", "fw_version", "idf_version", "chip", "chip_revision",
    "sta_mac", "mode", "ltf_config", "channel", "secondary_channel", "tx_mac_filter",
    "rate_hz", "queue_depth", "baud", "build_id",
]
CSI_KEYS = [
    "tag", "version", "rec_seq", "tx_seq", "drops", "src_mac", "rssi", "rate", "sig_mode",
    "mcs", "cwb", "smoothing", "not_sounding", "aggregation", "stbc", "fec_coding", "sgi",
    "noise_floor", "ampdu_cnt", "channel", "secondary_channel", "rx_timestamp_us", "ant",
    "sig_len", "rx_state", "bb_format", "agc_gain", "fft_gain", "first_word_invalid", "len",
    "csi",
]
STAT_KEYS = [
    "tag", "version", "uptime_ms", "cb_total", "cb_filtered", "enqueued",
    "dropped_queue_full", "dropped_oversize", "printed", "tx_ok", "tx_fail", "free_heap",
    "min_free_heap",
]
KEYS = {"RSHELLO": HELLO_KEYS, "RSCSI": CSI_KEYS, "RSSTAT": STAT_KEYS}


def _as_expected(field: str, key: str) -> object:
    if field == "NA":
        return None
    if key in ("tag", "fw_name", "fw_version", "idf_version", "chip", "chip_revision", "mode",
               "ltf_config", "build_id", "sta_mac", "tx_mac_filter", "src_mac"):
        return field
    if key == "csi":
        raw = bytes.fromhex(field)
        return [b - 256 if b >= 128 else b for b in raw]
    if not re.fullmatch(r"-?[0-9]+", field):
        raise ValueError(f"{key}: not an integer: {field!r}")
    return int(field)


def check(lines_path: str, expected_path: str) -> int:
    with open(lines_path, "rb") as fh:
        raw_lines = fh.read().split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    with open(expected_path, encoding="ascii") as fh:
        expected = [json.loads(line) for line in fh if line.strip()]
    failures = 0
    if len(raw_lines) != len(expected):
        print(f"FAIL: {len(raw_lines)} lines but {len(expected)} expected records")
        return 1
    for idx, (raw, exp) in enumerate(zip(raw_lines, expected)):
        text = raw.decode("ascii")
        star = text.rfind("*")
        body, crc_hex = text[:star], text[star + 1:]
        problems: list[str] = []
        if star < 0 or not re.fullmatch(r"[0-9A-F]{8}", crc_hex):
            problems.append("missing or malformed *CRC suffix")
        elif int(crc_hex, 16) != zlib.crc32(body.encode("ascii")):
            problems.append(f"CRC mismatch: line {crc_hex}, zlib {zlib.crc32(body.encode()):08X}")
        fields = body.split(",")
        tag = fields[0]
        if tag not in FIELD_COUNTS:
            problems.append(f"unknown tag {tag!r}")
        elif len(fields) != FIELD_COUNTS[tag]:
            problems.append(f"{tag}: {len(fields)} fields, expected {FIELD_COUNTS[tag]}")
        else:
            if tag == "RSCSI":
                n = int(fields[29])
                if len(fields[30]) != 2 * n or not re.fullmatch(r"[0-9a-f]*", fields[30]):
                    problems.append("csi_hex is not 2*len lowercase hex characters")
            for key, field in zip(KEYS[tag], fields):
                want = exp.get(key)
                try:
                    got = _as_expected(field, key)
                except ValueError as err:
                    problems.append(str(err))
                    continue
                if got != want:
                    problems.append(f"{key}: line has {got!r}, expected {want!r}")
        if body != exp["line"][: exp["line"].rfind("*")]:
            problems.append("line differs from the 'line' recorded in the expected file")
        status = "ok  " if not problems else "FAIL"
        print(f"{status} {idx:2d} {exp['name']:40s} {tag:8s} crc={crc_hex} bytes={len(raw) + 1}")
        for p in problems:
            print(f"       - {p}")
        failures += bool(problems)
    print(f"verify_golden: {len(raw_lines) - failures}/{len(raw_lines)} lines passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(sys.argv[1], sys.argv[2]))
