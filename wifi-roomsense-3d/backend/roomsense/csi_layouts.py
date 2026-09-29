"""CSI buffer layouts, transcribed from the ESP-IDF v5.5.5 Wi-Fi guide.

Source of truth: ``docs/en/api-guides/wifi.rst`` section "Wi-Fi Channel State
Information" in espressif/esp-idf tag v5.5.5 (commit b774170ff46c), plus the
``wifi_csi_info_t`` / ``wifi_pkt_rx_ctrl_t`` definitions in
``components/esp_wifi/include``. Key facts used here:

* Each subcarrier is two signed bytes: **imaginary first, then real**.
* LTF order in the buffer is LLTF, HT-LTF, STBC-HT-LTF (classic chips).
* ``first_word_invalid`` => the first four bytes (first two subcarriers) are
  invalid due to a hardware limitation.
* ESP32 / ESP32-S2 / ESP32-S3 / ESP32-C3 ("classic"): LLTF is 64 subcarriers
  (128 bytes). Its index order depends on ``secondary_channel``:
  none -> 0..31, -32..-1; below -> 0..63; above -> -64..-1.
* ESP32-C5: its own table (106/114/228/234/468/490 bytes).
* ESP32-C6 / ESP32-C61: **no layout table is published in the v5.5.5 guide**.
  They are rejected unless the user opts into an explicitly flagged
  assumption.

Anything that does not match a documented layout is rejected by returning
``None``; callers must never pad or truncate data to force a shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np

DOC_REF = "ESP-IDF v5.5.5 docs/en/api-guides/wifi.rst#wi-fi-channel-state-information"

ChipFamily = Literal["classic", "c5"]
LtfConfig = Literal["lltf_only", "lltf_htltf_stbc", "c5_default"]

CLASSIC_CHIPS = frozenset({"esp32", "esp32s2", "esp32s3", "esp32c3"})
C5_CHIPS = frozenset({"esp32c5"})
UNDOCUMENTED_LAYOUT_CHIPS = frozenset({"esp32c6", "esp32c61"})


def chip_family(chip: str | None, *, allow_undocumented_assumption: bool = False) -> ChipFamily | None:
    """Map a chip name to a documented layout family, or ``None``.

    ``allow_undocumented_assumption`` lets ESP32-C6/C61 reuse the ESP32-C5
    table. That is an *assumption*, not documentation; frames produced under
    it are flagged by the parser.
    """
    if chip is None:
        return None
    c = chip.strip().lower().replace("-", "")
    if c in CLASSIC_CHIPS:
        return "classic"
    if c in C5_CHIPS:
        return "c5"
    if c in UNDOCUMENTED_LAYOUT_CHIPS and allow_undocumented_assumption:
        return "c5"
    return None


@dataclass(frozen=True)
class CsiLayout:
    """A documented CSI buffer layout.

    ``segment_subcarriers`` gives, for the *primary segment only* (the part
    the processing pipeline uses), the native subcarrier index of each complex
    position in buffer order. ``primary_offset`` converts native indices to
    indices relative to the primary 20 MHz channel centre: ``k = native -
    primary_offset``. ``valid_k`` are the occupied (data + pilot) subcarriers
    relative to the primary channel centre, excluding DC and guard bands.
    """

    layout_id: str
    family: ChipFamily
    total_values: int  # number of int8 values in the whole buffer
    segment_name: str  # "LLTF", "HT-LTF", "HE-LTF", ...
    segment_value_offset: int  # index into raw values where the segment starts
    segment_subcarriers: tuple[int, ...]
    primary_offset: int
    valid_k: tuple[int, ...]
    documented: bool  # False => assumption, not from the docs table
    doc_ref: str = DOC_REF
    notes: str = ""

    @property
    def n_positions(self) -> int:
        return len(self.segment_subcarriers)

    def k_indices(self) -> np.ndarray:
        return np.asarray(self.segment_subcarriers, dtype=np.int32) - self.primary_offset

    def valid_position_mask(self) -> np.ndarray:
        k = self.k_indices()
        return np.isin(k, np.asarray(self.valid_k, dtype=np.int32))


def _rng(a: int, b: int) -> list[int]:
    """Inclusive integer range a..b (a may be > b is not used)."""
    return list(range(a, b + 1))


_LEGACY_VALID = tuple(k for k in range(-26, 27) if k != 0)
_HT20_VALID = tuple(k for k in range(-28, 29) if k != 0)
_HT40_VALID_C5 = tuple(k for k in range(-58, 59) if k not in (-1, 0, 1))
_HE20_VALID_C5 = tuple(k for k in range(-122, 123) if k not in (-1, 0, 1))

# Classic-chip LLTF (64 subcarriers) by secondary channel. Values: (native index
# order, primary_offset).
_CLASSIC_LLTF = {
    0: (_rng(0, 31) + _rng(-32, -1), 0),  # none
    2: (_rng(0, 63), 32),  # below: primary is the upper 20 MHz half
    1: (_rng(-64, -1), -32),  # above: primary is the lower 20 MHz half
}

# Classic-chip total buffer sizes documented for lltf+htltf+stbc enabled,
# keyed by (secondary_channel, sig_mode_is_ht, cwb_is_40, stbc).
_CLASSIC_TOTALS_ALL_LTF = {
    (0, False, False, False): 128,
    (0, True, False, False): 256,
    (0, True, False, True): 384,
    (2, False, False, False): 128,
    (2, True, False, False): 256,
    (2, True, False, True): 380,
    (2, True, True, False): 384,
    (2, True, True, True): 612,
    (1, False, False, False): 128,
    (1, True, False, False): 256,
    (1, True, False, True): 376,
    (1, True, True, False): 384,
    (1, True, True, True): 612,
}


def _classic_lltf_layout(secondary_channel: int, total: int, ltf_config: str) -> CsiLayout:
    order, offset = _CLASSIC_LLTF[secondary_channel]
    sec_name = {0: "none", 1: "above", 2: "below"}[secondary_channel]
    return CsiLayout(
        layout_id=f"classic.{ltf_config}.sec_{sec_name}.total{total}.LLTF64",
        family="classic",
        total_values=total,
        segment_name="LLTF",
        segment_value_offset=0,
        segment_subcarriers=tuple(order),
        primary_offset=offset,
        valid_k=_LEGACY_VALID,
        documented=True,
        notes="LLTF is the first 128 values when LLTF is enabled (docs: LTF order LLTF, HT-LTF, STBC-HT-LTF).",
    )


def resolve_layout(
    *,
    family: ChipFamily | None,
    total_values: int,
    ltf_config: LtfConfig | str | None,
    secondary_channel: int | None,
    sig_mode: int | None,
    cwb: int | None,
    stbc: int | None,
    documented: bool = True,
) -> CsiLayout | None:
    """Return the documented layout for this frame or ``None`` (reject).

    For classic chips the pipeline uses LLTF only (the first 128 values). For
    ESP32-C5 it uses the single LTF segment the chip emits for the packet type.
    """
    if family is None or ltf_config is None:
        return None
    if family == "classic":
        if secondary_channel not in (0, 1, 2):
            return None
        if ltf_config == "lltf_only":
            # Only LLTF enabled => buffer is exactly the 64-subcarrier LLTF.
            if total_values != 128:
                return None
            return _classic_lltf_layout(secondary_channel, total_values, ltf_config)
        if ltf_config == "lltf_htltf_stbc":
            if sig_mode is None or cwb is None or stbc is None:
                return None
            key = (secondary_channel, sig_mode == 1, cwb == 1, stbc != 0)
            expected = _CLASSIC_TOTALS_ALL_LTF.get(key)
            if expected is None or expected != total_values:
                return None
            return _classic_lltf_layout(secondary_channel, total_values, ltf_config)
        return None

    if family == "c5":
        if ltf_config != "c5_default":
            return None
        # Only the secondary-channel "none" rows and HT40 rows have an
        # unambiguous index order in the table; others are rejected.
        if total_values == 106 and secondary_channel in (0, None):
            order, valid, seg = _rng(0, 26) + _rng(-26, -1), _LEGACY_VALID, "LLTF"
        elif total_values == 114 and secondary_channel in (0, None):
            order, valid, seg = _rng(0, 28) + _rng(-28, -1), _HT20_VALID, "HT-LTF"
        elif total_values == 234:
            order, valid, seg = _rng(0, 58) + _rng(-58, -1), _HT40_VALID_C5, "HT-LTF"
        elif total_values == 490 and secondary_channel in (0, None):
            order, valid, seg = _rng(0, 122) + _rng(-122, -1), _HE20_VALID_C5, "HE-LTF"
        else:
            return None
        return CsiLayout(
            layout_id=f"c5.{ltf_config}.total{total_values}.{seg}{len(order)}" + ("" if documented else ".assumed"),
            family="c5",
            total_values=total_values,
            segment_name=seg,
            segment_value_offset=0,
            segment_subcarriers=tuple(order),
            primary_offset=0,
            valid_k=valid,
            documented=documented,
            notes="ESP32-C5 table. HT40 indices are relative to the 40 MHz centre."
            if total_values == 234
            else "ESP32-C5 table.",
        )
    return None


def layout_from_id(layout_id: str | None) -> CsiLayout | None:
    """Rebuild a layout from its ``layout_id`` (as stored on recorded frames).

    Returns ``None`` for unknown or malformed ids. The id encodes every input
    of :func:`resolve_layout`, so this is a pure inverse.
    """
    if not layout_id:
        return None
    parts = layout_id.split(".")
    try:
        if parts[0] == "classic" and len(parts) == 5:
            ltf = parts[1]
            sec = {"sec_none": 0, "sec_above": 1, "sec_below": 2}[parts[2]]
            total = int(parts[3].removeprefix("total"))
            if ltf not in ("lltf_only", "lltf_htltf_stbc") or parts[4] != "LLTF64":
                return None
            if ltf == "lltf_only" and total != 128:
                return None
            if ltf == "lltf_htltf_stbc" and total not in {v for (s, *_), v in _CLASSIC_TOTALS_ALL_LTF.items() if s == sec}:
                return None
            return _classic_lltf_layout(sec, total, ltf)
        if parts[0] == "c5" and len(parts) in (4, 5):
            documented = not (len(parts) == 5 and parts[4] == "assumed")
            if len(parts) == 5 and documented:
                return None
            total = int(parts[2].removeprefix("total"))
            lay = resolve_layout(
                family="c5", total_values=total, ltf_config=parts[1], secondary_channel=None,
                sig_mode=None, cwb=None, stbc=None, documented=documented,
            )
            if lay is None:
                return None
            return lay if lay.layout_id == layout_id else None
    except (KeyError, ValueError):
        return None
    return None


def extract_complex(
    raw_values: Sequence[int] | np.ndarray,
    layout: CsiLayout,
    first_word_invalid: bool | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert the layout's primary segment to complex CSI.

    Returns ``(k, csi, valid)`` where ``k`` are primary-relative subcarrier
    indices, ``csi`` is complex64 (real + j*imag), and ``valid`` is a boolean
    mask: occupied subcarrier AND not invalidated by ``first_word_invalid``.

    Raises ``ValueError`` if the value count does not match the layout.
    """
    raw = np.asarray(raw_values, dtype=np.int32)
    if raw.shape != (layout.total_values,):
        raise ValueError(f"expected {layout.total_values} values for {layout.layout_id}, got {raw.shape}")
    start = layout.segment_value_offset
    seg = raw[start : start + 2 * layout.n_positions]
    imag = seg[0::2].astype(np.float32)
    real = seg[1::2].astype(np.float32)
    csi = (real + 1j * imag).astype(np.complex64)
    valid = layout.valid_position_mask().copy()
    if first_word_invalid and start == 0:
        valid[:2] = False  # first four bytes == first two subcarrier positions
    return layout.k_indices(), csi, valid
