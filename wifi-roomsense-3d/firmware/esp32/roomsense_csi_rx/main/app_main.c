/*
 * roomsense_csi_rx: RoomSense CSI receiver (ESP-IDF, pinned to v5.5.5).
 *
 * Derived from espressif/esp-csi examples/get-started/csi_recv and
 * csi_recv_router at commit 8633d67152db2808f141cc1595970aa9cf406045
 * (SPDX-License-Identifier: Apache-2.0; the example sources also say "Public
 * Domain (or CC0 licensed, at your option)"). What changed and why is listed
 * in firmware/esp32/README.md. In short:
 *   - the CSI callback only filters by MAC, copies and enqueues (the ESP-IDF
 *     Wi-Fi guide says it runs in the Wi-Fi task and must not do lengthy work);
 *     upstream printed every value with ets_printf inside the callback;
 *   - output is the CRC-framed RSHELLO/RSCSI/RSSTAT protocol of
 *     docs/SERIAL_PROTOCOL.md, one fwrite() per complete line;
 *   - raw int8 CSI, no gain compensation (no esp_csi_gain_ctrl);
 *   - ESP-NOW mode uses 20 MHz (HT20) instead of upstream's HT40.
 *
 * Data path:
 *   Wi-Fi task  csi_rx_cb(): MAC filter -> copy into rs_csi_record_t -> xQueueSend(.., 0)
 *   printer task (low priority): xQueueReceive -> rs_format_csi_line -> fwrite + fflush
 *
 * This file has NOT been compiled for any target yet (see README STATUS).
 */
#include <inttypes.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_chip_info.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#if CONFIG_RS_MODE_ESPNOW_RX
#include "esp_now.h"
#endif
#if CONFIG_RS_MODE_ROUTER_PING
#include "lwip/ip_addr.h"
#include "ping/ping_sock.h"
#include "protocol_examples_common.h"
#endif

#include "rs_csi_core.h"

/* ------------------------------------------------------------------------- */
/* Build-time checks                                                         */
/* ------------------------------------------------------------------------- */

#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(5, 4, 0)
#error "roomsense_csi_rx needs ESP-IDF >= 5.4; it is written and documented against v5.5.5"
#endif

#if !CONFIG_ESP_WIFI_CSI_ENABLED
#error "CONFIG_ESP_WIFI_CSI_ENABLED must be y (see sdkconfig.defaults); the target must support CSI"
#endif

#if !(CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3 ||       \
      CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 ||      \
      CONFIG_IDF_TARGET_ESP32C61)
#error "unsupported target: use esp32, esp32s2, esp32s3, esp32c3, esp32c5, esp32c6 or esp32c61"
#endif

/*
 * ESP32-C5/C6/C61 use esp_wifi_rxctrl_t (esp_wifi_he_types.h) and
 * wifi_csi_acquire_config_t; the classic chips use wifi_pkt_rx_ctrl_t and the
 * lltf/htltf wifi_csi_config_t (local/esp_wifi_types_native.h). ESP-IDF selects
 * the struct with CONFIG_SOC_WIFI_HE_SUPPORT; fail loudly if our target list
 * and that switch ever disagree.
 */
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define RS_HE_RXCTRL 1
/* Host side: only documented C5 layouts are accepted; C6/C61 are rejected
 * unless the user opts into a flagged assumption (see csi_layouts.py). */
#define RS_LTF_CONFIG "c5_default"
#else
#define RS_HE_RXCTRL 0
/* LLTF only (upstream csi_recv_router config) => 128 int8 values. */
#define RS_LTF_CONFIG "lltf_only"
#endif

#if defined(CONFIG_SOC_WIFI_HE_SUPPORT) && CONFIG_SOC_WIFI_HE_SUPPORT
#define RS_SOC_HE 1
#else
#define RS_SOC_HE 0
#endif
#if RS_SOC_HE != RS_HE_RXCTRL
#error "rx_ctrl layout assumption does not match CONFIG_SOC_WIFI_HE_SUPPORT"
#endif

