/*
 * Writes the golden protocol files consumed by the Python parser tests:
 *
 *   gen_golden <lines.txt> <expected.jsonl>
 *
 * lines.txt      one machine line per case, exactly as the firmware formats
 *                it ("...*CRC8HEX\n"; the device's console adds '\r' on the wire).
 * expected.jsonl one JSON object per line (same order) with the input values,
 *                NA fields as null, so a parser can be checked field by field.
 *
 * All content is synthetic test data (see golden_cases.c).
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

#include "golden_cases.h"
#include "rs_csi_core.h"

static const char *const k_rx_names[RS_RX_FIELD_COUNT] = {
    "rssi",        "rate",      "sig_mode",   "mcs",     "cwb",
    "smoothing",   "not_sounding", "aggregation", "stbc", "fec_coding",
    "sgi",         "noise_floor",  "ampdu_cnt",  "channel", "secondary_channel",
    "ant",         "sig_len",   "rx_state",   "bb_format", "agc_gain",
    "fft_gain",
};

static void json_str_or_null(FILE *f, const char *key, const char *s)
{
    /* The formatter sends unsafe/NULL strings as NA; mirror that exactly. */
    if (rs_hello_str_is_safe(s)) {
        fprintf(f, ",\"%s\":\"%s\"", key, s);
    } else {
        fprintf(f, ",\"%s\":null", key);
    }
}

static void json_mac_or_null(FILE *f, const char *key, bool present, const uint8_t *mac)
{
    if (present) {
        char s[RS_MAC_STR_LEN + 1];
        rs_format_mac(s, mac);
        fprintf(f, ",\"%s\":\"%s\"", key, s);
    } else {
        fprintf(f, ",\"%s\":null", key);
    }
}

static void json_u32_or_null(FILE *f, const char *key, bool present, uint32_t v)
{
    if (present) {
        fprintf(f, ",\"%s\":%" PRIu32, key, v);
    } else {
        fprintf(f, ",\"%s\":null", key);
    }
}

static void json_i32_or_null(FILE *f, const char *key, bool present, int32_t v)
{
    if (present) {
        fprintf(f, ",\"%s\":%" PRId32, key, v);
    } else {
        fprintf(f, ",\"%s\":null", key);
    }
}

