"""Strict, bounded parsers for the serial line formats, plus frame building.

Supported input formats (``roomsense.schemas.InputFormat``):

* ``esp-csi-upstream-classic-v1``: 25 CSV columns printed by
  espressif/esp-csi ``examples/get-started`` on ESP32/S2/S3/C3.
* ``esp-csi-upstream-c5c6-v1``: 15 CSV columns printed by the same examples
  on ESP32-C5/C6/C61.
* ``roomsense-rscsi-v1``: this project's CRC-protected records, see
  ``docs/SERIAL_PROTOCOL.md``.

Safety rules applied to every line (serial input is untrusted):

* The length is checked before any other work, and nothing is ever passed to
  ``eval``/``exec``/``pickle``. Integers must match a strict decimal pattern,
  and every numeric field is range-checked against its C bit-field width.
* CSI value lists are validated with an anchored regex; their size is bounded
  before any conversion. Values are never padded or truncated: a count that
  does not match the ``len`` column is rejected.
* Missing metadata stays ``None``. In particular the upstream classic format
  has a column labelled ``rx_state`` that actually repeats ``sig_mode``
  (verified against esp-csi commit 8633d67), so ``rx_state`` is ``None`` for
  that format rather than a misleading number.

Upstream quirk on gain: the esp-csi examples compile ``CONFIG_GAIN_CONTROL`` on
ESP32-S3/C3/C5/C6/C61 and then print ``(int16_t)(compensate_gain * buf[i])``.
Those values are gain-compensated int16, not the raw int8 ``buf`` bytes, and
frames are flagged ``GAIN_COMPENSATED_UPSTREAM``.
"""

from __future__ import annotations

import csv
import re
import zlib
from dataclasses import dataclass
from typing import Any, Union

import numpy as np

from .. import csi_layouts
from ..config import ReceiverConfig
from ..csi_layouts import CsiLayout
from ..schemas import CsiFrame, DeviceIdentity, InputFormat, QualityFlag, SourceMode
from .rollover import CounterUnwrapper, TimestampUnwrapper, to_unsigned

__all__ = [
    "ParseError",
    "HelloRecord",
    "StatRecord",
    "CsiLineRecord",
    "DiagnosticLine",
    "ParsedLine",
    "parse_line",
    "FrameBuilder",
    "PARSE_ERROR_CODES",
    "GAIN_CONTROL_CHIPS",
    "NO_GAIN_CONTROL_CHIPS",
    "compute_crc_hex",
]

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

LINE_TOO_LONG = "LINE_TOO_LONG"
NOT_ASCII = "NOT_ASCII"
FIELD_COUNT = "FIELD_COUNT"
CSV_SYNTAX = "CSV_SYNTAX"
BAD_INT = "BAD_INT"
INT_RANGE = "INT_RANGE"
BAD_MAC = "BAD_MAC"
BAD_TEXT = "BAD_TEXT"
BAD_HEX = "BAD_HEX"
CSI_LIST_SYNTAX = "CSI_LIST_SYNTAX"
TOO_MANY_VALUES = "TOO_MANY_VALUES"
CSI_LEN_MISMATCH = "CSI_LEN_MISMATCH"
CRC_MISMATCH = "CRC_MISMATCH"
MISSING_CRC = "MISSING_CRC"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
EMPTY_CSI = "EMPTY_CSI"
MAC_NOT_CONFIGURED = "MAC_NOT_CONFIGURED"

PARSE_ERROR_CODES = frozenset(
    {
        LINE_TOO_LONG, NOT_ASCII, FIELD_COUNT, CSV_SYNTAX, BAD_INT, INT_RANGE, BAD_MAC, BAD_TEXT,
        BAD_HEX, CSI_LIST_SYNTAX, TOO_MANY_VALUES, CSI_LEN_MISMATCH, CRC_MISMATCH, MISSING_CRC,
        UNSUPPORTED_VERSION, UNSUPPORTED_FORMAT, EMPTY_CSI, MAC_NOT_CONFIGURED,
    }
)

_MAX_DETAIL_CHARS = 160


class ParseError(Exception):
    """A line was rejected. ``code`` is one of :data:`PARSE_ERROR_CODES`."""

    def __init__(self, code: str, detail: str = "") -> None:
        detail = detail[:_MAX_DETAIL_CHARS]
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Parsed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HelloRecord:
    """``RSHELLO`` fields (``None`` = ``NA`` on the wire)."""

    version: int
    fw_name: str | None
    fw_version: str | None
    idf_version: str | None
    chip: str | None
    chip_revision: str | None
    sta_mac: str | None
    mode: str | None
    ltf_config: str | None
    channel: int | None
    secondary_channel: int | None
    tx_mac_filter: str | None
    rate_hz: int | None
    queue_depth: int | None
    baud: int | None
    build_id: str | None


