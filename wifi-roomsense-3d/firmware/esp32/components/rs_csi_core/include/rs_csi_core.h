/*
 * rs_csi_core: platform-independent core of the RoomSense ESP32 firmware.
 *
 * This component implements the device side of docs/SERIAL_PROTOCOL.md
 * ("roomsense-rscsi-v1"): the RSHELLO / RSCSI / RSSTAT line formats, the CRC-32
 * framing, and the 12-byte RSTX ESP-NOW payload.
 *
 * It is plain C11 and includes no ESP-IDF headers, so the exact code that runs
 * on the ESP32 is also compiled and unit-tested on the development host
 * (firmware/esp32/host_tests). It is also freestanding: it needs only
 * <stdbool.h>, <stddef.h> and <stdint.h> (no libc calls, no heap, no printf),
 * which keeps its behaviour identical across newlib (ESP-IDF) and glibc.
 *
 * Nothing here allocates memory or keeps global state, so every function is
 * safe to call from any task. None of them is meant to be called from the
 * Wi-Fi task's CSI callback except the small record setters and
 * rs_espnow_body() / rs_parse_tx_payload(), which are O(1).
 */
#ifndef RS_CSI_CORE_H
#define RS_CSI_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------- */
/* Protocol constants                                                        */
/* ------------------------------------------------------------------------- */

/** Version number that follows every tag (RSHELLO,1 / RSCSI,1 / RSSTAT,1). */
#define RS_PROTOCOL_VERSION 1

/**
 * Largest CSI buffer documented in the ESP-IDF v5.5.5 Wi-Fi guide (classic
 * chips, HT40 + STBC with LLTF, HT-LTF and STBC-HT-LTF enabled). The receiver
 * drops (and counts) anything longer instead of truncating it.
 */
#define RS_MAX_CSI_LEN 612

#define RS_MAC_LEN 6
/** "aa:bb:cc:dd:ee:ff" without the NUL. */
#define RS_MAC_STR_LEN 17

/** RSTX ESP-NOW payload: magic(4) version(1) tx_id(1) seq(4, LE) rate_hz(2, LE). */
#define RS_TX_PAYLOAD_LEN 12
#define RS_TX_PAYLOAD_VERSION 1

/** Longest string field accepted in RSHELLO; longer strings are sent as NA. */
#define RS_HELLO_STR_MAX 40

/* Widths used to derive the worst-case line sizes below. */
#define RS_U32_DIGITS 10 /* 4294967295 */
#define RS_I32_DIGITS 11 /* -2147483648 */
#define RS_U64_DIGITS 20 /* 18446744073709551615 */
#define RS_CSI_LEN_DIGITS 3 /* len <= RS_MAX_CSI_LEN (612) */
#define RS_CRC_SUFFIX_LEN 10 /* "*XXXXXXXX\n" */

/*
 * Worst-case buffer sizes, *including* the terminating NUL. A formatter given
 * a buffer of at least this size never fails for a valid input. The host
 * tests check that the worst-case inputs produce exactly (size - 1) bytes.
 */

/* RSCSI: 31 fields -> 30 commas. 21 int32 rx_ctrl fields, 1 u32 timestamp. */
#define RS_CSI_LINE_MAX                                                        \
    (5 /* RSCSI */ + 1 /* version */ + 3 * RS_U32_DIGITS /* rec_seq,tx_seq,drops */ \
     + RS_MAC_STR_LEN + 21 * RS_I32_DIGITS + RS_U32_DIGITS /* timestamp */     \
     + 1 /* first_word_invalid */ + RS_CSI_LEN_DIGITS + 2 * RS_MAX_CSI_LEN     \
     + 30 /* commas */ + RS_CRC_SUFFIX_LEN + 1 /* NUL */)

/* RSHELLO: 17 fields -> 16 commas; 8 string fields. */
#define RS_HELLO_LINE_MAX                                                      \
    (7 /* RSHELLO */ + 1 + 8 * RS_HELLO_STR_MAX + 2 * RS_MAC_STR_LEN            \
     + 2 * RS_I32_DIGITS /* channel, secondary */ + 3 * RS_U32_DIGITS           \
     + 16 + RS_CRC_SUFFIX_LEN + 1)