#if CONFIG_RS_MODE_ROUTER_PING && !CONFIG_EXAMPLE_CONNECT_WIFI
#error "router mode connects over Wi-Fi: enable EXAMPLE_CONNECT_WIFI in menuconfig"
#endif

_Static_assert(CONFIG_RS_RATE_HZ >= 1 && CONFIG_RS_RATE_HZ <= 100, "RS_RATE_HZ out of range");
_Static_assert(CONFIG_RS_QUEUE_DEPTH >= 4 && CONFIG_RS_QUEUE_DEPTH <= 64,
               "RS_QUEUE_DEPTH out of range");
_Static_assert(RS_CSI_LINE_MAX >= RS_HELLO_LINE_MAX && RS_CSI_LINE_MAX >= RS_STAT_LINE_MAX,
               "the shared line buffer must hold every line type");

#ifndef RS_BUILD_ID
#define RS_BUILD_ID "NA"
#endif

#define RS_FW_NAME "roomsense_csi_rx"
#define RS_FW_VERSION "0.1.0"
#if CONFIG_RS_MODE_ESPNOW_RX
#define RS_MODE_NAME "ESPNOW_RX"
#else
#define RS_MODE_NAME "ROUTER_PING"
#endif

/*
 * Task layout. The Wi-Fi task runs at priority 23 and lwIP's tcpip task at
 * CONFIG_LWIP_TCPIP_TASK_PRIO (18 by default). The ping task (router mode)
 * sits above the printer so a busy serial port can slow printing (and fill the
 * queue, which is counted) but never delays the pings that generate CSI.
 */
#define RS_PRINTER_PRIORITY 4
#define RS_PING_PRIORITY 5
#define RS_PRINTER_STACK 4096
#define RS_PING_STACK 3072 /* upstream csi_recv_router value */
/* Sleep one tick after this many consecutive lines so the idle tasks (and the
 * task watchdog that watches them) always get CPU time, even if the serial
 * link is saturated and the queue never drains. */
#define RS_PRINTER_YIELD_EVERY 16
#define RS_DIAG_INTERVAL_MS 10000

static const char *TAG = "rs_rx";

/* ------------------------------------------------------------------------- */
/* State shared between tasks                                                */
/* ------------------------------------------------------------------------- */

/*
 * Counters. Written with relaxed atomics from the Wi-Fi task (callback), the
 * ping task and the printer; read by the printer. They are u32 and wrap by
 * design; the host unwraps them. Snapshots of several counters are not taken
 * atomically as a group, so an RSSTAT line may be off by a record or two
 * between fields.
 *
 * Identity (for a quiet queue): cb_total = cb_filtered + cb_invalid_arg +
 *   (enqueued + dropped_queue_full + dropped_oversize + dropped_invalid_csi).
 */
typedef struct {
    _Atomic uint32_t cb_total;            /* every CSI callback */
    _Atomic uint32_t cb_filtered;         /* other MAC: rejected before any copy */
    _Atomic uint32_t cb_invalid_arg;      /* NULL info/buf (diagnostic only) */
    _Atomic uint32_t enqueued;
    _Atomic uint32_t dropped_queue_full;  /* printer too slow */
    _Atomic uint32_t dropped_oversize;    /* len > RS_MAX_CSI_LEN, never truncated */
    _Atomic uint32_t dropped_invalid_csi; /* C5/C6/C61: rx_channel_estimate_info_vld == 0 */
    _Atomic uint32_t printed;             /* RSCSI lines fully written */
    _Atomic uint32_t write_errors;        /* fwrite/fflush reported an error */
    _Atomic uint32_t format_errors;       /* formatter refused a record (bug if > 0) */
    _Atomic uint32_t tx_ok;               /* router mode: ping replies */
    _Atomic uint32_t tx_fail;             /* router mode: ping timeouts */
} rs_counters_t;

static rs_counters_t s_cnt; /* static storage: zero-initialised, valid for atomics */

static QueueHandle_t s_queue;
static TaskHandle_t s_printer;