@dataclass(frozen=True, slots=True)
class StatRecord:
    """``RSSTAT`` firmware health counters (``None`` = ``NA``)."""

    version: int
    uptime_ms: int | None
    cb_total: int | None
    cb_filtered: int | None
    enqueued: int | None
    dropped_queue_full: int | None
    dropped_oversize: int | None
    printed: int | None
    tx_ok: int | None
    tx_fail: int | None
    free_heap: int | None
    min_free_heap: int | None


@dataclass(frozen=True, slots=True)
class CsiLineRecord:
    """One CSI line, independent of the wire format. ``None`` = not reported.

    ``seq`` and ``timestamp_us`` are already mapped to unsigned 32-bit values
    (upstream prints them with ``%d``).
    """

    input_format: InputFormat
    seq: int | None
    tx_seq: int | None
    drops: int | None
    mac: str
    rssi: int | None
    rate: int | None
    sig_mode: int | None
    mcs: int | None
    cwb: int | None
    smoothing: int | None
    not_sounding: int | None
    aggregation: int | None
    stbc: int | None
    fec_coding: int | None
    sgi: int | None
    noise_floor: int | None
    ampdu_cnt: int | None
    channel: int | None
    secondary_channel: int | None
    timestamp_us: int | None
    ant: int | None
    sig_len: int | None
    rx_state: int | None
    bb_format: int | None
    agc_gain: int | None
    fft_gain: int | None
    first_word_invalid: bool | None
    csi_len: int
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DiagnosticLine:
    """A non-record line (ESP-IDF log, boot ROM text, CSV header).

    ``text`` has ANSI colour codes removed and other control characters
    replaced. ``looks_like`` names another input format when the line looks
    like that format's records, which usually means the receiver's
    ``input_format`` is misconfigured.
    """

    text: str
    looks_like: str | None = None


ParsedLine = Union[CsiLineRecord, HelloRecord, StatRecord, DiagnosticLine]

# ---------------------------------------------------------------------------
# Field validation helpers
# ---------------------------------------------------------------------------

_INT_RE = re.compile(r"-?[0-9]{1,20}")
_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")
_TEXT_RE = re.compile(r"[A-Za-z0-9_.:+/@-]{1,64}")
# Anchored (used with fullmatch): optional sign, 1..10 digits, comma separated,
# no spaces. Anything else (floats, spaces, names, calls) is rejected.
_CSI_LIST_RE = re.compile(r"\[(?:-?[0-9]{1,10}(?:,-?[0-9]{1,10})*)?\]")
_HEX_RE = re.compile(r"[0-9a-f]*")
_CRC_RE = re.compile(r"[0-9A-F]{8}")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_PRINTABLE_RE = re.compile(r"[\x20-\x7e]*")

_NA = "NA"

INT8 = (-128, 127)
INT16 = (-32768, 32767)
U8 = (0, 255)
U32_PRINTED_SIGNED = (-(1 << 31), (1 << 32) - 1)  # %d of an unsigned 32-bit value
U32 = (0, (1 << 32) - 1)
U64 = (0, (1 << 64) - 1)
BIT = (0, 1)

# Ranges from the wifi_pkt_rx_ctrl_t / esp_wifi_rxctrl_t bit-field widths in
# ESP-IDF v5.5.5 (esp_wifi_types_native.h, esp_wifi_he_types.h).
RANGES: dict[str, tuple[int, int]] = {
    "rssi": INT8,
    "rate": (0, 31),
    "sig_mode": (0, 3),
    "mcs": (0, 127),
    "cwb": BIT,
    "smoothing": BIT,
    "not_sounding": BIT,
    "aggregation": BIT,
    "stbc": (0, 3),
    "fec_coding": BIT,
    "sgi": BIT,
    "noise_floor": INT8,
    "ampdu_cnt": U8,
    "channel": (0, 196),
    "secondary_channel": U8,
    "ant": (0, 3),
    "sig_len": (0, 16383),
    "rx_state": U8,
    "bb_format": (0, 15),
    "agc_gain": U8,
    "fft_gain": INT8,
    "first_word": BIT,
}


def _int(text: str, name: str, lo: int, hi: int) -> int:
    if not _INT_RE.fullmatch(text):
        raise ParseError(BAD_INT, f"{name}={text[:24]!r} is not a decimal integer")
    value = int(text)
    if not lo <= value <= hi:
        raise ParseError(INT_RANGE, f"{name}={value} outside [{lo}, {hi}]")
    return value


