/*
 * rs_csi_core: see include/rs_csi_core.h for the contract.
 *
 * Implementation notes
 * --------------------
 * - Freestanding on purpose: no libc (no snprintf/memcpy/strlen). Integer
 *   formatting is done by hand so the output is byte-identical on newlib and
 *   glibc and so the formatters are cheap enough to run for every CSI record.
 * - Every write goes through rs_writer_t, which refuses to write the last byte
 *   of the buffer (reserved for the NUL) and latches an overflow flag. That is
 *   what guarantees "never writes past cap" for all formatters.
 */
#include "rs_csi_core.h"

/* ------------------------------------------------------------------------- */
/* CRC-32 (IEEE, reflected)                                                  */
/* ------------------------------------------------------------------------- */

/*
 * Nibble-wise table (16 entries): 64 bytes of rodata instead of 1 KiB, still
 * only two table lookups per byte. Entry i is the CRC-32 register update for
 * the 4-bit value i shifted out, i.e. the classic table for poly 0xEDB88320.
 */
static const uint32_t k_crc_nibble[16] = {
    0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu, 0x76DC4190u, 0x6B6B51F4u,
    0x4DB26158u, 0x5005713Cu, 0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
    0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu,
};

uint32_t rs_crc32_update(uint32_t crc, const void *data, size_t len)
{
    const uint8_t *p = (const uint8_t *)data;
    if (p == NULL) {
        return crc;
    }
    crc = ~crc;
    for (size_t i = 0; i < len; i++) {
        crc ^= p[i];
        crc = (crc >> 4) ^ k_crc_nibble[crc & 0x0Fu];
        crc = (crc >> 4) ^ k_crc_nibble[crc & 0x0Fu];
    }
    return ~crc;
}

uint32_t rs_crc32(const void *data, size_t len)
{
    return rs_crc32_update(0u, data, len);
}

/* ------------------------------------------------------------------------- */
/* Bounded writer                                                            */
/* ------------------------------------------------------------------------- */

static const char k_hex_lower[16] = {'0', '1', '2', '3', '4', '5', '6', '7',
                                     '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'};
static const char k_hex_upper[16] = {'0', '1', '2', '3', '4', '5', '6', '7',
                                     '8', '9', 'A', 'B', 'C', 'D', 'E', 'F'};

typedef struct {
    char *buf;
    size_t cap; /* total bytes available, including the NUL slot */
    size_t len; /* bytes written so far (excluding NUL) */
    bool ok;    /* false once anything failed to fit */
} rs_writer_t;

static void w_init(rs_writer_t *w, char *buf, size_t cap)
{
    w->buf = buf;
    w->cap = cap;
    w->len = 0;
    /* A buffer without room for at least the NUL can hold no line at all. */
    w->ok = (buf != NULL && cap > 0);
}

/* Room for n more characters while still leaving one byte for the NUL. */
static bool w_room(const rs_writer_t *w, size_t n)
{
    return w->ok && w->len < w->cap && (w->cap - 1u - w->len) >= n;
}

static void w_char(rs_writer_t *w, char c)
{
    if (!w_room(w, 1)) {
        w->ok = false;
        return;
    }
    w->buf[w->len++] = c;
}

static void w_bytes(rs_writer_t *w, const char *s, size_t n)
{
    if (!w_room(w, n)) {
        w->ok = false;
        return;
    }
    for (size_t i = 0; i < n; i++) {
        w->buf[w->len++] = s[i];
    }
}

/* NUL-terminated literal; only used with short compile-time strings. */
static void w_lit(rs_writer_t *w, const char *s)
{
    size_t n = 0;
    while (s[n] != '\0') {
        n++;
    }
    w_bytes(w, s, n);
}

static void w_u64(rs_writer_t *w, uint64_t v)
{
    char tmp[RS_U64_DIGITS];
    size_t n = 0;
    do {
        tmp[n++] = (char)('0' + (int)(v % 10u));
        v /= 10u;
    } while (v != 0u);
    if (!w_room(w, n)) {
        w->ok = false;
        return;
    }
    while (n > 0) {
        w->buf[w->len++] = tmp[--n];
    }
}

static void w_u32(rs_writer_t *w, uint32_t v)
{
    /* 32-bit division is much cheaper than 64-bit on the ESP32 cores. */
    char tmp[RS_U32_DIGITS];
    size_t n = 0;
    do {
        tmp[n++] = (char)('0' + (int)(v % 10u));
        v /= 10u;
    } while (v != 0u);
    if (!w_room(w, n)) {
        w->ok = false;
        return;
    }
    while (n > 0) {
        w->buf[w->len++] = tmp[--n];
    }
}