/* The only MAC whose CSI is exported. Written once before the CSI callback is
 * registered (ESP-NOW: from Kconfig; router: the AP BSSID), read-only after. */
static uint8_t s_filter_mac[RS_MAC_LEN];
static atomic_bool s_filter_ready;
static atomic_bool s_wifi_ready; /* channel can be queried */

/* Callback-only state. esp_wifi calls the CSI callback from its single Wi-Fi
 * task, so these are never accessed concurrently. The record is static rather
 * than on the stack to keep ~730 bytes off the Wi-Fi task's stack. */
static uint32_t s_rec_seq;
static rs_csi_record_t s_cb_record;

/* Printer-only state. */
static rs_csi_record_t s_print_record;
static char s_line[RS_CSI_LINE_MAX];
static char s_chip_rev[16];

static inline void cnt_inc(_Atomic uint32_t *c)
{
    atomic_fetch_add_explicit(c, 1u, memory_order_relaxed);
}

static inline uint32_t cnt_get(_Atomic uint32_t *c)
{
    return atomic_load_explicit(c, memory_order_relaxed);
}

/* ------------------------------------------------------------------------- */
/* CSI callback (runs in the Wi-Fi task: no logging, no blocking)            */
/* ------------------------------------------------------------------------- */

static void fill_rx_fields(rs_csi_record_t *rec, const wifi_pkt_rx_ctrl_t *rx)
{
    rs_csi_record_set_rx(rec, RS_RX_RSSI, (int32_t)rx->rssi);
    rs_csi_record_set_rx(rec, RS_RX_RATE, (int32_t)rx->rate);
    rs_csi_record_set_rx(rec, RS_RX_NOISE_FLOOR, (int32_t)rx->noise_floor);
    rs_csi_record_set_rx(rec, RS_RX_CHANNEL, (int32_t)rx->channel);
    rs_csi_record_set_rx(rec, RS_RX_SIG_LEN, (int32_t)rx->sig_len);
    rec->has_rx_timestamp = true;
    rec->rx_timestamp_us = (uint32_t)rx->timestamp;
#if RS_HE_RXCTRL
    /*
     * esp_wifi_rxctrl_t (v5.5.5) has no sig_mode, mcs, cwb, smoothing,
     * not_sounding, aggregation, stbc, fec_coding, sgi, ampdu_cnt or ant: those
     * stay NA. rx_state exists here, but protocol v1 defines field 24 as the
     * classic-chip rx_state only, so it stays NA as well.
     */
    rs_csi_record_set_rx(rec, RS_RX_SECONDARY_CHANNEL, (int32_t)rx->second);
    rs_csi_record_set_rx(rec, RS_RX_BB_FORMAT, (int32_t)rx->cur_bb_format);
#else
    rs_csi_record_set_rx(rec, RS_RX_SIG_MODE, (int32_t)rx->sig_mode);
    rs_csi_record_set_rx(rec, RS_RX_MCS, (int32_t)rx->mcs);
    rs_csi_record_set_rx(rec, RS_RX_CWB, (int32_t)rx->cwb);
    rs_csi_record_set_rx(rec, RS_RX_SMOOTHING, (int32_t)rx->smoothing);
    rs_csi_record_set_rx(rec, RS_RX_NOT_SOUNDING, (int32_t)rx->not_sounding);
    rs_csi_record_set_rx(rec, RS_RX_AGGREGATION, (int32_t)rx->aggregation);
    rs_csi_record_set_rx(rec, RS_RX_STBC, (int32_t)rx->stbc);
    rs_csi_record_set_rx(rec, RS_RX_FEC_CODING, (int32_t)rx->fec_coding);
    rs_csi_record_set_rx(rec, RS_RX_SGI, (int32_t)rx->sgi);
    rs_csi_record_set_rx(rec, RS_RX_AMPDU_CNT, (int32_t)rx->ampdu_cnt);
    rs_csi_record_set_rx(rec, RS_RX_SECONDARY_CHANNEL, (int32_t)rx->secondary_channel);
    rs_csi_record_set_rx(rec, RS_RX_ANT, (int32_t)rx->ant);
    rs_csi_record_set_rx(rec, RS_RX_RX_STATE, (int32_t)rx->rx_state);
#endif
    /* agc_gain / fft_gain (fields 26, 27) are not read in protocol v1: NA. */
}