def _opt_int(text: str, name: str, lo: int, hi: int) -> int | None:
    return None if text == _NA else _int(text, name, lo, hi)


def _field(text: str, name: str) -> int:
    lo, hi = RANGES[name]
    return _int(text, name, lo, hi)


def _opt_field(text: str, name: str) -> int | None:
    return None if text == _NA else _field(text, name)


def _mac(text: str, name: str) -> str:
    if not _MAC_RE.fullmatch(text):
        raise ParseError(BAD_MAC, f"{name}={text[:24]!r} is not a MAC address")
    return text.lower()


def _opt_mac(text: str, name: str) -> str | None:
    return None if text == _NA else _mac(text, name)


def _opt_text(text: str, name: str) -> str | None:
    if text == _NA:
        return None
    if not _TEXT_RE.fullmatch(text):
        raise ParseError(BAD_TEXT, f"{name} has unexpected characters or length")
    return text


def _u32(text: str, name: str, *, printed_signed: bool) -> int:
    lo, hi = U32_PRINTED_SIGNED if printed_signed else U32
    return to_unsigned(_int(text, name, lo, hi), 32)


def _sanitize_diagnostic(text: str) -> str:
    return _CONTROL_RE.sub("?", _ANSI_RE.sub("", text))


def compute_crc_hex(body: str | bytes) -> str:
    """CRC-32 (``zlib.crc32``) of a record body as 8 uppercase hex digits."""
    data = body.encode("ascii") if isinstance(body, str) else body
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

_UPSTREAM_PREFIX = "CSI_DATA,"
_RS_PREFIXES = ("RSHELLO,", "RSCSI,", "RSSTAT,")
CLASSIC_COLUMNS = 25
C5C6_COLUMNS = 15
_RS_FIELD_COUNTS = {"RSHELLO": 17, "RSCSI": 31, "RSSTAT": 13}
_UPSTREAM_FORMATS = (InputFormat.UPSTREAM_CLASSIC_V1, InputFormat.UPSTREAM_C5C6_V1)


def _strip_terminator(line: str | bytes) -> str | bytes:
    if isinstance(line, bytes):
        if line.endswith(b"\r\n"):
            return line[:-2]
        if line.endswith(b"\n") or line.endswith(b"\r"):
            return line[:-1]
        return line
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def parse_line(
    line: bytes | str,
    fmt: InputFormat,
    *,
    max_line_bytes: int,
    max_csi_values: int,
) -> ParsedLine:
    """Parse one serial line. Raises :class:`ParseError` on any defect.

    Lines that are not machine records of ``fmt`` are returned as
    :class:`DiagnosticLine` and must never be treated as data.
    """
    body = _strip_terminator(line)
    # Length first: for str input len() counts characters, and a non-ASCII
    # line is rejected just below anyway, so chars == bytes for accepted lines.
    if len(body) > max_line_bytes:
        raise ParseError(LINE_TOO_LONG, f"{len(body)} > {max_line_bytes} bytes")
    if isinstance(body, bytes):
        try:
            text = body.decode("ascii")
        except UnicodeDecodeError:
            raise ParseError(NOT_ASCII, "line contains non-ASCII bytes") from None
    else:
        text = body
        if not text.isascii():
            raise ParseError(NOT_ASCII, "line contains non-ASCII characters")

    if fmt in _UPSTREAM_FORMATS:
        if not text.startswith(_UPSTREAM_PREFIX):
            hint = InputFormat.ROOMSENSE_RSCSI_V1.value if text.startswith(_RS_PREFIXES) else None
            return DiagnosticLine(_sanitize_diagnostic(text), looks_like=hint)
        _require_printable(text)
        return _parse_upstream(text, fmt, max_csi_values)
    if fmt == InputFormat.ROOMSENSE_RSCSI_V1:
        if not text.startswith(_RS_PREFIXES):
            hint = "esp-csi-upstream" if text.startswith(_UPSTREAM_PREFIX) else None
            return DiagnosticLine(_sanitize_diagnostic(text), looks_like=hint)
        _require_printable(text)
        return _parse_roomsense(text, max_csi_values)
    raise ParseError(UNSUPPORTED_FORMAT, f"{getattr(fmt, 'value', fmt)!s} is not a serial line format")


def _require_printable(text: str) -> None:
    if not _PRINTABLE_RE.fullmatch(text):
        raise ParseError(NOT_ASCII, "machine record contains control characters")