/* RSSTAT: 13 fields -> 12 commas. */
#define RS_STAT_LINE_MAX                                                       \
    (6 /* RSSTAT */ + 1 + RS_U64_DIGITS + 10 * RS_U32_DIGITS + 12              \
     + RS_CRC_SUFFIX_LEN + 1)

/* ------------------------------------------------------------------------- */
/* CRC-32 and hex                                                            */
/* ------------------------------------------------------------------------- */

/**
 * IEEE 802.3 CRC-32 (reflected, poly 0xEDB88320, init/xorout 0xFFFFFFFF),
 * identical to Python's zlib.crc32(data). rs_crc32("123456789") == 0xCBF43926.
 */
uint32_t rs_crc32(const void *data, size_t len);

/**
 * Incremental form: crc = rs_crc32_update(0, a, na); crc = rs_crc32_update(crc, b, nb)
 * equals rs_crc32(a||b). Same convention as zlib.crc32(data, value).
 */
uint32_t rs_crc32_update(uint32_t crc, const void *data, size_t len);

/**
 * Encode n int8 values as 2*n lowercase hex characters (two's complement
 * bytes, so -1 -> "ff", -128 -> "80") followed by a NUL.
 * Returns false, and writes nothing except out[0] = '\0' when cap > 0, if
 * cap < 2*n + 1. Never writes past cap.
 */
bool rs_hex_encode_i8(const int8_t *vals, size_t n, char *out, size_t cap);

/* ------------------------------------------------------------------------- */
/* MAC helpers                                                               */
/* ------------------------------------------------------------------------- */

/** Strict parse of "xx:xx:xx:xx:xx:xx" (hex digits of either case). */
bool rs_parse_mac(const char *s, uint8_t mac[RS_MAC_LEN]);

/** Bit 0 of the first octet clear (ESP-IDF refuses to set a multicast MAC). */
bool rs_mac_is_unicast(const uint8_t mac[RS_MAC_LEN]);

/** Bit 1 of the first octet set (locally administered, not a vendor OUI). */
bool rs_mac_is_locally_administered(const uint8_t mac[RS_MAC_LEN]);

/** Writes 17 lowercase characters plus NUL into out (needs 18 bytes). */
void rs_format_mac(char out[RS_MAC_STR_LEN + 1], const uint8_t mac[RS_MAC_LEN]);

/**
 * esp_chip_info_t.revision is "MXX" (major*100 + minor). Formats "M.m", e.g.
 * 2 -> "0.2", 101 -> "1.1". Returns false if cap is too small (out[0]='\0').
 */
bool rs_format_chip_revision(char *out, size_t cap, uint32_t revision);

/* ------------------------------------------------------------------------- */
/* CSI record                                                                */
/* ------------------------------------------------------------------------- */

/**
 * rx_ctrl metadata carried as int32, in RSCSI wire order (field numbers from
 * docs/SERIAL_PROTOCOL.md). rx_timestamp_us (field 21) is unsigned and lives
 * in its own member. A field whose presence bit is clear is emitted as "NA":
 * on ESP32-C5/C6/C61 several classic fields do not exist in esp_wifi_rxctrl_t,
 * and agc_gain / fft_gain are never read in protocol v1.
 */
typedef enum {
    RS_RX_RSSI = 0,          /* 6  */
    RS_RX_RATE,              /* 7  */
    RS_RX_SIG_MODE,          /* 8  */
    RS_RX_MCS,               /* 9  */
    RS_RX_CWB,               /* 10 */
    RS_RX_SMOOTHING,         /* 11 */
    RS_RX_NOT_SOUNDING,      /* 12 */
    RS_RX_AGGREGATION,       /* 13 */
    RS_RX_STBC,              /* 14 */
    RS_RX_FEC_CODING,        /* 15 */
    RS_RX_SGI,               /* 16 */
    RS_RX_NOISE_FLOOR,       /* 17 */
    RS_RX_AMPDU_CNT,         /* 18 */
    RS_RX_CHANNEL,           /* 19 */
    RS_RX_SECONDARY_CHANNEL, /* 20 */
    /* field 21 is rx_timestamp_us (see rs_csi_record_t) */
    RS_RX_ANT,       /* 22 */
    RS_RX_SIG_LEN,   /* 23 */
    RS_RX_RX_STATE,  /* 24 */
    RS_RX_BB_FORMAT, /* 25 */
    RS_RX_AGC_GAIN,  /* 26 */
    RS_RX_FFT_GAIN,  /* 27 */
    RS_RX_FIELD_COUNT
} rs_rx_field_t;