#if CONFIG_RS_MODE_ESPNOW_RX
/*
 * tx_seq comes from the RSTX payload inside the ESP-NOW body. esp-csi's
 * csi_recv reads *(uint32_t *)(info->payload + 15) unconditionally; here the
 * frame structure and every length is checked first (rs_espnow_body), so a
 * short or foreign frame gives tx_seq = NA instead of reading out of bounds.
 */
static void read_tx_seq(rs_csi_record_t *rec, const wifi_csi_info_t *info)
{
    const uint8_t *body = NULL;
    size_t body_len = 0;
    uint32_t tx_seq = 0;
    if (info->payload != NULL &&
        rs_espnow_body(info->payload, (size_t)info->payload_len, &body, &body_len) &&
        rs_parse_tx_payload(body, body_len, &tx_seq, NULL)) {
        rec->tx_seq = tx_seq;
        rec->has_tx_seq = true;
    }
}
#endif

static void csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;
    cnt_inc(&s_cnt.cb_total);
    if (info == NULL || info->buf == NULL) {
        cnt_inc(&s_cnt.cb_invalid_arg);
        return;
    }

    /* MAC filter FIRST: CSI from any other device is never copied or parsed. */
    if (memcmp(info->mac, s_filter_mac, RS_MAC_LEN) != 0) {
        cnt_inc(&s_cnt.cb_filtered);
        return;
    }

    /* Every accepted callback gets a sequence number before any drop decision,
     * so every device-side drop is visible to the host as a rec_seq gap.
     * uint32_t arithmetic wraps at 2^32 by design. */
    const uint32_t seq = s_rec_seq++;

    if (info->len > RS_MAX_CSI_LEN) {
        /* Never truncate: a cut buffer would look like a different layout. */
        cnt_inc(&s_cnt.dropped_oversize);
        return;
    }
#if RS_HE_RXCTRL
    /* ESP-IDF v5.5.5 Wi-Fi guide (ESP32-C5): the CSI data is invalid unless
     * rx_ctrl.rx_channel_estimate_info_vld is 1. */
    if (!info->rx_ctrl.rx_channel_estimate_info_vld) {
        cnt_inc(&s_cnt.dropped_invalid_csi);
        return;
    }
#endif

    rs_csi_record_t *rec = &s_cb_record;
    rs_csi_record_reset(rec);
    rec->rec_seq = seq;
    rec->drops = cnt_get(&s_cnt.dropped_queue_full) + cnt_get(&s_cnt.dropped_oversize);
    memcpy(rec->src_mac, info->mac, RS_MAC_LEN);
    fill_rx_fields(rec, &info->rx_ctrl);
    rec->first_word_invalid = info->first_word_invalid;
    rec->len = info->len;
    memcpy(rec->csi, info->buf, info->len);
#if CONFIG_RS_MODE_ESPNOW_RX
    read_tx_seq(rec, info);
#endif
    /* Timeout 0: the Wi-Fi task must never wait for the serial port. */
    if (xQueueSend(s_queue, rec, 0) == pdTRUE) {
        cnt_inc(&s_cnt.enqueued);
    } else {
        cnt_inc(&s_cnt.dropped_queue_full);
    }
}

/* ------------------------------------------------------------------------- */
/* Printer task                                                              */
/* ------------------------------------------------------------------------- */

/*
 * One fwrite per complete line: newlib holds the stdout lock for the whole
 * call, so ESP_LOG output from other tasks cannot land inside a machine line.
 * (ets_printf/esp_rom_printf bypass that lock and are not used here; any ROM
 * or driver output that still interleaves is caught by the line CRC.)
 */