def _parse_csi_list(text: str, max_csi_values: int, value_range: tuple[int, int]) -> tuple[int, ...]:
    if not _CSI_LIST_RE.fullmatch(text):
        raise ParseError(CSI_LIST_SYNTAX, "CSI data must be [int,int,...] without spaces")
    inner = text[1:-1]
    if not inner:
        raise ParseError(EMPTY_CSI, "CSI list is empty")
    count = inner.count(",") + 1
    if count > max_csi_values:
        raise ParseError(TOO_MANY_VALUES, f"{count} values > max_csi_values={max_csi_values}")
    lo, hi = value_range
    values = tuple(int(v) for v in inner.split(","))
    for v in values:
        if not lo <= v <= hi:
            raise ParseError(INT_RANGE, f"CSI value {v} outside [{lo}, {hi}]")
    return values


def _check_len(csi_len: int, max_csi_values: int) -> None:
    if csi_len == 0:
        raise ParseError(EMPTY_CSI, "len column is 0")
    if csi_len > max_csi_values:
        raise ParseError(TOO_MANY_VALUES, f"len={csi_len} > max_csi_values={max_csi_values}")


def _parse_upstream(text: str, fmt: InputFormat, max_csi_values: int) -> CsiLineRecord:
    try:
        rows = list(csv.reader([text], strict=True))
    except csv.Error as exc:
        raise ParseError(CSV_SYNTAX, str(exc)) from None
    if len(rows) != 1:
        raise ParseError(CSV_SYNTAX, "expected exactly one CSV row")
    cols = rows[0]
    expected = CLASSIC_COLUMNS if fmt == InputFormat.UPSTREAM_CLASSIC_V1 else C5C6_COLUMNS
    if len(cols) != expected:
        raise ParseError(
            FIELD_COUNT, f"{fmt.value} needs {expected} columns, got {len(cols)} (is input_format right?)"
        )
    seq = _u32(cols[1], "seq", printed_signed=True)
    mac = _mac(cols[2], "mac")
    rssi = _field(cols[3], "rssi")
    rate = _field(cols[4], "rate")

    if fmt == InputFormat.UPSTREAM_CLASSIC_V1:
        sig_mode = _field(cols[5], "sig_mode")
        mcs = _field(cols[6], "mcs")
        cwb = _field(cols[7], "cwb")
        smoothing = _field(cols[8], "smoothing")
        not_sounding = _field(cols[9], "not_sounding")
        aggregation = _field(cols[10], "aggregation")
        stbc = _field(cols[11], "stbc")
        fec_coding = _field(cols[12], "fec_coding")
        sgi = _field(cols[13], "sgi")
        noise_floor = _field(cols[14], "noise_floor")
        ampdu_cnt = _field(cols[15], "ampdu_cnt")
        channel = _field(cols[16], "channel")
        secondary_channel = _field(cols[17], "secondary_channel")
        timestamp_us = _u32(cols[18], "local_timestamp", printed_signed=True)
        ant = _field(cols[19], "ant")
        sig_len = _field(cols[20], "sig_len")
        # cols[21] is labelled rx_state/rx_format but the firmware prints
        # rx_ctrl->sig_mode again there. Validate it, then discard it.
        _field(cols[21], "sig_mode")
        rx_state = None
        bb_format = None
        agc_gain = fft_gain = None
        len_col, fw_col, data_col = cols[22], cols[23], cols[24]
    else:
        noise_floor = _field(cols[5], "noise_floor")
        fft_gain = _field(cols[6], "fft_gain")
        agc_gain = _field(cols[7], "agc_gain")
        channel = _field(cols[8], "channel")
        timestamp_us = _u32(cols[9], "local_timestamp", printed_signed=True)
        sig_len = _field(cols[10], "sig_len")
        bb_format = _field(cols[11], "bb_format")
        rx_state = None
        sig_mode = mcs = cwb = smoothing = not_sounding = aggregation = None
        stbc = fec_coding = sgi = ampdu_cnt = secondary_channel = ant = None
        len_col, fw_col, data_col = cols[12], cols[13], cols[14]

    csi_len = _int(len_col, "len", 0, 65535)
    _check_len(csi_len, max_csi_values)
    first_word_invalid = bool(_field(fw_col, "first_word"))
    values = _parse_csi_list(data_col, max_csi_values, INT16)
    if len(values) != csi_len:
        raise ParseError(CSI_LEN_MISMATCH, f"len column {csi_len} != {len(values)} values")
    return CsiLineRecord(
        input_format=fmt,
        seq=seq,
        tx_seq=None,
        drops=None,
        mac=mac,
        rssi=rssi,
        rate=rate,
        sig_mode=sig_mode,
        mcs=mcs,
        cwb=cwb,
        smoothing=smoothing,
        not_sounding=not_sounding,
        aggregation=aggregation,
        stbc=stbc,
        fec_coding=fec_coding,
        sgi=sgi,
        noise_floor=noise_floor,
        ampdu_cnt=ampdu_cnt,
        channel=channel,
        secondary_channel=secondary_channel,
        timestamp_us=timestamp_us,
        ant=ant,
        sig_len=sig_len,
        rx_state=rx_state,
        bb_format=bb_format,
        agc_gain=agc_gain,
        fft_gain=fft_gain,
        first_word_invalid=first_word_invalid,
        csi_len=csi_len,
        values=values,
    )