/** Index of the first rx field that is written *after* rx_timestamp_us. */
#define RS_RX_FIRST_AFTER_TIMESTAMP RS_RX_ANT

/**
 * One CSI measurement as it travels from the Wi-Fi callback to the printer
 * task. The CSI buffer is owned (copied), fixed-size and bounded, so a record
 * can be passed by value through a FreeRTOS queue. About 730 bytes.
 */
typedef struct {
    uint32_t rec_seq; /* receiver sequence (wraps at 2^32) */
    uint32_t tx_seq;  /* RSTX transmitter sequence, valid if has_tx_seq */
    bool has_tx_seq;
    uint32_t drops; /* cumulative device drops (queue full + oversize) */
    uint8_t src_mac[RS_MAC_LEN];
    uint32_t rx_present; /* bit i set => rx[i] is valid */
    int32_t rx[RS_RX_FIELD_COUNT];
    bool has_rx_timestamp;
    uint32_t rx_timestamp_us;
    bool first_word_invalid;
    uint16_t len; /* number of valid int8 values in csi[] (<= RS_MAX_CSI_LEN) */
    int8_t csi[RS_MAX_CSI_LEN];
} rs_csi_record_t;

/**
 * Clear all metadata (every optional field becomes NA, len = 0). The CSI
 * buffer itself is not cleared because only csi[0..len) is ever read; this
 * keeps the reset cheap enough for the Wi-Fi callback.
 */
void rs_csi_record_reset(rs_csi_record_t *rec);

/** Set an rx field and mark it present. Out-of-range f is ignored. */
void rs_csi_record_set_rx(rs_csi_record_t *rec, rs_rx_field_t f, int32_t value);

/** True if rx field f was set. */
bool rs_csi_record_has_rx(const rs_csi_record_t *rec, rs_rx_field_t f);

/* ------------------------------------------------------------------------- */
/* Line formatters                                                           */
/* ------------------------------------------------------------------------- */

/*
 * All three formatters:
 *  - write one complete machine line "TAG,1,...*CRC8HEX\n" into out;
 *  - return the number of bytes written, including the trailing '\n' and
 *    excluding the terminating NUL (i.e. exactly what should be sent);
 *  - return -1 if the line would not fit in cap or the input is invalid.
 *    On failure out[0] is set to '\0' (when cap > 0) so a partial line can
 *    never be sent by mistake;
 *  - never write at or beyond out[cap], and always NUL-terminate when cap > 0.
 */

/** RSCSI line. Returns -1 if rec->len > RS_MAX_CSI_LEN. */
int rs_format_csi_line(char *out, size_t cap, const rs_csi_record_t *rec);

/** Content of an RSHELLO line. NULL / empty / unsafe strings are sent as NA. */
typedef struct {
    const char *fw_name;       /* "roomsense_csi_rx" */
    const char *fw_version;    /* "0.1.0" */
    const char *idf_version;   /* esp_get_idf_version() */
    const char *chip;          /* CONFIG_IDF_TARGET, e.g. "esp32s3" */
    const char *chip_revision; /* "0.2" (see rs_format_chip_revision) */
    bool has_sta_mac;
    uint8_t sta_mac[RS_MAC_LEN]; /* receiver's own station MAC */
    const char *mode;            /* "ESPNOW_RX" or "ROUTER_PING" */
    const char *ltf_config;      /* "lltf_only" or "c5_default" */
    bool has_channel;
    int32_t channel;
    bool has_secondary_channel;
    int32_t secondary_channel; /* 0 none, 1 above, 2 below */
    bool has_tx_mac_filter;
    uint8_t tx_mac_filter[RS_MAC_LEN];
    bool has_rate_hz;
    uint32_t rate_hz;
    bool has_queue_depth;
    uint32_t queue_depth;
    bool has_baud;
    uint32_t baud; /* console UART baud; NA if the console is not a UART */
    const char *build_id; /* git describe of the firmware tree, or NULL */
} rs_hello_t;