static bool write_line(const char *line, int n)
{
    if (n <= 0) {
        cnt_inc(&s_cnt.format_errors);
        return false;
    }
    const size_t len = (size_t)n;
    const size_t written = fwrite(line, 1, len, stdout);
    const int flushed = fflush(stdout);
    if (written != len || flushed != 0) {
        cnt_inc(&s_cnt.write_errors);
        return false;
    }
    return true;
}

static void emit_hello(void)
{
    rs_hello_t h;
    memset(&h, 0, sizeof h);
    h.fw_name = RS_FW_NAME;
    h.fw_version = RS_FW_VERSION;
    h.idf_version = esp_get_idf_version();
    h.chip = CONFIG_IDF_TARGET;

    esp_chip_info_t chip;
    esp_chip_info(&chip);
    h.chip_revision =
        rs_format_chip_revision(s_chip_rev, sizeof s_chip_rev, chip.revision) ? s_chip_rev : NULL;

    /* This firmware never changes its own station MAC, so the eFuse-derived
     * address is the one on air. */
    if (esp_read_mac(h.sta_mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        h.has_sta_mac = true;
    }
    h.mode = RS_MODE_NAME;
    h.ltf_config = RS_LTF_CONFIG;

    if (atomic_load(&s_wifi_ready)) {
        uint8_t primary = 0;
        wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
        if (esp_wifi_get_channel(&primary, &second) == ESP_OK) {
            h.has_channel = true;
            h.channel = (int32_t)primary;
            h.has_secondary_channel = true;
            h.secondary_channel = (int32_t)second; /* 0 none, 1 above, 2 below */
        }
    }
    if (atomic_load(&s_filter_ready)) {
        h.has_tx_mac_filter = true;
        memcpy(h.tx_mac_filter, s_filter_mac, RS_MAC_LEN);
    }
    h.has_rate_hz = true;
    h.rate_hz = CONFIG_RS_RATE_HZ;
    h.has_queue_depth = true;
    h.queue_depth = CONFIG_RS_QUEUE_DEPTH;
#if CONFIG_ESP_CONSOLE_UART
    h.has_baud = true;
    h.baud = CONFIG_ESP_CONSOLE_UART_BAUDRATE;
#endif
    /* USB-Serial-JTAG / USB CDC consoles have no meaningful baud rate: NA. */
    h.build_id = (strcmp(RS_BUILD_ID, "NA") == 0) ? NULL : RS_BUILD_ID;

    (void)write_line(s_line, rs_format_hello(s_line, sizeof s_line, &h));
}

static void emit_stat(void)
{
    rs_stat_t s;
    memset(&s, 0, sizeof s);
    s.uptime_ms = (uint64_t)(esp_timer_get_time() / 1000);
    s.cb_total = cnt_get(&s_cnt.cb_total);
    s.cb_filtered = cnt_get(&s_cnt.cb_filtered);
    s.enqueued = cnt_get(&s_cnt.enqueued);
    s.dropped_queue_full = cnt_get(&s_cnt.dropped_queue_full);
    s.dropped_oversize = cnt_get(&s_cnt.dropped_oversize);
    s.printed = cnt_get(&s_cnt.printed);
#if CONFIG_RS_MODE_ROUTER_PING
    s.has_tx_ok = true;
    s.tx_ok = cnt_get(&s_cnt.tx_ok);
    s.has_tx_fail = true;
    s.tx_fail = cnt_get(&s_cnt.tx_fail);
#endif
    s.free_heap = esp_get_free_heap_size();
    s.min_free_heap = esp_get_minimum_free_heap_size();
    (void)write_line(s_line, rs_format_stat(s_line, sizeof s_line, &s));
}

/* Diagnostics that protocol v1 has no field for go to the log (a diagnostic
 * line for the host), only when something is non-zero. */
static void log_diagnostics(void)
{
    const uint32_t invalid_arg = cnt_get(&s_cnt.cb_invalid_arg);
    const uint32_t invalid_csi = cnt_get(&s_cnt.dropped_invalid_csi);
    const uint32_t write_errors = cnt_get(&s_cnt.write_errors);
    const uint32_t format_errors = cnt_get(&s_cnt.format_errors);
    if (invalid_arg || invalid_csi || write_errors || format_errors) {
        ESP_LOGW(TAG,
                 "diag: cb_invalid_arg=%" PRIu32 " dropped_invalid_csi=%" PRIu32
                 " write_errors=%" PRIu32 " format_errors=%" PRIu32,
                 invalid_arg, invalid_csi, write_errors, format_errors);
    }
}

static TickType_t ticks_until(TickType_t now, TickType_t last, TickType_t period)
{
    const TickType_t elapsed = (TickType_t)(now - last); /* wrap-safe */
    return (elapsed >= period) ? 0 : (TickType_t)(period - elapsed);
}

static void printer_task(void *arg)
{
    (void)arg;
    const TickType_t hello_period = pdMS_TO_TICKS(CONFIG_RS_HELLO_INTERVAL_MS);
    const TickType_t stat_period = pdMS_TO_TICKS(CONFIG_RS_STAT_INTERVAL_MS);
    const TickType_t diag_period = pdMS_TO_TICKS(RS_DIAG_INTERVAL_MS);
    TickType_t last_hello = xTaskGetTickCount();
    TickType_t last_stat = last_hello;
    TickType_t last_diag = last_hello;
    uint32_t lines_since_yield = 0;

    emit_hello(); /* at boot, before Wi-Fi is up: channel/filter are NA */

    for (;;) {
        const TickType_t now = xTaskGetTickCount();
        const TickType_t to_hello = ticks_until(now, last_hello, hello_period);
        const TickType_t to_stat = ticks_until(now, last_stat, stat_period);
        const TickType_t wait = (to_hello < to_stat) ? to_hello : to_stat;

        if (xQueueReceive(s_queue, &s_print_record, wait) == pdTRUE) {
            if (write_line(s_line, rs_format_csi_line(s_line, sizeof s_line, &s_print_record))) {
                cnt_inc(&s_cnt.printed);
            }
            if (++lines_since_yield >= RS_PRINTER_YIELD_EVERY) {
                lines_since_yield = 0;
                vTaskDelay(1);
            }
        } else {
            lines_since_yield = 0;
        }

        const TickType_t t = xTaskGetTickCount();
        /* app_main notifies us when the channel / filter MAC become known. */
        if (ulTaskNotifyTake(pdTRUE, 0) > 0 || (TickType_t)(t - last_hello) >= hello_period) {
            emit_hello();
            last_hello = t;
        }
        if ((TickType_t)(t - last_stat) >= stat_period) {
            emit_stat();
            last_stat = t;
        }
        if ((TickType_t)(t - last_diag) >= diag_period) {
            log_diagnostics();
            last_diag = t;
        }
    }
}

/* ------------------------------------------------------------------------- */
/* Wi-Fi / CSI setup                                                         */
/* ------------------------------------------------------------------------- */

#if CONFIG_RS_MODE_ESPNOW_RX
/*
 * Mirrors esp-csi csi_recv's wifi_init() (STA mode, RAM storage, no power
 * save, fixed channel) with two deliberate differences:
 *   - 20 MHz (HT20, secondary channel NONE) instead of HT40, so the classic
 *     LLTF layout is the documented "secondary channel none" row;
 *   - the receiver keeps its own factory MAC. Upstream csi_recv also sets the
 *     receiver's MAC to the transmitter's 1a:00:00:00:00:00, which would put
 *     two stations with one address on air.
 */
static void wifi_init_espnow_rx(void)
{
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
#if RS_HE_RXCTRL
    /* Upstream order for C5/C6/C61: start first, then band/protocol/bandwidth. */
    ESP_ERROR_CHECK(esp_wifi_start());
    const esp_err_t band_err = esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY);
    if (band_err != ESP_OK) {
        ESP_LOGW(TAG, "esp_wifi_set_band_mode(2G_ONLY): %s", esp_err_to_name(band_err));
    }
    wifi_protocols_t protocols = {
        .ghz_2g = WIFI_PROTOCOL_11N,
#if CONFIG_IDF_TARGET_ESP32C5
        .ghz_5g = WIFI_PROTOCOL_11N,
#endif
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protocols));
    wifi_bandwidths_t bandwidths = {
        .ghz_2g = WIFI_BW_HT20,
#if CONFIG_IDF_TARGET_ESP32C5
        .ghz_5g = WIFI_BW_HT20,
#endif
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_RS_CHANNEL, WIFI_SECOND_CHAN_NONE));
}
#endif