def _parse_roomsense(text: str, max_csi_values: int) -> ParsedLine:
    star = text.rfind("*")
    if star < 0:
        raise ParseError(MISSING_CRC, "machine record without *CRC32 suffix")
    body, crc_text = text[:star], text[star + 1 :]
    if not _CRC_RE.fullmatch(crc_text):
        raise ParseError(MISSING_CRC, "CRC suffix must be 8 uppercase hex digits")
    if compute_crc_hex(body) != crc_text:
        raise ParseError(CRC_MISMATCH, "CRC32 does not match the record (interleaved or truncated line?)")
    fields = body.split(",")
    if len(fields) < 2:
        raise ParseError(FIELD_COUNT, "record has no version field")
    tag, version = fields[0], fields[1]
    if version != "1":
        raise ParseError(UNSUPPORTED_VERSION, f"{tag} version {version[:8]!r} (host supports 1)")
    expected = _RS_FIELD_COUNTS[tag]
    if len(fields) != expected:
        raise ParseError(FIELD_COUNT, f"{tag} needs {expected} fields, got {len(fields)}")
    if tag == "RSCSI":
        return _parse_rscsi(fields, max_csi_values)
    if tag == "RSHELLO":
        return _parse_rshello(fields)
    return _parse_rsstat(fields)


def _parse_rscsi(f: list[str], max_csi_values: int) -> CsiLineRecord:
    seq = _u32(f[2], "rec_seq", printed_signed=False)
    tx_seq = None if f[3] == _NA else _u32(f[3], "tx_seq", printed_signed=False)
    drops = _opt_int(f[4], "drops", *U32)
    mac = _mac(f[5], "src_mac")
    timestamp_us = None if f[21] == _NA else _u32(f[21], "rx_timestamp_us", printed_signed=False)
    fw = _opt_field(f[28], "first_word")
    csi_len = _int(f[29], "len", 0, 65535)
    _check_len(csi_len, max_csi_values)
    hex_text = f[30]
    if len(hex_text) != 2 * csi_len:
        raise ParseError(CSI_LEN_MISMATCH, f"len={csi_len} but {len(hex_text)} hex chars")
    if not _HEX_RE.fullmatch(hex_text):
        raise ParseError(BAD_HEX, "CSI payload must be lowercase hex")
    raw = np.frombuffer(bytes.fromhex(hex_text), dtype=np.int8)
    return CsiLineRecord(
        input_format=InputFormat.ROOMSENSE_RSCSI_V1,
        seq=seq,
        tx_seq=tx_seq,
        drops=drops,
        mac=mac,
        rssi=_opt_field(f[6], "rssi"),
        rate=_opt_field(f[7], "rate"),
        sig_mode=_opt_field(f[8], "sig_mode"),
        mcs=_opt_field(f[9], "mcs"),
        cwb=_opt_field(f[10], "cwb"),
        smoothing=_opt_field(f[11], "smoothing"),
        not_sounding=_opt_field(f[12], "not_sounding"),
        aggregation=_opt_field(f[13], "aggregation"),
        stbc=_opt_field(f[14], "stbc"),
        fec_coding=_opt_field(f[15], "fec_coding"),
        sgi=_opt_field(f[16], "sgi"),
        noise_floor=_opt_field(f[17], "noise_floor"),
        ampdu_cnt=_opt_field(f[18], "ampdu_cnt"),
        channel=_opt_field(f[19], "channel"),
        secondary_channel=_opt_field(f[20], "secondary_channel"),
        timestamp_us=timestamp_us,
        ant=_opt_field(f[22], "ant"),
        sig_len=_opt_field(f[23], "sig_len"),
        rx_state=_opt_field(f[24], "rx_state"),
        bb_format=_opt_field(f[25], "bb_format"),
        agc_gain=_opt_field(f[26], "agc_gain"),
        fft_gain=_opt_field(f[27], "fft_gain"),
        first_word_invalid=None if fw is None else bool(fw),
        csi_len=csi_len,
        values=tuple(int(v) for v in raw.tolist()),
    )