static void write_expected(FILE *f, const golden_case_t *c, const char *line, int len)
{
    /* line without the trailing '\n' (it contains no quotes or backslashes) */
    fprintf(f, "{\"name\":\"%s\",\"synthetic\":true,\"line\":\"%.*s\"", c->name, len - 1, line);
    fprintf(f, ",\"note\":\"%s\"", c->note);
    switch (c->kind) {
    case GOLDEN_HELLO: {
        const rs_hello_t *h = &c->hello;
        fprintf(f, ",\"tag\":\"RSHELLO\",\"version\":1");
        json_str_or_null(f, "fw_name", h->fw_name);
        json_str_or_null(f, "fw_version", h->fw_version);
        json_str_or_null(f, "idf_version", h->idf_version);
        json_str_or_null(f, "chip", h->chip);
        json_str_or_null(f, "chip_revision", h->chip_revision);
        json_mac_or_null(f, "sta_mac", h->has_sta_mac, h->sta_mac);
        json_str_or_null(f, "mode", h->mode);
        json_str_or_null(f, "ltf_config", h->ltf_config);
        json_i32_or_null(f, "channel", h->has_channel, h->channel);
        json_i32_or_null(f, "secondary_channel", h->has_secondary_channel, h->secondary_channel);
        json_mac_or_null(f, "tx_mac_filter", h->has_tx_mac_filter, h->tx_mac_filter);
        json_u32_or_null(f, "rate_hz", h->has_rate_hz, h->rate_hz);
        json_u32_or_null(f, "queue_depth", h->has_queue_depth, h->queue_depth);
        json_u32_or_null(f, "baud", h->has_baud, h->baud);
        json_str_or_null(f, "build_id", h->build_id);
        break;
    }
    case GOLDEN_CSI: {
        const rs_csi_record_t *r = &c->csi;
        fprintf(f, ",\"tag\":\"RSCSI\",\"version\":1");
        json_u32_or_null(f, "rec_seq", true, r->rec_seq);
        json_u32_or_null(f, "tx_seq", r->has_tx_seq, r->tx_seq);
        json_u32_or_null(f, "drops", true, r->drops);
        json_mac_or_null(f, "src_mac", true, r->src_mac);
        for (int i = 0; i < RS_RX_FIELD_COUNT; i++) {
            json_i32_or_null(f, k_rx_names[i], rs_csi_record_has_rx(r, (rs_rx_field_t)i), r->rx[i]);
        }
        json_u32_or_null(f, "rx_timestamp_us", r->has_rx_timestamp, r->rx_timestamp_us);
        fprintf(f, ",\"first_word_invalid\":%d", r->first_word_invalid ? 1 : 0);
        fprintf(f, ",\"len\":%u,\"csi\":[", (unsigned)r->len);
        for (unsigned i = 0; i < r->len; i++) {
            fprintf(f, "%s%d", i ? "," : "", (int)r->csi[i]);
        }
        fprintf(f, "]");
        break;
    }
    case GOLDEN_STAT: {
        const rs_stat_t *s = &c->stat;
        fprintf(f, ",\"tag\":\"RSSTAT\",\"version\":1");
        fprintf(f, ",\"uptime_ms\":%" PRIu64, s->uptime_ms);
        json_u32_or_null(f, "cb_total", true, s->cb_total);
        json_u32_or_null(f, "cb_filtered", true, s->cb_filtered);
        json_u32_or_null(f, "enqueued", true, s->enqueued);
        json_u32_or_null(f, "dropped_queue_full", true, s->dropped_queue_full);
        json_u32_or_null(f, "dropped_oversize", true, s->dropped_oversize);
        json_u32_or_null(f, "printed", true, s->printed);
        json_u32_or_null(f, "tx_ok", s->has_tx_ok, s->tx_ok);
        json_u32_or_null(f, "tx_fail", s->has_tx_fail, s->tx_fail);
        json_u32_or_null(f, "free_heap", true, s->free_heap);
        json_u32_or_null(f, "min_free_heap", true, s->min_free_heap);
        break;
    }
    }
    fprintf(f, "}\n");
}

int main(int argc, char **argv)
{
    if (argc != 3) {
        fprintf(stderr, "usage: %s <lines.txt> <expected.jsonl>\n", argv[0]);
        return 2;
    }
    static golden_case_t cases[GOLDEN_MAX_CASES];
    const size_t n = golden_build_cases(cases, GOLDEN_MAX_CASES);

    FILE *lines = fopen(argv[1], "wb");
    FILE *expected = fopen(argv[2], "wb");
    if (lines == NULL || expected == NULL) {
        perror("fopen");
        return 1;
    }
    static char buf[RS_CSI_LINE_MAX];
    for (size_t i = 0; i < n; i++) {
        int len = -1;
        switch (cases[i].kind) {
        case GOLDEN_HELLO:
            len = rs_format_hello(buf, sizeof buf, &cases[i].hello);
            break;
        case GOLDEN_CSI:
            len = rs_format_csi_line(buf, sizeof buf, &cases[i].csi);
            break;
        case GOLDEN_STAT:
            len = rs_format_stat(buf, sizeof buf, &cases[i].stat);
            break;
        }
        if (len <= 0) {
            fprintf(stderr, "formatting golden case %s failed\n", cases[i].name);
            return 1;
        }
        if (fwrite(buf, 1, (size_t)len, lines) != (size_t)len) {
            perror("fwrite");
            return 1;
        }
        write_expected(expected, &cases[i], buf, len);
    }
    if (fclose(lines) != 0 || fclose(expected) != 0) {
        perror("fclose");
        return 1;
    }
    printf("wrote %zu golden lines to %s and expected values to %s\n", n, argv[1], argv[2]);
    return 0;
}