static void csi_init(void)
{
#if CONFIG_RS_MODE_ESPNOW_RX
    /* As upstream csi_recv: promiscuous mode so the station, which is not
     * associated, still receives the transmitter's frames. No promiscuous RX
     * callback is registered; only the CSI callback sees the frames. */
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
#endif

    /* Exactly the esp-csi csi_recv_router configuration for every target. */
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable = true,
        .acquire_csi_legacy = true,
        .acquire_csi_force_lltf = false,
        .acquire_csi_ht20 = true,
        .acquire_csi_ht40 = true,
        .acquire_csi_vht = false,
        .acquire_csi_su = false,
        .acquire_csi_mu = false,
        .acquire_csi_dcm = false,
        .acquire_csi_beamformed = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg = 0,
        .dump_ack_en = false,
        .reserved = false,
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config_t csi_config = {
        .enable = true,
        .acquire_csi_legacy = true,
        .acquire_csi_ht20 = true,
        .acquire_csi_ht40 = true,
        .acquire_csi_su = false,
        .acquire_csi_mu = false,
        .acquire_csi_dcm = false,
        .acquire_csi_beamformed = false,
        .acquire_csi_he_stbc = 2,
        .val_scale_cfg = false,
        .dump_ack_en = false,
        .reserved = false,
    };
#else
    /* LLTF only => 128 values (host ltf_config "lltf_only"). */
    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = true,
        .channel_filter_en = true,
        .manu_scale = true,
        .shift = 1, /* upstream writes `true`, i.e. 1 for this uint8_t field */
        .dump_ack_en = false,
    };