def _parse_rshello(f: list[str]) -> HelloRecord:
    chip = _opt_text(f[5], "chip")
    return HelloRecord(
        version=1,
        fw_name=_opt_text(f[2], "fw_name"),
        fw_version=_opt_text(f[3], "fw_version"),
        idf_version=_opt_text(f[4], "idf_version"),
        chip=None if chip is None else chip.lower(),
        chip_revision=_opt_text(f[6], "chip_revision"),
        sta_mac=_opt_mac(f[7], "sta_mac"),
        mode=_opt_text(f[8], "mode"),
        ltf_config=_opt_text(f[9], "ltf_config"),
        channel=_opt_field(f[10], "channel"),
        secondary_channel=_opt_field(f[11], "secondary_channel"),
        tx_mac_filter=_opt_mac(f[12], "tx_mac_filter"),
        rate_hz=_opt_int(f[13], "rate_hz", 0, 100_000),
        queue_depth=_opt_int(f[14], "queue_depth", 0, 1_000_000),
        baud=_opt_int(f[15], "baud", 1, 100_000_000),
        build_id=_opt_text(f[16], "build_id"),
    )


def _parse_rsstat(f: list[str]) -> StatRecord:
    names = (
        "uptime_ms", "cb_total", "cb_filtered", "enqueued", "dropped_queue_full",
        "dropped_oversize", "printed", "tx_ok", "tx_fail", "free_heap", "min_free_heap",
    )
    vals: dict[str, Any] = {}
    for i, name in enumerate(names, start=2):
        lo, hi = U64 if name == "uptime_ms" else U32
        vals[name] = _opt_int(f[i], name, lo, hi)
    return StatRecord(version=1, **vals)


# ---------------------------------------------------------------------------
# Frame building
# ---------------------------------------------------------------------------

# Upstream esp-csi examples compile CONFIG_GAIN_CONTROL on these targets and
# then print gain-compensated int16 values (see module docstring).
GAIN_CONTROL_CHIPS = frozenset({"esp32s3", "esp32c3", "esp32c5", "esp32c6", "esp32c61"})
NO_GAIN_CONTROL_CHIPS = frozenset({"esp32", "esp32s2"})

_MAX_LAYOUT_CACHE = 64


def _norm_chip(chip: str | None) -> str | None:
    if chip is None:
        return None
    c = chip.strip().lower().replace("-", "")
    return c or None


