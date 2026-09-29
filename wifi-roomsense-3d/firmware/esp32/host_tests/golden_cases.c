/*
 * Golden (synthetic) protocol test vectors. See golden_cases.h.
 *
 * Changing any value here changes golden_rscsi_lines.txt: regenerate it with
 * `make -C firmware/esp32/host_tests golden` and re-run the Python parser tests.
 */
#include "golden_cases.h"

#include <stdint.h>
#include <string.h>

static const uint8_t k_tx_mac[RS_MAC_LEN] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
/* Locally administered example addresses (bit 1 of the first octet set). */
static const uint8_t k_rx_sta_mac[RS_MAC_LEN] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x01};
static const uint8_t k_ap_bssid[RS_MAC_LEN] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};

/*
 * Deterministic int8 pattern that covers negatives, zero, -128 and 127.
 * (seed*37 + i*53) mod 256 visits every byte value over 256 consecutive i
 * because 53 is odd.
 */
static void fill_pattern(rs_csi_record_t *r, uint16_t len, uint32_t seed)
{
    r->len = len;
    for (uint32_t i = 0; i < len; i++) {
        const uint32_t v = (seed * 37u + i * 53u) & 0xFFu;
        r->csi[i] = (int8_t)(v >= 128u ? (int32_t)v - 256 : (int32_t)v);
    }
}

/* Classic chip (ESP32/S2/S3/C3) rx_ctrl: every field present except the
 * C5/C6-only bb_format and the never-read agc/fft gains. */
static void set_classic_rx(rs_csi_record_t *r, int32_t rssi, int32_t rate, int32_t sig_mode,
                           int32_t mcs, int32_t cwb, int32_t stbc, int32_t noise_floor,
                           int32_t channel, int32_t secondary, uint32_t ts, int32_t sig_len)
{
    rs_csi_record_set_rx(r, RS_RX_RSSI, rssi);
    rs_csi_record_set_rx(r, RS_RX_RATE, rate);
    rs_csi_record_set_rx(r, RS_RX_SIG_MODE, sig_mode);
    rs_csi_record_set_rx(r, RS_RX_MCS, mcs);
    rs_csi_record_set_rx(r, RS_RX_CWB, cwb);
    rs_csi_record_set_rx(r, RS_RX_SMOOTHING, 1);
    rs_csi_record_set_rx(r, RS_RX_NOT_SOUNDING, 1);
    rs_csi_record_set_rx(r, RS_RX_AGGREGATION, 0);
    rs_csi_record_set_rx(r, RS_RX_STBC, stbc);
    rs_csi_record_set_rx(r, RS_RX_FEC_CODING, 0);
    rs_csi_record_set_rx(r, RS_RX_SGI, 0);
    rs_csi_record_set_rx(r, RS_RX_NOISE_FLOOR, noise_floor);
    rs_csi_record_set_rx(r, RS_RX_AMPDU_CNT, 0);
    rs_csi_record_set_rx(r, RS_RX_CHANNEL, channel);
    rs_csi_record_set_rx(r, RS_RX_SECONDARY_CHANNEL, secondary);
    r->has_rx_timestamp = true;
    r->rx_timestamp_us = ts;
    rs_csi_record_set_rx(r, RS_RX_ANT, 0);
    rs_csi_record_set_rx(r, RS_RX_SIG_LEN, sig_len);
    rs_csi_record_set_rx(r, RS_RX_RX_STATE, 0);
}

static golden_case_t *next_case(golden_case_t *out, size_t max, size_t *n, golden_kind_t kind,
                                const char *name, const char *note)
{
    if (*n >= max) {
        return NULL;
    }
    golden_case_t *c = &out[(*n)++];
    memset(c, 0, sizeof *c);
    c->kind = kind;
    c->name = name;
    c->note = note;
    rs_csi_record_reset(&c->csi);
    return c;
}