int rs_format_hello(char *out, size_t cap, const rs_hello_t *hello);

/** Content of an RSSTAT line. Counters are u32 and wrap; the host unwraps. */
typedef struct {
    uint64_t uptime_ms;
    uint32_t cb_total;
    uint32_t cb_filtered;
    uint32_t enqueued;
    uint32_t dropped_queue_full;
    uint32_t dropped_oversize;
    uint32_t printed;
    bool has_tx_ok; /* router mode: ping replies received */
    uint32_t tx_ok;
    bool has_tx_fail; /* router mode: ping timeouts */
    uint32_t tx_fail;
    uint32_t free_heap;
    uint32_t min_free_heap;
} rs_stat_t;

int rs_format_stat(char *out, size_t cap, const rs_stat_t *stat);

/**
 * True if s is a safe RSHELLO string value: 1..RS_HELLO_STR_MAX printable,
 * non-space ASCII characters, none of which is ',' or '*'. Exposed for tests.
 */
bool rs_hello_str_is_safe(const char *s);

/* ------------------------------------------------------------------------- */
/* RSTX transmitter payload and ESP-NOW framing                              */
/* ------------------------------------------------------------------------- */

/**
 * Build the 12-byte little-endian RSTX payload. Returns RS_TX_PAYLOAD_LEN, or
 * 0 (writing nothing) if out is NULL or cap < RS_TX_PAYLOAD_LEN.
 */
size_t rs_build_tx_payload(uint8_t *out, size_t cap, uint8_t tx_id, uint32_t seq,
                           uint16_t rate_hz);

/**
 * Parse an RSTX payload. Returns true only if p is non-NULL, n >= 12, the
 * magic is "RSTX" and the version is 1; then *seq and *tx_id (each may be
 * NULL) are written. Bytes beyond the first 12 are ignored. On failure the
 * outputs are left untouched.
 */
bool rs_parse_tx_payload(const uint8_t *p, size_t n, uint32_t *seq, uint8_t *tx_id);

/** Like rs_parse_tx_payload but also returns the transmitter's configured rate. */
bool rs_parse_tx_payload_ex(const uint8_t *p, size_t n, uint32_t *seq, uint8_t *tx_id,
                            uint16_t *rate_hz);

/**
 * Offset of the ESP-NOW body inside the vendor-specific action frame body
 * (the part after the 24-byte MAC header), from ESP-IDF v5.5.5
 * docs/en/api-reference/network/esp_now.rst "Frame Format":
 *   category(1) OUI(3) random(4) | element id(1) length(1) OUI(3) type(1) version(1) | body
 * = 8 + 7 = 15. esp-csi's csi_recv reads its counter at info->payload + 15.
 */
#define RS_ESPNOW_BODY_OFFSET 15

/**
 * Locate the ESP-NOW body in a frame body (wifi_csi_info_t.payload,
 * payload_len). Checks every structural byte the ESP-IDF docs define
 * (category 127, Espressif OUI 18:fe:34 twice, element id 221, type 4) and
 * that the element's length field fits inside payload_len, so a frame that is
 * not ESP-NOW (or a wrong offset assumption) yields false, never garbage.
 * On success *body points into payload and *body_len is the body length.
 */
bool rs_espnow_body(const uint8_t *payload, size_t payload_len, const uint8_t **body,
                    size_t *body_len);

/* ------------------------------------------------------------------------- */
/* Timing helper                                                             */
/* ------------------------------------------------------------------------- */

/**
 * Number of scheduler ticks between event k and event k+1 so that rate_hz
 * events happen in exactly tick_hz ticks (floor((r+1)*T/R) - floor(r*T/R)
 * with r = k mod R). E.g. 30 Hz at 1000 Hz ticks alternates 33/33/34 ms
 * instead of drifting to 30.3 Hz. Returns 0 if rate_hz == 0 or
 * rate_hz > tick_hz (invalid; callers must validate their configuration).
 */
uint32_t rs_period_ticks(uint32_t k, uint32_t rate_hz, uint32_t tick_hz);

#ifdef __cplusplus
}
#endif

#endif /* RS_CSI_CORE_H */