#endif
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

#if CONFIG_RS_MODE_ROUTER_PING
static void on_ping_success(esp_ping_handle_t hdl, void *args)
{
    (void)hdl;
    (void)args;
    cnt_inc(&s_cnt.tx_ok);
}

static void on_ping_timeout(esp_ping_handle_t hdl, void *args)
{
    (void)hdl;
    (void)args;
    cnt_inc(&s_cnt.tx_fail);
}

/*
 * As esp-csi csi_recv_router: ICMP echo to the gateway so the AP sends frames
 * (and therefore CSI) at a steady rate. Only reads the IP configuration the
 * AP handed out; nothing on the router is changed.
 */
static void ping_router_start(void)
{
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip_info;
    memset(&ip_info, 0, sizeof ip_info);
    ESP_ERROR_CHECK(netif == NULL ? ESP_ERR_NOT_FOUND : esp_netif_get_ip_info(netif, &ip_info));

    esp_ping_config_t ping_config = ESP_PING_DEFAULT_CONFIG();
    ping_config.count = ESP_PING_COUNT_INFINITE;
    /* Integer milliseconds: 25 Hz -> 40 ms exactly; e.g. 30 Hz -> 33 ms (30.3 Hz). */
    ping_config.interval_ms = 1000u / (uint32_t)CONFIG_RS_RATE_HZ;
    /* A lost reply otherwise stalls the ping loop for the default 1 s; with
     * timeout == interval the schedule stays steady and a late reply simply
     * counts as tx_fail. */
    ping_config.timeout_ms = ping_config.interval_ms;
    ping_config.data_size = 1;
    ping_config.task_stack_size = RS_PING_STACK;
    ping_config.task_prio = RS_PING_PRIORITY;
    ip_addr_set_ip4_u32_val(ping_config.target_addr, ip_info.gw.addr);

    const esp_ping_callbacks_t cbs = {
        .cb_args = NULL,
        .on_ping_success = on_ping_success,
        .on_ping_timeout = on_ping_timeout,
        .on_ping_end = NULL,
    };
    esp_ping_handle_t ping = NULL;
    ESP_ERROR_CHECK(esp_ping_new_session(&ping_config, &cbs, &ping));
    ESP_ERROR_CHECK(esp_ping_start(ping));
    ESP_LOGI(TAG, "pinging gateway " IPSTR " every %" PRIu32 " ms", IP2STR(&ip_info.gw),
             ping_config.interval_ms);
}
#endif