static void w_i32(rs_writer_t *w, int32_t v)
{
    if (v < 0) {
        w_char(w, '-');
        /* -(v + 1) + 1 avoids the signed overflow of -INT32_MIN. */
        w_u32(w, (uint32_t)(-(v + 1)) + 1u);
    } else {
        w_u32(w, (uint32_t)v);
    }
}

static void w_na(rs_writer_t *w)
{
    w_bytes(w, "NA", 2);
}

static void w_opt_u32(rs_writer_t *w, bool present, uint32_t v)
{
    if (present) {
        w_u32(w, v);
    } else {
        w_na(w);
    }
}

static void w_opt_i32(rs_writer_t *w, bool present, int32_t v)
{
    if (present) {
        w_i32(w, v);
    } else {
        w_na(w);
    }
}

static void w_mac(rs_writer_t *w, const uint8_t mac[RS_MAC_LEN])
{
    char s[RS_MAC_STR_LEN + 1];
    rs_format_mac(s, mac);
    w_bytes(w, s, RS_MAC_STR_LEN);
}

static void w_hex_i8(rs_writer_t *w, const int8_t *vals, size_t n)
{
    if (n > (SIZE_MAX / 2u) || !w_room(w, 2u * n)) {
        w->ok = false;
        return;
    }
    for (size_t i = 0; i < n; i++) {
        /* Two's-complement byte of the int8 value, as copied from buf. */
        const uint8_t b = (uint8_t)vals[i];
        w->buf[w->len++] = k_hex_lower[b >> 4];
        w->buf[w->len++] = k_hex_lower[b & 0x0Fu];
    }
}

/*
 * Append "*XXXXXXXX\n" (CRC of every byte written so far) and the NUL.
 * Returns the line length or -1; on failure the buffer is emptied so that a
 * truncated line can never be transmitted by accident.
 */
static int w_finish_line(rs_writer_t *w)
{
    if (w->ok) {
        const uint32_t crc = rs_crc32(w->buf, w->len);
        char suffix[RS_CRC_SUFFIX_LEN];
        suffix[0] = '*';
        for (int i = 0; i < 8; i++) {
            suffix[1 + i] = k_hex_upper[(crc >> (28 - 4 * i)) & 0x0Fu];
        }
        suffix[9] = '\n';
        w_bytes(w, suffix, sizeof suffix);
    }
    if (!w->ok || w->len > (size_t)INT32_MAX) {
        if (w->buf != NULL && w->cap > 0) {
            w->buf[0] = '\0';
        }
        return -1;
    }
    w->buf[w->len] = '\0'; /* w_room() always kept this byte free */
    return (int)w->len;
}

/* ------------------------------------------------------------------------- */
/* Hex / MAC / small helpers                                                 */
/* ------------------------------------------------------------------------- */