class FrameBuilder:
    """Turn parsed records of ONE receiver into :class:`CsiFrame` objects.

    Holds the per-receiver state that a single line cannot carry: firmware
    identity from ``RSHELLO``, counter/timestamp unwrapping and the last
    firmware drop count. Not thread-safe; each reader thread owns one.
    """

    def __init__(
        self,
        receiver: ReceiverConfig,
        *,
        session_id: str,
        source_mode: SourceMode,
        keep_raw_lines: bool = True,
        max_raw_line_chars: int = 4096,
    ) -> None:
        self.receiver = receiver
        self.session_id = session_id
        self.source_mode = source_mode
        self.keep_raw_lines = keep_raw_lines
        self.max_raw_line_chars = max(0, int(max_raw_line_chars))
        self.link_id = f"{receiver.transmitter_id}->{receiver.receiver_id}"
        self._hello: HelloRecord | None = None
        self._counter = CounterUnwrapper(bits=32)
        self._timestamp = TimestampUnwrapper()
        self._last_drops: int | None = None
        self._layout_cache: dict[tuple[Any, ...], tuple[CsiLayout | None, tuple[int, ...] | None]] = {}
        self.last_identity_changes: list[str] = []
        self._identity = self._make_identity()

    # -- identity ---------------------------------------------------------

    @property
    def hello(self) -> HelloRecord | None:
        return self._hello

    @property
    def identity(self) -> DeviceIdentity:
        return self._identity

    def _make_identity(self) -> DeviceIdentity:
        rx = self.receiver
        h = self._hello
        if h is not None:
            return DeviceIdentity(
                receiver_id=rx.receiver_id,
                chip=h.chip,
                board=rx.declared_board,  # boards are only ever user-declared
                firmware_name=h.fw_name,
                firmware_version=h.fw_version,
                idf_version=h.idf_version,
                station_mac=h.sta_mac,
                identity_source="firmware_hello",
            )
        declared = _norm_chip(rx.declared_chip)
        if declared is not None or rx.declared_board is not None:
            return DeviceIdentity(
                receiver_id=rx.receiver_id,
                chip=declared,
                board=rx.declared_board,
                identity_source="user_config",
            )
        return DeviceIdentity(receiver_id=rx.receiver_id, identity_source="unavailable")

    def on_hello(self, hello: HelloRecord) -> list[str]:
        """Store firmware identity. Returns notices for the UI/log:

        ``IDENTITY_CHANGED`` (chip, firmware, station MAC, LTF config or channel
        differ from the previous hello; field names in
        ``last_identity_changes``), ``DECLARED_CHIP_MISMATCH``,
        ``LTF_CONFIG_MISMATCH`` and ``TX_MAC_FILTER_MISMATCH`` (firmware
        disagrees with the user's receiver config).
        """
        notices: list[str] = []
        prev = self._hello
        self.last_identity_changes = []
        if prev is not None:
            for name in ("chip", "fw_name", "fw_version", "sta_mac", "ltf_config", "channel"):
                if getattr(prev, name) != getattr(hello, name):
                    self.last_identity_changes.append(name)
            if self.last_identity_changes:
                notices.append("IDENTITY_CHANGED")
        rx = self.receiver
        declared = _norm_chip(rx.declared_chip)
        if declared is not None and hello.chip is not None and _norm_chip(hello.chip) != declared:
            notices.append("DECLARED_CHIP_MISMATCH")
        if rx.ltf_config is not None and hello.ltf_config is not None and rx.ltf_config != hello.ltf_config:
            notices.append("LTF_CONFIG_MISMATCH")
        if (
            rx.transmitter_mac is not None
            and hello.tx_mac_filter is not None
            and rx.transmitter_mac.lower() != hello.tx_mac_filter
        ):
            notices.append("TX_MAC_FILTER_MISMATCH")
        self._hello = hello
        self._identity = self._make_identity()
        return notices

    def _mac_filter(self) -> str | None:
        if self.receiver.transmitter_mac is not None:
            return self.receiver.transmitter_mac.lower()
        if self._hello is not None:
            return self._hello.tx_mac_filter
        return None

    # -- layout -----------------------------------------------------------

    def _layout(
        self, rec: CsiLineRecord, chip: str | None, ltf_config: str | None
    ) -> tuple[CsiLayout | None, tuple[int, ...] | None, bool]:
        allow = self.receiver.allow_undocumented_layout_assumption
        family = csi_layouts.chip_family(chip, allow_undocumented_assumption=allow)
        assumed = family is not None and chip in csi_layouts.UNDOCUMENTED_LAYOUT_CHIPS
        key = (
            family, len(rec.values), ltf_config, rec.secondary_channel, rec.sig_mode, rec.cwb, rec.stbc,
            not assumed, bool(rec.first_word_invalid),
        )
        cached = self._layout_cache.get(key)
        if cached is None:
            layout = csi_layouts.resolve_layout(
                family=family,
                total_values=len(rec.values),
                ltf_config=ltf_config,
                secondary_channel=rec.secondary_channel,
                sig_mode=rec.sig_mode,
                cwb=rec.cwb,
                stbc=rec.stbc,
                documented=not assumed,
            )
            valid: tuple[int, ...] | None = None
            if layout is not None:
                mask = layout.valid_position_mask().copy()
                # Same rule as csi_layouts.extract_complex: the first four
                # bytes are the first two subcarrier positions.
                if rec.first_word_invalid and layout.segment_value_offset == 0:
                    mask[:2] = False
                valid = tuple(int(k) for k in layout.k_indices()[mask])
            if len(self._layout_cache) >= _MAX_LAYOUT_CACHE:
                self._layout_cache.clear()
            cached = (layout, valid)
            self._layout_cache[key] = cached
        return cached[0], cached[1], assumed

    # -- build ------------------------------------------------------------

    def build(
        self,
        rec: CsiLineRecord,
        *,
        host_monotonic_ns: int | None,
        host_unix_ns: int | None,
        raw_line: str | None = None,
    ) -> CsiFrame:
        """Build a frame. Raises ``ParseError(MAC_NOT_CONFIGURED)`` for lines
        from a transmitter other than the configured one (they are not part
        of any configured sensing link and are not kept)."""
        rx = self.receiver
        wanted_mac = self._mac_filter()
        if wanted_mac is not None and rec.mac != wanted_mac:
            raise ParseError(MAC_NOT_CONFIGURED, "CSI from a transmitter that is not configured for this link")

        flags: list[str] = []
        identity = self._identity
        chip = _norm_chip(identity.chip)
        is_rs = rec.input_format == InputFormat.ROOMSENSE_RSCSI_V1
        if is_rs:
            ltf_config = self._hello.ltf_config if self._hello is not None else None
        else:
            ltf_config = rx.ltf_config

        layout, valid, assumed = self._layout(rec, chip, ltf_config)
        if layout is None:
            flags.append(QualityFlag.UNKNOWN_LAYOUT.value)
        elif assumed or not layout.documented:
            flags.append(QualityFlag.UNDOCUMENTED_LAYOUT_ASSUMPTION.value)
        if rec.first_word_invalid:
            flags.append(QualityFlag.FIRST_WORD_INVALID.value)

        if is_rs:
            gain_comp: bool | None = False
        elif chip in GAIN_CONTROL_CHIPS:
            gain_comp = True
            flags.append(QualityFlag.GAIN_COMPENSATED_UPSTREAM.value)
        elif chip in NO_GAIN_CONTROL_CHIPS:
            gain_comp = False
        else:
            gain_comp = None

        counter_unwrapped: int | None = None
        if rec.seq is not None:
            counter_unwrapped, cflags = self._counter.update(rec.seq)
            flags.extend(cflags)
        ts_unwrapped: int | None = None
        if rec.timestamp_us is None:
            flags.append(QualityFlag.DEVICE_TIMESTAMP_UNAVAILABLE.value)
        else:
            ts_unwrapped, tflags = self._timestamp.update(rec.timestamp_us)
            flags.extend(tflags)
        if host_monotonic_ns is None or host_unix_ns is None:
            flags.append(QualityFlag.HOST_TIMESTAMP_UNAVAILABLE.value)

        if rec.drops is not None:
            if self._last_drops is not None and rec.drops > self._last_drops:
                flags.append(QualityFlag.FIRMWARE_QUEUE_DROPS.value)
            self._last_drops = rec.drops
        if rec.rx_state is not None and rec.rx_state != 0:
            flags.append(QualityFlag.RX_STATE_ERROR.value)
        if all(v == 0 for v in rec.values):
            flags.append(QualityFlag.ALL_ZERO_CSI.value)
        # Saturation is only meaningful for raw int8 bytes; gain-compensated
        # int16 values no longer show where the ADC/int8 range was hit.
        if gain_comp is False and any(v == -128 or v == 127 for v in rec.values):
            flags.append(QualityFlag.SATURATED_VALUES.value)

        bandwidth = None if rec.cwb is None else (40 if rec.cwb == 1 else 20)
        kept_line = None
        if self.keep_raw_lines and raw_line is not None:
            kept_line = raw_line.rstrip("\r\n")[: self.max_raw_line_chars]

        return CsiFrame(
            source_mode=self.source_mode,
            session_id=self.session_id,
            receiver_id=rx.receiver_id,
            transmitter_id=rx.transmitter_id,
            link_id=self.link_id,
            input_format=rec.input_format,
            device=identity,
            frame_counter=rec.seq,
            frame_counter_unwrapped=counter_unwrapped,
            transmitter_counter=rec.tx_seq,
            device_timestamp_us=rec.timestamp_us,
            device_timestamp_unwrapped_us=ts_unwrapped,
            host_arrival_monotonic_ns=host_monotonic_ns,
            host_arrival_unix_ns=host_unix_ns,
            transmitter_mac=rec.mac,
            channel=rec.channel,
            secondary_channel=rec.secondary_channel,
            bandwidth_mhz=bandwidth,
            sig_mode=rec.sig_mode,
            bb_format=rec.bb_format,
            mcs=rec.mcs,
            rate=rec.rate,
            stbc=rec.stbc,
            rssi_dbm=rec.rssi,
            noise_floor_dbm=rec.noise_floor,
            agc_gain=rec.agc_gain,
            fft_gain=rec.fft_gain,
            sig_len=rec.sig_len,
            rx_state=rec.rx_state,
            antenna=rec.ant,
            first_word_invalid=rec.first_word_invalid,
            csi_len=rec.csi_len,
            raw_csi=rec.values,
            values_are_gain_compensated=gain_comp,
            layout_id=None if layout is None else layout.layout_id,
            valid_subcarriers=valid,
            quality_flags=tuple(dict.fromkeys(flags)),
            firmware_drop_count=rec.drops,
            raw_line=kept_line,
        )
