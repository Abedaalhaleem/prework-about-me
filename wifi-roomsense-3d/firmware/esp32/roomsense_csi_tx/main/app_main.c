/*
 * roomsense_csi_tx: RoomSense ESP-NOW transmitter (ESP-IDF, pinned to v5.5.5).
 *
 * Derived from espressif/esp-csi examples/get-started/csi_send at commit
 * 8633d67152db2808f141cc1595970aa9cf406045 (SPDX-License-Identifier:
 * Apache-2.0; the example source also says "Public Domain (or CC0 licensed,
 * at your option)"). Differences from upstream, and why:
 *   - 20 MHz (HT20, MCS0 long GI) instead of HT40, matching roomsense_csi_rx;
 *   - payload is the 12-byte RSTX record (magic, version, tx_id, seq, rate)
 *     from docs/SERIAL_PROTOCOL.md instead of a bare 4-byte counter;
 *   - steady rate with xTaskDelayUntil (upstream: usleep after each send);
 *   - seq advances only when esp_now_send() accepted the frame, so gaps seen
 *     by the receiver mean over-the-air loss, not local send failures;
 *   - the MAC must be unicast and locally administered and is set before
 *     esp_wifi_start() (the esp_wifi_set_mac() docs require the interface to be
 *     disabled); upstream sets it after start;
 *   - NVS is never erased by the firmware.
 *
 * It only ever broadcasts its own small payload, at a bounded rate (<= 100 Hz).
 * The payload is neither encrypted nor authenticated, so the receiver treats
 * tx_seq as a diagnostic only.
 *
 * This file has NOT been compiled for any target yet (see README STATUS).
 */
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "rs_csi_core.h"

#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(5, 4, 0)
#error "roomsense_csi_tx needs ESP-IDF >= 5.4; it is written and documented against v5.5.5"
#endif

#if !(CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3 ||       \
      CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 ||      \
      CONFIG_IDF_TARGET_ESP32C61)
#error "unsupported target: use esp32, esp32s2, esp32s3, esp32c3, esp32c5, esp32c6 or esp32c61"
#endif

_Static_assert(CONFIG_RS_RATE_HZ >= 1 && CONFIG_RS_RATE_HZ <= 100, "RS_RATE_HZ out of range");
_Static_assert(CONFIG_RS_RATE_HZ <= CONFIG_FREERTOS_HZ,
               "RS_RATE_HZ must not exceed the FreeRTOS tick rate");
_Static_assert(CONFIG_RS_TX_ID >= 0 && CONFIG_RS_TX_ID <= 255, "RS_TX_ID out of range");

/* ESP-IDF docs: on ESP_ERR_ESPNOW_NO_MEM "delay a while before sending the next data". */
#define RS_NOMEM_BACKOFF_MS 10

static const char *TAG = "rs_tx";
static const uint8_t k_broadcast[ESP_NOW_ETH_ALEN] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

static void halt_forever(const char *why) __attribute__((noreturn));
static void halt_forever(const char *why)
{
    for (;;) {
        ESP_LOGE(TAG, "%s", why);
        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}

static void wifi_init_tx(const uint8_t mac[RS_MAC_LEN])
{
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, mac));
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
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

static void espnow_init_tx(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    esp_now_peer_info_t peer;
    memset(&peer, 0, sizeof peer);
    memcpy(peer.peer_addr, k_broadcast, ESP_NOW_ETH_ALEN);
    peer.channel = CONFIG_RS_CHANNEL;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false; /* broadcast frames cannot be encrypted */
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    /*
     * HT20 MCS0 (long GI): an OFDM HT frame, so the receiver gets LLTF (and,
     * if enabled, HT-LTF) CSI. The ESP-NOW default rate is 1 Mbps DSSS, whose
     * frames carry no OFDM training fields.
     */
    esp_now_rate_config_t rate_config = {
        .phymode = WIFI_PHY_MODE_HT20,
        .rate = WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(k_broadcast, &rate_config));
}

void app_main(void)
{
    uint8_t mac[RS_MAC_LEN];
    if (!rs_parse_mac(CONFIG_RS_TX_MAC, mac) || !rs_mac_is_unicast(mac) ||
        !rs_mac_is_locally_administered(mac)) {
        halt_forever("CONFIG_RS_TX_MAC must be a unicast, locally administered MAC "
                     "(xx:xx:xx:xx:xx:xx with bit 1 of the first octet set, e.g. 1a:00:00:00:00:00)");
    }

    const esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_flash_init: %s", esp_err_to_name(nvs_err));
        halt_forever("NVS is unusable (often left over from other firmware). This firmware "
                     "does not erase flash itself. If you accept losing what is stored in "
                     "NVS, erase it yourself (idf.py -p PORT erase-flash) and flash again.");
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_tx(mac);
    espnow_init_tx();

    char mac_str[RS_MAC_STR_LEN + 1];
    rs_format_mac(mac_str, mac);
    ESP_LOGI(TAG, "roomsense_csi_tx: channel %d (HT20), %d Hz, tx_id %d, mac %s",
             CONFIG_RS_CHANNEL, CONFIG_RS_RATE_HZ, CONFIG_RS_TX_ID, mac_str);

    uint32_t seq = 0;       /* advances only for frames esp_now_send() accepted */
    uint32_t slot = 0;      /* schedule index for rs_period_ticks() */
    uint32_t sent_ok = 0;   /* u32 counters wrap; they are only logged */
    uint32_t send_fail = 0;
    uint32_t no_mem = 0;
    uint32_t late = 0;      /* periods where we were already past the wake time */
    const TickType_t log_period = pdMS_TO_TICKS(CONFIG_RS_TX_LOG_INTERVAL_MS);
    TickType_t last_log = xTaskGetTickCount();
    TickType_t last_wake = last_log;

    for (;;) {
        uint8_t payload[RS_TX_PAYLOAD_LEN];
        (void)rs_build_tx_payload(payload, sizeof payload, (uint8_t)CONFIG_RS_TX_ID, seq,
                                  (uint16_t)CONFIG_RS_RATE_HZ);
        const esp_err_t err = esp_now_send(k_broadcast, payload, sizeof payload);
        if (err == ESP_OK) {
            sent_ok++;
            seq++;
        } else {
            send_fail++;
            if (err == ESP_ERR_ESPNOW_NO_MEM) {
                no_mem++;
                vTaskDelay(pdMS_TO_TICKS(RS_NOMEM_BACKOFF_MS));
            }
        }

        const TickType_t now = xTaskGetTickCount();
        if ((TickType_t)(now - last_log) >= log_period) {
            ESP_LOGI(TAG,
                     "seq=%" PRIu32 " sent_ok=%" PRIu32 " send_fail=%" PRIu32 " (no_mem=%" PRIu32
                     ") late=%" PRIu32 " free_heap=%" PRIu32,
                     seq, sent_ok, send_fail, no_mem, late, esp_get_free_heap_size());
            last_log = now;
        }

        const TickType_t period = (TickType_t)rs_period_ticks(slot++, CONFIG_RS_RATE_HZ,
                                                              (uint32_t)configTICK_RATE_HZ);
        if (xTaskDelayUntil(&last_wake, period) == pdFALSE) {
            /* Already late (e.g. after a NO_MEM back-off): restart the schedule
             * from now instead of sending a catch-up burst. */
            late++;
            last_wake = xTaskGetTickCount();
        }
    }
}