size_t golden_build_cases(golden_case_t *out, size_t max)
{
    size_t n = 0;
    golden_case_t *c;

    /* --- RSHELLO --- */
    c = next_case(out, max, &n, GOLDEN_HELLO, "hello_espnow_esp32s3",
                  "ESP-NOW receiver on a classic chip; build id unknown");
    if (c) {
        c->hello = (rs_hello_t){
            .fw_name = "roomsense_csi_rx", .fw_version = "0.1.0", .idf_version = "v5.5.5",
            .chip = "esp32s3", .chip_revision = "0.2", .has_sta_mac = true,
            .mode = "ESPNOW_RX", .ltf_config = "lltf_only", .has_channel = true, .channel = 11,
            .has_secondary_channel = true, .secondary_channel = 0, .has_tx_mac_filter = true,
            .has_rate_hz = true, .rate_hz = 25, .has_queue_depth = true, .queue_depth = 32,
            .has_baud = true, .baud = 921600, .build_id = NULL,
        };
        memcpy(c->hello.sta_mac, k_rx_sta_mac, RS_MAC_LEN);
        memcpy(c->hello.tx_mac_filter, k_tx_mac, RS_MAC_LEN);
    }

    c = next_case(out, max, &n, GOLDEN_HELLO, "hello_router_before_connect",
                  "router mode at boot: channel and AP BSSID not known yet (NA)");
    if (c) {
        c->hello = (rs_hello_t){
            .fw_name = "roomsense_csi_rx", .fw_version = "0.1.0", .idf_version = "v5.5.5",
            .chip = "esp32", .chip_revision = "3.1", .has_sta_mac = true,
            .mode = "ROUTER_PING", .ltf_config = "lltf_only", .has_channel = false,
            .has_secondary_channel = false, .has_tx_mac_filter = false, .has_rate_hz = true,
            .rate_hz = 25, .has_queue_depth = true, .queue_depth = 32, .has_baud = true,
            .baud = 921600, .build_id = "0123abc-dirty",
        };
        memcpy(c->hello.sta_mac, k_rx_sta_mac, RS_MAC_LEN);
    }

    c = next_case(out, max, &n, GOLDEN_HELLO, "hello_espnow_esp32c5",
                  "ESP32-C5 receiver reports ltf_config c5_default");
    if (c) {
        c->hello = (rs_hello_t){
            .fw_name = "roomsense_csi_rx", .fw_version = "0.1.0", .idf_version = "v5.5.5",
            .chip = "esp32c5", .chip_revision = "1.0", .has_sta_mac = true,
            .mode = "ESPNOW_RX", .ltf_config = "c5_default", .has_channel = true, .channel = 11,
            .has_secondary_channel = true, .secondary_channel = 0, .has_tx_mac_filter = true,
            .has_rate_hz = true, .rate_hz = 25, .has_queue_depth = true, .queue_depth = 32,
            .has_baud = true, .baud = 921600, .build_id = "0123abc",
        };
        memcpy(c->hello.sta_mac, k_rx_sta_mac, RS_MAC_LEN);
        memcpy(c->hello.tx_mac_filter, k_tx_mac, RS_MAC_LEN);
    }

    /* --- RSCSI --- */
    c = next_case(out, max, &n, GOLDEN_CSI, "csi_classic_espnow_len128",
                  "classic chip, ESP-NOW HT20 MCS0 frame, lltf_only => 128 values");
    if (c) {
        c->csi.rec_seq = 0;
        c->csi.has_tx_seq = true;
        c->csi.tx_seq = 1000;
        c->csi.drops = 0;
        memcpy(c->csi.src_mac, k_tx_mac, RS_MAC_LEN);
        set_classic_rx(&c->csi, -45, 11, 1, 0, 0, 0, -95, 11, 0, 123456789u, 55);
        fill_pattern(&c->csi, 128, 1);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_classic_router_rec_seq_near_wrap",
                  "router mode (tx_seq NA), non-HT frame, secondary above, rec_seq 2^32-2");
    if (c) {
        c->csi.rec_seq = 4294967294u;
        c->csi.has_tx_seq = false;
        c->csi.drops = 17;
        memcpy(c->csi.src_mac, k_ap_bssid, RS_MAC_LEN);
        set_classic_rx(&c->csi, -67, 11, 0, 0, 0, 0, -92, 6, 1, 4294967000u, 60);
        fill_pattern(&c->csi, 128, 2);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_classic_router_rec_seq_max",
                  "rec_seq 2^32-1 (last value before wrap); timestamp wrapped");
    if (c) {
        c->csi.rec_seq = 4294967295u;
        c->csi.has_tx_seq = false;
        c->csi.drops = 17;
        memcpy(c->csi.src_mac, k_ap_bssid, RS_MAC_LEN);
        set_classic_rx(&c->csi, -66, 11, 0, 0, 0, 0, -92, 6, 1, 250u, 60);
        fill_pattern(&c->csi, 128, 3);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_classic_router_rec_seq_wrapped",
                  "rec_seq wrapped to 0 (u32 rollover, not a reboot)");
    if (c) {
        c->csi.rec_seq = 0;
        c->csi.has_tx_seq = false;
        c->csi.drops = 18;
        memcpy(c->csi.src_mac, k_ap_bssid, RS_MAC_LEN);
        set_classic_rx(&c->csi, -68, 11, 0, 0, 0, 0, -92, 6, 1, 40250u, 60);
        fill_pattern(&c->csi, 128, 4);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_classic_len612_first_word_invalid",
                  "largest documented buffer (HT40 STBC, secondary below, all LTFs); "
                  "formatter capacity case, not produced by the lltf_only config");
    if (c) {
        c->csi.rec_seq = 123456;
        c->csi.has_tx_seq = true;
        c->csi.tx_seq = 4294967295u;
        c->csi.drops = 4294967295u;
        memcpy(c->csi.src_mac, k_tx_mac, RS_MAC_LEN);
        set_classic_rx(&c->csi, -60, 0, 1, 7, 1, 1, -90, 6, 2, 1000u, 1024);
        c->csi.first_word_invalid = true;
        fill_pattern(&c->csi, RS_MAX_CSI_LEN, 5);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_c5_ht20_len114",
                  "ESP32-C5 esp_wifi_rxctrl_t: classic-only fields NA, bb_format present");
    if (c) {
        c->csi.rec_seq = 42;
        c->csi.has_tx_seq = true;
        c->csi.tx_seq = 5;
        c->csi.drops = 0;
        memcpy(c->csi.src_mac, k_tx_mac, RS_MAC_LEN);
        rs_csi_record_set_rx(&c->csi, RS_RX_RSSI, -50);
        rs_csi_record_set_rx(&c->csi, RS_RX_RATE, 11);
        rs_csi_record_set_rx(&c->csi, RS_RX_NOISE_FLOOR, -96);
        rs_csi_record_set_rx(&c->csi, RS_RX_CHANNEL, 11);
        rs_csi_record_set_rx(&c->csi, RS_RX_SECONDARY_CHANNEL, 0);
        c->csi.has_rx_timestamp = true;
        c->csi.rx_timestamp_us = 98765u;
        rs_csi_record_set_rx(&c->csi, RS_RX_SIG_LEN, 55);
        rs_csi_record_set_rx(&c->csi, RS_RX_BB_FORMAT, 2);
        fill_pattern(&c->csi, 114, 6);
    }

    c = next_case(out, max, &n, GOLDEN_CSI, "csi_all_optional_na",
                  "every optional field NA (including timestamp and tx_seq)");
    if (c) {
        c->csi.rec_seq = 9;
        c->csi.has_tx_seq = false;
        c->csi.drops = 2;
        memcpy(c->csi.src_mac, k_tx_mac, RS_MAC_LEN);
        fill_pattern(&c->csi, 128, 7);
    }

    /* --- RSSTAT --- */
    c = next_case(out, max, &n, GOLDEN_STAT, "stat_espnow",
                  "ESP-NOW mode: tx_ok / tx_fail not applicable (NA)");
    if (c) {
        c->stat = (rs_stat_t){
            .uptime_ms = 5000, .cb_total = 1300, .cb_filtered = 1000, .enqueued = 125,
            .dropped_queue_full = 0, .dropped_oversize = 0, .printed = 125,
            .has_tx_ok = false, .has_tx_fail = false, .free_heap = 180000,
            .min_free_heap = 170000,
        };
    }

    c = next_case(out, max, &n, GOLDEN_STAT, "stat_router_long_uptime_near_wrap",
                  "uptime > 2^32 ms (u64) and u32 counters at/near their wrap point");
    if (c) {
        c->stat = (rs_stat_t){
            .uptime_ms = 5000000000ull, .cb_total = 4294967295u, .cb_filtered = 3000000000u,
            .enqueued = 1294967290u, .dropped_queue_full = 3, .dropped_oversize = 1,
            .printed = 1294967280u, .has_tx_ok = true, .tx_ok = 123456,
            .has_tx_fail = true, .tx_fail = 12, .free_heap = 150000, .min_free_heap = 120000,
        };
    }

    return n;
}