/* ------------------------------------------------------------------------- */
/* Startup                                                                   */
/* ------------------------------------------------------------------------- */

/* Stop here (without rebooting in a loop) and keep explaining why. */
static void halt_forever(const char *why) __attribute__((noreturn));
static void halt_forever(const char *why)
{
    for (;;) {
        ESP_LOGE(TAG, "%s", why);
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}

void app_main(void)
{
    /* Queue and printer first, so RSHELLO/RSSTAT appear even if setup stops. */
    s_queue = xQueueCreate(CONFIG_RS_QUEUE_DEPTH, sizeof(rs_csi_record_t));
    if (s_queue == NULL) {
        halt_forever("cannot allocate the CSI queue; lower RS_QUEUE_DEPTH");
    }
    if (xTaskCreate(printer_task, "rs_printer", RS_PRINTER_STACK, NULL, RS_PRINTER_PRIORITY,
                    &s_printer) != pdPASS) {
        halt_forever("cannot create the printer task");
    }

    /* The firmware never erases flash on its own (upstream csi_recv erases NVS
     * when nvs_flash_init reports a full or newer-format partition). */
    const esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_flash_init: %s", esp_err_to_name(nvs_err));
        halt_forever("NVS is unusable (often left over from other firmware). This firmware "
                     "does not erase flash itself. If you accept losing what is stored in "
                     "NVS, erase it yourself (idf.py -p PORT erase-flash) and flash again.");
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

#if CONFIG_RS_MODE_ESPNOW_RX
    if (!rs_parse_mac(CONFIG_RS_TX_MAC, s_filter_mac) || !rs_mac_is_unicast(s_filter_mac)) {
        halt_forever("CONFIG_RS_TX_MAC is not a unicast MAC of the form xx:xx:xx:xx:xx:xx");
    }
    atomic_store(&s_filter_ready, true);
    wifi_init_espnow_rx();
    /* Upstream csi_recv initialises ESP-NOW on the receiver too. The receiver
     * never transmits, so no peer, PMK or rate is configured. */
    ESP_ERROR_CHECK(esp_now_init());
#else
    ESP_ERROR_CHECK(example_connect());
    /* Deviation from upstream: no modem sleep, because rx_ctrl.timestamp is
     * only precise without it (esp_wifi_types_native.h). Only this board's own
     * power-save setting changes. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    wifi_ap_record_t ap_info;
    memset(&ap_info, 0, sizeof ap_info);
    ESP_ERROR_CHECK(esp_wifi_sta_get_ap_info(&ap_info));
    memcpy(s_filter_mac, ap_info.bssid, RS_MAC_LEN);
    atomic_store(&s_filter_ready, true);
#endif
    atomic_store(&s_wifi_ready, true);

    csi_init();
#if CONFIG_RS_MODE_ROUTER_PING
    ping_router_start();
#endif

    char filter[RS_MAC_STR_LEN + 1];
    rs_format_mac(filter, s_filter_mac);
    ESP_LOGI(TAG, "%s %s: mode %s, ltf_config %s, exporting CSI only from %s", RS_FW_NAME,
             RS_FW_VERSION, RS_MODE_NAME, RS_LTF_CONFIG, filter);
    /* Re-announce RSHELLO now that channel and filter MAC are known. */
    xTaskNotifyGive(s_printer);
}