bool rs_hex_encode_i8(const int8_t *vals, size_t n, char *out, size_t cap)
{
    if (out == NULL || cap == 0) {
        return false;
    }
    if ((vals == NULL && n > 0) || n > (SIZE_MAX - 1u) / 2u || cap < 2u * n + 1u) {
        out[0] = '\0';
        return false;
    }
    rs_writer_t w;
    w_init(&w, out, cap);
    w_hex_i8(&w, vals, n);
    out[w.len] = '\0';
    return w.ok;
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

bool rs_parse_mac(const char *s, uint8_t mac[RS_MAC_LEN])
{
    if (s == NULL || mac == NULL) {
        return false;
    }
    uint8_t tmp[RS_MAC_LEN];
    for (int i = 0; i < RS_MAC_LEN; i++) {
        const char *p = s + 3 * i;
        const int hi = hex_value(p[0]);
        const int lo = (hi < 0) ? -1 : hex_value(p[1]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        const char sep = p[2];
        if ((i < RS_MAC_LEN - 1 && sep != ':') || (i == RS_MAC_LEN - 1 && sep != '\0')) {
            return false;
        }
        tmp[i] = (uint8_t)((hi << 4) | lo);
    }
    for (int i = 0; i < RS_MAC_LEN; i++) {
        mac[i] = tmp[i];
    }
    return true;
}

bool rs_mac_is_unicast(const uint8_t mac[RS_MAC_LEN])
{
    return mac != NULL && (mac[0] & 0x01u) == 0u;
}

bool rs_mac_is_locally_administered(const uint8_t mac[RS_MAC_LEN])
{
    return mac != NULL && (mac[0] & 0x02u) != 0u;
}

void rs_format_mac(char out[RS_MAC_STR_LEN + 1], const uint8_t mac[RS_MAC_LEN])
{
    for (int i = 0; i < RS_MAC_LEN; i++) {
        out[3 * i] = k_hex_lower[mac[i] >> 4];
        out[3 * i + 1] = k_hex_lower[mac[i] & 0x0Fu];
        out[3 * i + 2] = (i == RS_MAC_LEN - 1) ? '\0' : ':';
    }
}

bool rs_format_chip_revision(char *out, size_t cap, uint32_t revision)
{
    if (out == NULL || cap == 0) {
        return false;
    }
    rs_writer_t w;
    w_init(&w, out, cap);
    w_u32(&w, revision / 100u);
    w_char(&w, '.');
    w_u32(&w, revision % 100u);
    if (!w.ok) {
        out[0] = '\0';
        return false;
    }
    out[w.len] = '\0';
    return true;
}

bool rs_hello_str_is_safe(const char *s)
{
    if (s == NULL) {
        return false;
    }
    size_t n = 0;
    for (; s[n] != '\0'; n++) {
        const unsigned char c = (unsigned char)s[n];
        /* Printable, no space, and none of the protocol's separators. */
        if (n >= RS_HELLO_STR_MAX || c < 0x21u || c > 0x7Eu || c == ',' || c == '*') {
            return false;
        }
    }
    return n > 0;
}

static void w_hello_str(rs_writer_t *w, const char *s)
{
    if (rs_hello_str_is_safe(s)) {
        w_lit(w, s);
    } else {
        /* Unknown or unrepresentable => unavailable, never a guess. */
        w_na(w);
    }
}

/* ------------------------------------------------------------------------- */
/* Record helpers                                                            */
/* ------------------------------------------------------------------------- */

void rs_csi_record_reset(rs_csi_record_t *rec)
{
    if (rec == NULL) {
        return;
    }
    rec->rec_seq = 0;
    rec->tx_seq = 0;
    rec->has_tx_seq = false;
    rec->drops = 0;
    for (int i = 0; i < RS_MAC_LEN; i++) {
        rec->src_mac[i] = 0;
    }
    rec->rx_present = 0;
    for (int i = 0; i < RS_RX_FIELD_COUNT; i++) {
        rec->rx[i] = 0;
    }
    rec->has_rx_timestamp = false;
    rec->rx_timestamp_us = 0;
    rec->first_word_invalid = false;
    rec->len = 0;
}

void rs_csi_record_set_rx(rs_csi_record_t *rec, rs_rx_field_t f, int32_t value)
{
    if (rec == NULL || (int)f < 0 || (int)f >= (int)RS_RX_FIELD_COUNT) {
        return;
    }
    rec->rx[f] = value;
    rec->rx_present |= (1u << (unsigned)f);
}

bool rs_csi_record_has_rx(const rs_csi_record_t *rec, rs_rx_field_t f)
{
    if (rec == NULL || (int)f < 0 || (int)f >= (int)RS_RX_FIELD_COUNT) {
        return false;
    }
    return (rec->rx_present & (1u << (unsigned)f)) != 0u;
}

/* ------------------------------------------------------------------------- */
/* Formatters                                                                */
/* ------------------------------------------------------------------------- */

int rs_format_csi_line(char *out, size_t cap, const rs_csi_record_t *rec)
{
    rs_writer_t w;
    w_init(&w, out, cap);
    if (rec == NULL || rec->len > RS_MAX_CSI_LEN) {
        w.ok = false;
        return w_finish_line(&w);
    }

    w_lit(&w, "RSCSI,1,");
    w_u32(&w, rec->rec_seq); /* 2 */
    w_char(&w, ',');
    w_opt_u32(&w, rec->has_tx_seq, rec->tx_seq); /* 3 */
    w_char(&w, ',');
    w_u32(&w, rec->drops); /* 4 */
    w_char(&w, ',');
    w_mac(&w, rec->src_mac); /* 5 */

    /* 6..20: rssi .. secondary_channel */
    for (int f = 0; f < (int)RS_RX_FIRST_AFTER_TIMESTAMP; f++) {
        w_char(&w, ',');
        w_opt_i32(&w, rs_csi_record_has_rx(rec, (rs_rx_field_t)f), rec->rx[f]);
    }
    w_char(&w, ',');
    w_opt_u32(&w, rec->has_rx_timestamp, rec->rx_timestamp_us); /* 21 */
    /* 22..27: ant .. fft_gain */
    for (int f = (int)RS_RX_FIRST_AFTER_TIMESTAMP; f < (int)RS_RX_FIELD_COUNT; f++) {
        w_char(&w, ',');
        w_opt_i32(&w, rs_csi_record_has_rx(rec, (rs_rx_field_t)f), rec->rx[f]);
    }
    w_char(&w, ',');
    w_char(&w, rec->first_word_invalid ? '1' : '0'); /* 28 */
    w_char(&w, ',');
    w_u32(&w, rec->len); /* 29 */
    w_char(&w, ',');
    w_hex_i8(&w, rec->csi, rec->len); /* 30 */
    return w_finish_line(&w);
}

int rs_format_hello(char *out, size_t cap, const rs_hello_t *h)
{
    rs_writer_t w;
    w_init(&w, out, cap);
    if (h == NULL) {
        w.ok = false;
        return w_finish_line(&w);
    }
    w_lit(&w, "RSHELLO,1,");
    w_hello_str(&w, h->fw_name); /* 2 */
    w_char(&w, ',');
    w_hello_str(&w, h->fw_version); /* 3 */
    w_char(&w, ',');
    w_hello_str(&w, h->idf_version); /* 4 */
    w_char(&w, ',');
    w_hello_str(&w, h->chip); /* 5 */
    w_char(&w, ',');
    w_hello_str(&w, h->chip_revision); /* 6 */
    w_char(&w, ',');
    if (h->has_sta_mac) { /* 7 */
        w_mac(&w, h->sta_mac);
    } else {
        w_na(&w);
    }
    w_char(&w, ',');
    w_hello_str(&w, h->mode); /* 8 */
    w_char(&w, ',');
    w_hello_str(&w, h->ltf_config); /* 9 */
    w_char(&w, ',');
    w_opt_i32(&w, h->has_channel, h->channel); /* 10 */
    w_char(&w, ',');
    w_opt_i32(&w, h->has_secondary_channel, h->secondary_channel); /* 11 */
    w_char(&w, ',');
    if (h->has_tx_mac_filter) { /* 12 */
        w_mac(&w, h->tx_mac_filter);
    } else {
        w_na(&w);
    }
    w_char(&w, ',');
    w_opt_u32(&w, h->has_rate_hz, h->rate_hz); /* 13 */
    w_char(&w, ',');
    w_opt_u32(&w, h->has_queue_depth, h->queue_depth); /* 14 */
    w_char(&w, ',');
    w_opt_u32(&w, h->has_baud, h->baud); /* 15 */
    w_char(&w, ',');
    w_hello_str(&w, h->build_id); /* 16 */
    return w_finish_line(&w);
}

int rs_format_stat(char *out, size_t cap, const rs_stat_t *s)
{
    rs_writer_t w;
    w_init(&w, out, cap);
    if (s == NULL) {
        w.ok = false;
        return w_finish_line(&w);
    }
    w_lit(&w, "RSSTAT,1,");
    w_u64(&w, s->uptime_ms); /* 2 */
    w_char(&w, ',');
    w_u32(&w, s->cb_total); /* 3 */
    w_char(&w, ',');
    w_u32(&w, s->cb_filtered); /* 4 */
    w_char(&w, ',');
    w_u32(&w, s->enqueued); /* 5 */
    w_char(&w, ',');
    w_u32(&w, s->dropped_queue_full); /* 6 */
    w_char(&w, ',');
    w_u32(&w, s->dropped_oversize); /* 7 */
    w_char(&w, ',');
    w_u32(&w, s->printed); /* 8 */
    w_char(&w, ',');
    w_opt_u32(&w, s->has_tx_ok, s->tx_ok); /* 9 */
    w_char(&w, ',');
    w_opt_u32(&w, s->has_tx_fail, s->tx_fail); /* 10 */
    w_char(&w, ',');
    w_u32(&w, s->free_heap); /* 11 */
    w_char(&w, ',');
    w_u32(&w, s->min_free_heap); /* 12 */
    return w_finish_line(&w);
}

/* ------------------------------------------------------------------------- */
/* RSTX payload / ESP-NOW                                                    */
/* ------------------------------------------------------------------------- */

static const uint8_t k_tx_magic[4] = {'R', 'S', 'T', 'X'};

size_t rs_build_tx_payload(uint8_t *out, size_t cap, uint8_t tx_id, uint32_t seq,
                           uint16_t rate_hz)
{
    if (out == NULL || cap < RS_TX_PAYLOAD_LEN) {
        return 0;
    }
    out[0] = k_tx_magic[0];
    out[1] = k_tx_magic[1];
    out[2] = k_tx_magic[2];
    out[3] = k_tx_magic[3];
    out[4] = RS_TX_PAYLOAD_VERSION;
    out[5] = tx_id;
    /* Explicit little-endian so the wire format never depends on the CPU. */
    out[6] = (uint8_t)(seq & 0xFFu);
    out[7] = (uint8_t)((seq >> 8) & 0xFFu);
    out[8] = (uint8_t)((seq >> 16) & 0xFFu);
    out[9] = (uint8_t)((seq >> 24) & 0xFFu);
    out[10] = (uint8_t)(rate_hz & 0xFFu);
    out[11] = (uint8_t)((rate_hz >> 8) & 0xFFu);
    return RS_TX_PAYLOAD_LEN;
}

bool rs_parse_tx_payload_ex(const uint8_t *p, size_t n, uint32_t *seq, uint8_t *tx_id,
                            uint16_t *rate_hz)
{
    if (p == NULL || n < RS_TX_PAYLOAD_LEN) {
        return false;
    }
    if (p[0] != k_tx_magic[0] || p[1] != k_tx_magic[1] || p[2] != k_tx_magic[2] ||
        p[3] != k_tx_magic[3]) {
        return false;
    }
    if (p[4] != RS_TX_PAYLOAD_VERSION) {
        return false;
    }
    if (seq != NULL) {
        *seq = (uint32_t)p[6] | ((uint32_t)p[7] << 8) | ((uint32_t)p[8] << 16) |
               ((uint32_t)p[9] << 24);
    }
    if (tx_id != NULL) {
        *tx_id = p[5];
    }
    if (rate_hz != NULL) {
        *rate_hz = (uint16_t)((uint16_t)p[10] | (uint16_t)((uint16_t)p[11] << 8));
    }
    return true;
}

bool rs_parse_tx_payload(const uint8_t *p, size_t n, uint32_t *seq, uint8_t *tx_id)
{
    return rs_parse_tx_payload_ex(p, n, seq, tx_id, NULL);
}

bool rs_espnow_body(const uint8_t *payload, size_t payload_len, const uint8_t **body,
                    size_t *body_len)
{
    /* Offsets from the ESP-IDF v5.5.5 ESP-NOW "Frame Format" section. */
    enum {
        CATEGORY = 0,      /* 127: vendor specific */
        OUI1 = 1,          /* 18 fe 34 */
        ELEMENT_ID = 8,    /* 221: vendor specific element */
        ELEMENT_LEN = 9,   /* OUI(3) + type(1) + version(1) + body */
        OUI2 = 10,         /* 18 fe 34 */
        TYPE = 13,         /* 4: ESP-NOW */
        ELEMENT_FIXED = 5, /* OUI + type + version counted by ELEMENT_LEN */
    };
    static const uint8_t oui[3] = {0x18u, 0xFEu, 0x34u};

    if (payload == NULL || body == NULL || body_len == NULL ||
        payload_len < RS_ESPNOW_BODY_OFFSET) {
        return false;
    }
    if (payload[CATEGORY] != 127u || payload[ELEMENT_ID] != 221u || payload[TYPE] != 4u) {
        return false;
    }
    for (int i = 0; i < 3; i++) {
        if (payload[OUI1 + i] != oui[i] || payload[OUI2 + i] != oui[i]) {
            return false;
        }
    }
    if (payload[ELEMENT_LEN] < ELEMENT_FIXED) {
        return false;
    }
    const size_t n = (size_t)payload[ELEMENT_LEN] - ELEMENT_FIXED;
    if (n > payload_len - RS_ESPNOW_BODY_OFFSET) {
        return false; /* element claims more bytes than were captured */
    }
    *body = payload + RS_ESPNOW_BODY_OFFSET;
    *body_len = n;
    return true;
}

/* ------------------------------------------------------------------------- */
/* Timing                                                                    */
/* ------------------------------------------------------------------------- */

uint32_t rs_period_ticks(uint32_t k, uint32_t rate_hz, uint32_t tick_hz)
{
    if (rate_hz == 0u || rate_hz > tick_hz) {
        return 0u;
    }
    /*
     * The schedule t(k) = floor(k*T/R) repeats every R events (t(k+R) =
     * t(k) + T), so reducing k modulo R keeps the arithmetic in 64 bits with
     * no overflow and makes the counter's own 2^32 wrap harmless.
     */
    const uint64_t r = (uint64_t)(k % rate_hz);
    const uint64_t t0 = (r * tick_hz) / rate_hz;
    const uint64_t t1 = ((r + 1u) * tick_hz) / rate_hz;
    return (uint32_t)(t1 - t0);
}
