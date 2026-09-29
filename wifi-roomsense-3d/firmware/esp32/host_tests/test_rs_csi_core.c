/*
 * Host unit tests for components/rs_csi_core (the exact sources that run on
 * the ESP32). Build and run with `make -C firmware/esp32/host_tests test`.
 *
 * Usage: test_rs_csi_core [golden_lines.txt]
 *   If the golden file path is given and the file exists, the test also checks
 *   that the committed golden file still matches the current formatter output.
 *
 * Exit status is non-zero if any check fails. The summary line reports the
 * real number of test functions and individual checks that passed/failed.
 */
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "golden_cases.h"
#include "rs_csi_core.h"

/* ------------------------------------------------------------------------- */
/* Minimal test harness                                                      */
/* ------------------------------------------------------------------------- */

static int g_checks_passed;
static int g_checks_failed;
static int g_current_failed;

#define CHECK(cond)                                                                   \
    do {                                                                              \
        if (cond) {                                                                   \
            g_checks_passed++;                                                        \
        } else {                                                                      \
            g_checks_failed++;                                                        \
            g_current_failed++;                                                       \
            fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                             \
    } while (0)

#define CHECK_STREQ(actual, expected)                                                 \
    do {                                                                              \
        const char *a_ = (actual);                                                    \
        const char *e_ = (expected);                                                  \
        if (a_ != NULL && e_ != NULL && strcmp(a_, e_) == 0) {                        \
            g_checks_passed++;                                                        \
        } else {                                                                      \
            g_checks_failed++;                                                        \
            g_current_failed++;                                                       \
            fprintf(stderr, "  FAIL %s:%d:\n    got      \"%s\"\n    expected \"%s\"\n", \
                    __FILE__, __LINE__, a_ ? a_ : "(null)", e_ ? e_ : "(null)");      \
        }                                                                             \
    } while (0)

typedef void (*test_fn)(void);

static const char *g_golden_path;

/* ------------------------------------------------------------------------- */
/* Independent references                                                    */
/* ------------------------------------------------------------------------- */

/* Bit-by-bit CRC-32 (IEEE), deliberately different from the nibble table. */
static uint32_t ref_crc32(const void *data, size_t len)
{
    const uint8_t *p = data;
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= p[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 1u) ? (crc >> 1) ^ 0xEDB88320u : (crc >> 1);
        }
    }
    return ~crc;
}

/* body + "*%08X\n" using the reference CRC. */
static void ref_line(char *out, size_t cap, const char *body)
{
    snprintf(out, cap, "%s*%08" PRIX32 "\n", body, ref_crc32(body, strlen(body)));
}

static uint32_t g_lcg = 12345u;
static uint32_t lcg(void)
{
    g_lcg = g_lcg * 1664525u + 1013904223u;
    return g_lcg;
}

/*
 * Split a machine line at ',' up to '*', verify its CRC with the reference
 * implementation and return the number of fields (or -1 if malformed).
 */
static int split_and_verify(const char *line, char fields[][2048], int max_fields)
{
    const char *star = strrchr(line, '*');
    if (star == NULL || strlen(star) != 10 || star[9] != '\n') {
        return -1;
    }
    char crc_hex[9];
    memcpy(crc_hex, star + 1, 8);
    crc_hex[8] = '\0';
    char expect_hex[9];
    snprintf(expect_hex, sizeof expect_hex, "%08" PRIX32,
             ref_crc32(line, (size_t)(star - line)));
    if (strcmp(crc_hex, expect_hex) != 0) {
        return -1;
    }
    int n = 0;
    const char *p = line;
    while (p <= star && n < max_fields) {
        const char *end = p;
        while (end < star && *end != ',') {
            end++;
        }
        const size_t flen = (size_t)(end - p);
        if (flen >= 2048) {
            return -1;
        }
        memcpy(fields[n], p, flen);
        fields[n][flen] = '\0';
        n++;
        if (end == star) {
            break;
        }
        p = end + 1;
    }
    return n;
}

static char g_fields[40][2048];

/* ------------------------------------------------------------------------- */
/* Record builders                                                           */
/* ------------------------------------------------------------------------- */

static void make_classic_record(rs_csi_record_t *r)
{
    static const uint8_t mac[6] = {0x1a, 0, 0, 0, 0, 0};
    rs_csi_record_reset(r);
    r->rec_seq = 7;
    r->has_tx_seq = true;
    r->tx_seq = 42;
    r->drops = 3;
    memcpy(r->src_mac, mac, 6);
    const int32_t vals[] = {-45, 11, 1, 0, 0, 1, 1, 0, 0, 0, 0, -95, 0, 11, 0};
    for (int i = 0; i < 15; i++) {
        rs_csi_record_set_rx(r, (rs_rx_field_t)i, vals[i]);
    }
    r->has_rx_timestamp = true;
    r->rx_timestamp_us = 123456;
    rs_csi_record_set_rx(r, RS_RX_ANT, 0);
    rs_csi_record_set_rx(r, RS_RX_SIG_LEN, 57);
    rs_csi_record_set_rx(r, RS_RX_RX_STATE, 0);
    r->first_word_invalid = false;
    r->len = 4;
    r->csi[0] = 0;
    r->csi[1] = -1;
    r->csi[2] = 127;
    r->csi[3] = -128;
}

static void make_worst_case_record(rs_csi_record_t *r)
{
    rs_csi_record_reset(r);
    r->rec_seq = UINT32_MAX;
    r->has_tx_seq = true;
    r->tx_seq = UINT32_MAX;
    r->drops = UINT32_MAX;
    memset(r->src_mac, 0xFF, 6);
    for (int i = 0; i < RS_RX_FIELD_COUNT; i++) {
        rs_csi_record_set_rx(r, (rs_rx_field_t)i, INT32_MIN); /* 11 characters each */
    }
    r->has_rx_timestamp = true;
    r->rx_timestamp_us = UINT32_MAX;
    r->first_word_invalid = true;
    r->len = RS_MAX_CSI_LEN;
    for (int i = 0; i < RS_MAX_CSI_LEN; i++) {
        r->csi[i] = (int8_t)(i - 128);
    }
}

/* ------------------------------------------------------------------------- */
/* Tests                                                                     */
/* ------------------------------------------------------------------------- */

static void test_crc_known_answers(void)
{
    CHECK(rs_crc32("123456789", 9) == 0xCBF43926u);
    CHECK(rs_crc32("", 0) == 0x00000000u);
    CHECK(rs_crc32("a", 1) == 0xE8B7BE43u);
    const char *fox = "The quick brown fox jumps over the lazy dog";
    CHECK(rs_crc32(fox, strlen(fox)) == 0x414FA339u);
    CHECK(rs_crc32(NULL, 0) == 0u);
    /* Values below were computed with Python zlib.crc32 (host side). */
    const char *l1 = "RSCSI,1,7,42,3,1a:00:00:00:00:00,-45,11,1,0,0,1,1,0,0,0,0,-95,0,11,0,"
                     "123456,0,57,0,NA,NA,NA,0,4,00ff7f80";
    CHECK(rs_crc32(l1, strlen(l1)) == 0xFC7F2A50u);
    const char *l2 = "RSSTAT,1,5000000000,4294967295,1000,125,0,0,125,NA,NA,180000,170000";
    CHECK(rs_crc32(l2, strlen(l2)) == 0xA97FFFC2u);
}

static void test_crc_matches_reference_random(void)
{
    static uint8_t buf[2048];
    for (size_t len = 0; len < sizeof buf; len += 1 + (len / 7)) {
        for (size_t i = 0; i < len; i++) {
            buf[i] = (uint8_t)(lcg() >> 24);
        }
        CHECK(rs_crc32(buf, len) == ref_crc32(buf, len));
    }
}

static void test_crc_incremental(void)
{
    const char *s = "RSHELLO,1,roomsense_csi_rx,0.1.0";
    const size_t n = strlen(s);
    for (size_t split = 0; split <= n; split++) {
        uint32_t c = rs_crc32_update(0, s, split);
        c = rs_crc32_update(c, s + split, n - split);
        CHECK(c == rs_crc32(s, n));
    }
}

static void test_hex_encoding(void)
{
    const int8_t v[] = {0, 1, -1, 127, -128, 16, -16};
    char out[15];
    CHECK(rs_hex_encode_i8(v, 7, out, sizeof out));
    CHECK_STREQ(out, "0001ff7f8010f0");

    /* Exact fit (2n + 1) works; one byte less fails without writing past cap. */
    char small[16];
    memset(small, 'Z', sizeof small);
    CHECK(!rs_hex_encode_i8(v, 7, small, 14));
    CHECK(small[0] == '\0');
    CHECK(small[14] == 'Z' && small[15] == 'Z');

    char empty[1] = {'Z'};
    CHECK(rs_hex_encode_i8(v, 0, empty, 1));
    CHECK(empty[0] == '\0');
    CHECK(!rs_hex_encode_i8(v, 1, NULL, 3));
    CHECK(!rs_hex_encode_i8(v, 1, out, 0));
    CHECK(!rs_hex_encode_i8(NULL, 1, out, sizeof out));

    /* Every int8 value round-trips through two's-complement hex. */
    int8_t all[256];
    for (int i = 0; i < 256; i++) {
        all[i] = (int8_t)(i - 128);
    }
    static char hex[513];
    CHECK(rs_hex_encode_i8(all, 256, hex, sizeof hex));
    int ok = 1;
    for (int i = 0; i < 256; i++) {
        unsigned b = 0;
        char pair[3] = {hex[2 * i], hex[2 * i + 1], '\0'};
        if (sscanf(pair, "%2x", &b) != 1 || (int8_t)(uint8_t)b != all[i]) {
            ok = 0;
        }
        if ((pair[0] >= 'A' && pair[0] <= 'F') || (pair[1] >= 'A' && pair[1] <= 'F')) {
            ok = 0; /* must be lowercase */
        }
    }
    CHECK(ok);
}

static void test_mac_parse_format(void)
{
    uint8_t mac[6] = {0};
    CHECK(rs_parse_mac("1a:00:00:00:00:00", mac));
    CHECK(mac[0] == 0x1a && mac[1] == 0 && mac[5] == 0);
    CHECK(rs_parse_mac("AA:bb:0C:dD:ee:FF", mac));
    CHECK(mac[0] == 0xAA && mac[1] == 0xBB && mac[2] == 0x0C && mac[3] == 0xDD &&
          mac[4] == 0xEE && mac[5] == 0xFF);
    char s[18];
    rs_format_mac(s, mac);
    CHECK_STREQ(s, "aa:bb:0c:dd:ee:ff");

    uint8_t keep[6] = {1, 2, 3, 4, 5, 6};
    const char *bad[] = {"",
                         "1a:00:00:00:00",
                         "1a:00:00:00:00:00:",
                         "1a:00:00:00:00:000",
                         "1a-00-00-00-00-00",
                         "1a:00:00:00:00:0g",
                         "1a:0:00:00:00:000",
                         " 1a:00:00:00:00:00",
                         "1a:00:00:00:00:0"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        CHECK(!rs_parse_mac(bad[i], keep));
    }
    CHECK(keep[0] == 1 && keep[5] == 6); /* untouched on failure */
    CHECK(!rs_parse_mac(NULL, keep));

    const uint8_t tx[6] = {0x1a, 0, 0, 0, 0, 0};
    const uint8_t mc[6] = {0x15, 0, 0, 0, 0, 0};
    const uint8_t vendor[6] = {0x34, 0x85, 0x18, 0, 0, 1};
    CHECK(rs_mac_is_unicast(tx));
    CHECK(rs_mac_is_locally_administered(tx));
    CHECK(!rs_mac_is_unicast(mc));
    CHECK(!rs_mac_is_locally_administered(vendor));
    CHECK(!rs_mac_is_unicast(NULL));
}

static void test_chip_revision(void)
{
    char out[16];
    CHECK(rs_format_chip_revision(out, sizeof out, 0));
    CHECK_STREQ(out, "0.0");
    CHECK(rs_format_chip_revision(out, sizeof out, 2));
    CHECK_STREQ(out, "0.2");
    CHECK(rs_format_chip_revision(out, sizeof out, 101));
    CHECK_STREQ(out, "1.1");
    CHECK(rs_format_chip_revision(out, sizeof out, 312));
    CHECK_STREQ(out, "3.12");
    char tiny[3] = {'Z', 'Z', 'Z'};
    CHECK(!rs_format_chip_revision(tiny, 3, 101)); /* "1.1" needs 4 bytes */
    CHECK(tiny[0] == '\0');
}

static void test_record_setters(void)
{
    static rs_csi_record_t r;
    memset(&r, 0x5A, sizeof r);
    rs_csi_record_reset(&r);
    CHECK(r.rx_present == 0 && !r.has_tx_seq && !r.has_rx_timestamp && r.len == 0);
    for (int i = 0; i < RS_RX_FIELD_COUNT; i++) {
        CHECK(!rs_csi_record_has_rx(&r, (rs_rx_field_t)i));
    }
    rs_csi_record_set_rx(&r, RS_RX_NOISE_FLOOR, -97);
    CHECK(rs_csi_record_has_rx(&r, RS_RX_NOISE_FLOOR));
    CHECK(r.rx[RS_RX_NOISE_FLOOR] == -97);
    rs_csi_record_set_rx(&r, RS_RX_FIELD_COUNT, 1);     /* ignored */
    rs_csi_record_set_rx(&r, (rs_rx_field_t)-1, 1);     /* ignored */
    CHECK(r.rx_present == (1u << RS_RX_NOISE_FLOOR));
    CHECK(!rs_csi_record_has_rx(&r, RS_RX_FIELD_COUNT));
    CHECK(!rs_csi_record_has_rx(NULL, RS_RX_RSSI));
}

static void test_csi_line_exact_classic(void)
{
    static rs_csi_record_t r;
    make_classic_record(&r);
    char out[RS_CSI_LINE_MAX];
    const int n = rs_format_csi_line(out, sizeof out, &r);
    const char *expected =
        "RSCSI,1,7,42,3,1a:00:00:00:00:00,-45,11,1,0,0,1,1,0,0,0,0,-95,0,11,0,"
        "123456,0,57,0,NA,NA,NA,0,4,00ff7f80*FC7F2A50\n"; /* CRC from Python zlib */
    CHECK_STREQ(out, expected);
    CHECK(n == (int)strlen(expected));
}

static void test_csi_line_exact_c5_na(void)
{
    static rs_csi_record_t r;
    static const uint8_t mac[6] = {0x1a, 0, 0, 0, 0, 0};
    rs_csi_record_reset(&r);
    r.rec_seq = 4294967295u;
    r.has_tx_seq = false;
    r.drops = 0;
    memcpy(r.src_mac, mac, 6);
    /* Only the fields that exist in esp_wifi_rxctrl_t and the protocol maps. */
    rs_csi_record_set_rx(&r, RS_RX_RSSI, -50);
    rs_csi_record_set_rx(&r, RS_RX_RATE, 11);
    rs_csi_record_set_rx(&r, RS_RX_NOISE_FLOOR, -96);
    rs_csi_record_set_rx(&r, RS_RX_CHANNEL, 11);
    rs_csi_record_set_rx(&r, RS_RX_SECONDARY_CHANNEL, 0);
    r.has_rx_timestamp = true;
    r.rx_timestamp_us = 4294967295u;
    rs_csi_record_set_rx(&r, RS_RX_SIG_LEN, 60);
    rs_csi_record_set_rx(&r, RS_RX_BB_FORMAT, 2);
    r.first_word_invalid = true;
    r.len = 2;
    r.csi[0] = -128;
    r.csi[1] = 1;
    char out[RS_CSI_LINE_MAX];
    const int n = rs_format_csi_line(out, sizeof out, &r);
    const char *expected =
        "RSCSI,1,4294967295,NA,0,1a:00:00:00:00:00,-50,11,NA,NA,NA,NA,NA,NA,NA,NA,NA,-96,NA,"
        "11,0,4294967295,NA,60,NA,2,NA,NA,1,2,8001*3D1843D4\n"; /* CRC from Python zlib */
    CHECK_STREQ(out, expected);
    CHECK(n == (int)strlen(expected));
}

static void test_csi_line_parses_back(void)
{
    static rs_csi_record_t r;
    make_classic_record(&r);
    r.len = 128;
    for (int i = 0; i < 128; i++) {
        r.csi[i] = (int8_t)(lcg() >> 24);
    }
    static char out[RS_CSI_LINE_MAX];
    CHECK(rs_format_csi_line(out, sizeof out, &r) > 0);
    const int nf = split_and_verify(out, g_fields, 40);
    CHECK(nf == 31);
    if (nf == 31) {
        CHECK_STREQ(g_fields[0], "RSCSI");
        CHECK_STREQ(g_fields[1], "1");
        CHECK_STREQ(g_fields[29], "128");
        CHECK(strlen(g_fields[30]) == 256);
        int ok = 1;
        for (int i = 0; i < 128; i++) {
            unsigned b = 0;
            char pair[3] = {g_fields[30][2 * i], g_fields[30][2 * i + 1], '\0'};
            if (sscanf(pair, "%2x", &b) != 1 || (int8_t)(uint8_t)b != r.csi[i]) {
                ok = 0;
            }
        }
        CHECK(ok);
    }
}

static void test_csi_line_worst_case_fits(void)
{
    static rs_csi_record_t r;
    make_worst_case_record(&r);
    static char out[RS_CSI_LINE_MAX];
    const int n = rs_format_csi_line(out, sizeof out, &r);
    /* The documented bound is tight: worst case uses every byte but the NUL. */
    CHECK(n == RS_CSI_LINE_MAX - 1);
    CHECK(n > 0 && out[n - 1] == '\n' && out[n] == '\0');
    CHECK(split_and_verify(out, g_fields, 40) == 31);

    /* One byte short => -1, empty output, nothing written past the cap. */
    char *exact = malloc(RS_CSI_LINE_MAX - 1);
    CHECK(exact != NULL);
    if (exact != NULL) {
        CHECK(rs_format_csi_line(exact, RS_CSI_LINE_MAX - 1, &r) == -1);
        CHECK(exact[0] == '\0');
        free(exact);
    }
    printf("  info: worst-case RSCSI line = %d bytes (RS_CSI_LINE_MAX = %d incl. NUL)\n", n,
           RS_CSI_LINE_MAX);
}

/*
 * For every capacity from 0 to (needed + 2): the formatter either returns the
 * full line or -1, never writes at/after out[cap] (checked by a canary and by
 * AddressSanitizer on an exactly sized heap block) and always NUL-terminates.
 */
static void check_every_cap(int (*fmt)(char *, size_t, const void *), const void *arg,
                            const char *label)
{
    static char full[RS_CSI_LINE_MAX];
    const int needed = fmt(full, sizeof full, arg);
    CHECK(needed > 0);
    if (needed <= 0) {
        return;
    }
    int bad = 0;
    for (size_t cap = 0; cap <= (size_t)needed + 2u; cap++) {
        char *exact = malloc(cap == 0 ? 1 : cap); /* ASan catches any overflow */
        char *canary = malloc(cap + 32u);
        if (exact == NULL || canary == NULL) {
            bad = 1;
            free(exact);
            free(canary);
            break;
        }
        memset(exact, 'Q', cap == 0 ? 1 : cap);
        memset(canary, 'Q', cap + 32u);
        const int n1 = fmt(exact, cap, arg);
        const int n2 = fmt(canary, cap, arg);
        for (size_t i = cap; i < cap + 32u; i++) {
            if (canary[i] != 'Q') {
                bad = 1;
            }
        }
        if (cap >= (size_t)needed + 1u) {
            if (n1 != needed || n2 != needed || memcmp(canary, full, (size_t)needed + 1u) != 0) {
                bad = 1;
            }
        } else {
            if (n1 != -1 || n2 != -1) {
                bad = 1;
            }
            if (cap > 0 && (exact[0] != '\0' || canary[0] != '\0')) {
                bad = 1;
            }
            if (cap == 0 && exact[0] != 'Q') {
                bad = 1; /* cap 0: must not write even the NUL */
            }
        }
        free(exact);
        free(canary);
    }
    if (bad) {
        fprintf(stderr, "  capacity sweep failed for %s\n", label);
    }
    CHECK(!bad);
}

static int fmt_csi(char *o, size_t c, const void *a) { return rs_format_csi_line(o, c, a); }
static int fmt_hello(char *o, size_t c, const void *a) { return rs_format_hello(o, c, a); }
static int fmt_stat(char *o, size_t c, const void *a) { return rs_format_stat(o, c, a); }

static void test_every_capacity(void)
{
    static rs_csi_record_t r;
    make_classic_record(&r);
    check_every_cap(fmt_csi, &r, "RSCSI small");
    make_worst_case_record(&r);
    check_every_cap(fmt_csi, &r, "RSCSI worst case");
    const rs_hello_t h = {.fw_name = "roomsense_csi_rx", .mode = "ESPNOW_RX", .has_baud = true,
                          .baud = 921600};
    check_every_cap(fmt_hello, &h, "RSHELLO");
    const rs_stat_t s = {.uptime_ms = 123, .cb_total = 5, .free_heap = 1};
    check_every_cap(fmt_stat, &s, "RSSTAT");
}

static void test_csi_line_rejects_oversize_len(void)
{
    static rs_csi_record_t r;
    make_classic_record(&r);
    r.len = RS_MAX_CSI_LEN + 1;
    static char out[RS_CSI_LINE_MAX + 16];
    memset(out, 'Q', sizeof out);
    CHECK(rs_format_csi_line(out, sizeof out, &r) == -1);
    CHECK(out[0] == '\0');
    r.len = 0xFFFF;
    CHECK(rs_format_csi_line(out, sizeof out, &r) == -1);
}

static void test_formatters_null_args(void)
{
    char out[64] = "junk";
    static rs_csi_record_t r;
    make_classic_record(&r);
    CHECK(rs_format_csi_line(NULL, 100, &r) == -1);
    CHECK(rs_format_csi_line(out, sizeof out, NULL) == -1);
    CHECK(out[0] == '\0');
    CHECK(rs_format_hello(out, sizeof out, NULL) == -1);
    CHECK(rs_format_stat(out, sizeof out, NULL) == -1);
    CHECK(rs_format_stat(NULL, 0, NULL) == -1);
}

static void test_hello_exact(void)
{
    rs_hello_t h = {
        .fw_name = "roomsense_csi_rx", .fw_version = "0.1.0", .idf_version = "v5.5.5",
        .chip = "esp32s3", .chip_revision = "0.2", .has_sta_mac = true,
        .sta_mac = {0x02, 0, 0, 0, 0, 0x01}, .mode = "ESPNOW_RX", .ltf_config = "lltf_only",
        .has_channel = true, .channel = 11, .has_secondary_channel = true,
        .secondary_channel = 0, .has_tx_mac_filter = true, .tx_mac_filter = {0x1a, 0, 0, 0, 0, 0},
        .has_rate_hz = true, .rate_hz = 25, .has_queue_depth = true, .queue_depth = 32,
        .has_baud = true, .baud = 921600, .build_id = NULL,
    };
    char out[RS_HELLO_LINE_MAX];
    CHECK(rs_format_hello(out, sizeof out, &h) > 0);
    CHECK_STREQ(out, "RSHELLO,1,roomsense_csi_rx,0.1.0,v5.5.5,esp32s3,0.2,02:00:00:00:00:01,"
                     "ESPNOW_RX,lltf_only,11,0,1a:00:00:00:00:00,25,32,921600,NA*ECF1B21B\n");
    CHECK(split_and_verify(out, g_fields, 40) == 17);
}

static void test_hello_na_and_unsafe_strings(void)
{
    /* Router mode before the connection: channel, secondary and BSSID unknown.
     * An idf_version with a comma must never corrupt the field layout. */
    rs_hello_t h = {
        .fw_name = "roomsense_csi_rx", .fw_version = "0.1.0", .idf_version = "v5.5,5",
        .chip = "esp32", .chip_revision = "3.1", .has_sta_mac = false,
        .mode = "ROUTER_PING", .ltf_config = "lltf_only", .has_channel = false,
        .has_secondary_channel = false, .has_tx_mac_filter = false, .has_rate_hz = true,
        .rate_hz = 25, .has_queue_depth = true, .queue_depth = 32, .has_baud = false,
        .build_id = "",
    };
    char out[RS_HELLO_LINE_MAX];
    CHECK(rs_format_hello(out, sizeof out, &h) > 0);
    char expected[RS_HELLO_LINE_MAX];
    ref_line(expected, sizeof expected,
             "RSHELLO,1,roomsense_csi_rx,0.1.0,NA,esp32,3.1,NA,ROUTER_PING,lltf_only,NA,NA,NA,"
             "25,32,NA,NA");
    CHECK_STREQ(out, expected);
    CHECK_STREQ(out, "RSHELLO,1,roomsense_csi_rx,0.1.0,NA,esp32,3.1,NA,ROUTER_PING,lltf_only,"
                     "NA,NA,NA,25,32,NA,NA*10B504C4\n"); /* CRC from Python zlib */

    CHECK(rs_hello_str_is_safe("v5.5.5-dirty"));
    CHECK(!rs_hello_str_is_safe(NULL));
    CHECK(!rs_hello_str_is_safe(""));
    CHECK(!rs_hello_str_is_safe("a,b"));
    CHECK(!rs_hello_str_is_safe("a*b"));
    CHECK(!rs_hello_str_is_safe("a b"));
    CHECK(!rs_hello_str_is_safe("a\nb"));
    CHECK(!rs_hello_str_is_safe("a\rb"));
    CHECK(!rs_hello_str_is_safe("caf\xc3\xa9"));
    char s40[41];
    memset(s40, 'x', 40);
    s40[40] = '\0';
    CHECK(rs_hello_str_is_safe(s40));
    char s41[42];
    memset(s41, 'x', 41);
    s41[41] = '\0';
    CHECK(!rs_hello_str_is_safe(s41));
}

static void test_hello_worst_case_fits(void)
{
    char s[RS_HELLO_STR_MAX + 1];
    memset(s, 'x', RS_HELLO_STR_MAX);
    s[RS_HELLO_STR_MAX] = '\0';
    const rs_hello_t h = {
        .fw_name = s, .fw_version = s, .idf_version = s, .chip = s, .chip_revision = s,
        .has_sta_mac = true, .mode = s, .ltf_config = s, .has_channel = true,
        .channel = INT32_MIN, .has_secondary_channel = true, .secondary_channel = INT32_MIN,
        .has_tx_mac_filter = true, .has_rate_hz = true, .rate_hz = UINT32_MAX,
        .has_queue_depth = true, .queue_depth = UINT32_MAX, .has_baud = true,
        .baud = UINT32_MAX, .build_id = s,
    };
    static char out[RS_HELLO_LINE_MAX];
    const int n = rs_format_hello(out, sizeof out, &h);
    CHECK(n == RS_HELLO_LINE_MAX - 1);
    CHECK(rs_format_hello(out, RS_HELLO_LINE_MAX - 1, &h) == -1);
}

static void test_stat_exact(void)
{
    const rs_stat_t s = {
        .uptime_ms = 5000000000ull, .cb_total = 4294967295u, .cb_filtered = 1000,
        .enqueued = 125, .dropped_queue_full = 0, .dropped_oversize = 0, .printed = 125,
        .has_tx_ok = false, .has_tx_fail = false, .free_heap = 180000, .min_free_heap = 170000,
    };
    char out[RS_STAT_LINE_MAX];
    CHECK(rs_format_stat(out, sizeof out, &s) > 0);
    CHECK_STREQ(out, "RSSTAT,1,5000000000,4294967295,1000,125,0,0,125,NA,NA,180000,170000"
                     "*A97FFFC2\n"); /* CRC from Python zlib */
    CHECK(split_and_verify(out, g_fields, 40) == 13);

    const rs_stat_t router = {.has_tx_ok = true, .tx_ok = 7, .has_tx_fail = true, .tx_fail = 1,
                              .free_heap = UINT32_MAX};
    CHECK(rs_format_stat(out, sizeof out, &router) > 0);
    CHECK_STREQ(out, "RSSTAT,1,0,0,0,0,0,0,0,7,1,4294967295,0*2F87082A\n");
}

static void test_stat_worst_case_fits(void)
{
    const rs_stat_t s = {
        .uptime_ms = UINT64_MAX, .cb_total = UINT32_MAX, .cb_filtered = UINT32_MAX,
        .enqueued = UINT32_MAX, .dropped_queue_full = UINT32_MAX,
        .dropped_oversize = UINT32_MAX, .printed = UINT32_MAX, .has_tx_ok = true,
        .tx_ok = UINT32_MAX, .has_tx_fail = true, .tx_fail = UINT32_MAX,
        .free_heap = UINT32_MAX, .min_free_heap = UINT32_MAX,
    };
    char out[RS_STAT_LINE_MAX];
    CHECK(rs_format_stat(out, sizeof out, &s) == RS_STAT_LINE_MAX - 1);
    CHECK(rs_format_stat(out, RS_STAT_LINE_MAX - 1, &s) == -1);
}

static void test_counter_wraparound_u32(void)
{
    /* The device counters are uint32_t and wrap; they must print as u32,
     * never as a negative or 64-bit value. */
    static rs_csi_record_t r;
    make_classic_record(&r);
    uint32_t seq = UINT32_MAX;
    r.rec_seq = seq;
    r.drops = seq;
    static char out[RS_CSI_LINE_MAX];
    CHECK(rs_format_csi_line(out, sizeof out, &r) > 0);
    CHECK(split_and_verify(out, g_fields, 40) == 31);
    CHECK_STREQ(g_fields[2], "4294967295");
    CHECK_STREQ(g_fields[4], "4294967295");
    seq++; /* wraps to 0, like the firmware's rec_seq */
    r.rec_seq = seq;
    r.drops = seq;
    r.tx_seq = UINT32_MAX;
    CHECK(rs_format_csi_line(out, sizeof out, &r) > 0);
    CHECK(split_and_verify(out, g_fields, 40) == 31);
    CHECK_STREQ(g_fields[2], "0");
    CHECK_STREQ(g_fields[3], "4294967295");
    CHECK_STREQ(g_fields[4], "0");

    rs_stat_t s = {.cb_total = UINT32_MAX, .printed = UINT32_MAX};
    s.cb_total++;
    CHECK(rs_format_stat(out, sizeof out, &s) > 0);
    CHECK(split_and_verify(out, g_fields, 40) == 13);
    CHECK_STREQ(g_fields[3], "0");
    CHECK_STREQ(g_fields[8], "4294967295");
}

static void test_tx_payload_roundtrip_and_bytes(void)
{
    uint8_t p[RS_TX_PAYLOAD_LEN];
    CHECK(rs_build_tx_payload(p, sizeof p, 0xAB, 0x11223344u, 25) == RS_TX_PAYLOAD_LEN);
    const uint8_t expected[RS_TX_PAYLOAD_LEN] = {'R', 'S', 'T', 'X', 1, 0xAB,
                                                 0x44, 0x33, 0x22, 0x11, 25, 0};
    CHECK(memcmp(p, expected, sizeof p) == 0);
    uint32_t seq = 0;
    uint8_t id = 0;
    uint16_t rate = 0;
    CHECK(rs_parse_tx_payload(p, sizeof p, &seq, &id));
    CHECK(seq == 0x11223344u && id == 0xAB);
    CHECK(rs_parse_tx_payload_ex(p, sizeof p, &seq, &id, &rate));
    CHECK(rate == 25);
    CHECK(rs_parse_tx_payload(p, sizeof p, NULL, NULL));

    /* Extremes round-trip. */
    CHECK(rs_build_tx_payload(p, sizeof p, 0, UINT32_MAX, UINT16_MAX) == RS_TX_PAYLOAD_LEN);
    CHECK(rs_parse_tx_payload_ex(p, sizeof p, &seq, &id, &rate));
    CHECK(seq == UINT32_MAX && id == 0 && rate == UINT16_MAX);

    /* Too-small output buffer: returns 0 and writes nothing. */
    uint8_t small[RS_TX_PAYLOAD_LEN];
    memset(small, 0xEE, sizeof small);
    CHECK(rs_build_tx_payload(small, RS_TX_PAYLOAD_LEN - 1, 1, 1, 1) == 0);
    int untouched = 1;
    for (size_t i = 0; i < sizeof small; i++) {
        untouched &= (small[i] == 0xEE);
    }
    CHECK(untouched);
    CHECK(rs_build_tx_payload(NULL, 64, 1, 1, 1) == 0);

    /* Trailing bytes after the 12-byte payload are ignored. */
    uint8_t longer[20] = {0};
    CHECK(rs_build_tx_payload(longer, sizeof longer, 3, 99, 50) == RS_TX_PAYLOAD_LEN);
    CHECK(rs_parse_tx_payload(longer, sizeof longer, &seq, &id) && seq == 99 && id == 3);
}

static void test_tx_payload_rejects(void)
{
    uint8_t p[RS_TX_PAYLOAD_LEN];
    rs_build_tx_payload(p, sizeof p, 1, 1234, 25);
    uint32_t seq = 777;
    uint8_t id = 77;

    CHECK(!rs_parse_tx_payload(p, RS_TX_PAYLOAD_LEN - 1, &seq, &id)); /* short */
    CHECK(!rs_parse_tx_payload(p, 0, &seq, &id));
    CHECK(!rs_parse_tx_payload(NULL, RS_TX_PAYLOAD_LEN, &seq, &id));

    uint8_t bad[RS_TX_PAYLOAD_LEN];
    memcpy(bad, p, sizeof bad);
    bad[0] = 'r'; /* wrong magic */
    CHECK(!rs_parse_tx_payload(bad, sizeof bad, &seq, &id));
    memcpy(bad, p, sizeof bad);
    bad[3] = 'Y';
    CHECK(!rs_parse_tx_payload(bad, sizeof bad, &seq, &id));
    memcpy(bad, p, sizeof bad);
    bad[4] = 2; /* unknown version */
    CHECK(!rs_parse_tx_payload(bad, sizeof bad, &seq, &id));
    memcpy(bad, p, sizeof bad);
    bad[4] = 0;
    CHECK(!rs_parse_tx_payload(bad, sizeof bad, &seq, &id));

    /* The upstream csi_send payload is a bare 4-byte counter: not ours. */
    const uint8_t upstream[4] = {1, 0, 0, 0};
    CHECK(!rs_parse_tx_payload(upstream, sizeof upstream, &seq, &id));

    CHECK(seq == 777 && id == 77); /* outputs untouched on every failure */
}

/* Frame body (after the 24-byte MAC header) of an ESP-NOW v1 broadcast. */
static size_t build_espnow_frame_body(uint8_t *out, const uint8_t *body, size_t body_len)
{
    size_t i = 0;
    out[i++] = 127; /* category: vendor specific */
    out[i++] = 0x18;
    out[i++] = 0xfe;
    out[i++] = 0x34; /* Espressif OUI */
    out[i++] = 0xde;
    out[i++] = 0xad;
    out[i++] = 0xbe;
    out[i++] = 0xef; /* random value */
    out[i++] = 221;  /* element id */
    out[i++] = (uint8_t)(5 + body_len);
    out[i++] = 0x18;
    out[i++] = 0xfe;
    out[i++] = 0x34;
    out[i++] = 4;    /* type: ESP-NOW */
    out[i++] = 0x01; /* reserved | version */
    memcpy(out + i, body, body_len);
    i += body_len;
    return i;
}

static void test_espnow_body(void)
{
    uint8_t payload[RS_TX_PAYLOAD_LEN];
    rs_build_tx_payload(payload, sizeof payload, 9, 4242, 25);
    uint8_t frame[64];
    const size_t flen = build_espnow_frame_body(frame, payload, sizeof payload);
    CHECK(flen == RS_ESPNOW_BODY_OFFSET + RS_TX_PAYLOAD_LEN);

    const uint8_t *body = NULL;
    size_t body_len = 0;
    CHECK(rs_espnow_body(frame, flen, &body, &body_len));
    CHECK(body == frame + RS_ESPNOW_BODY_OFFSET && body_len == RS_TX_PAYLOAD_LEN);

    /* End to end: frame -> body -> RSTX fields (what the CSI callback does). */
    uint32_t seq = 0;
    uint8_t id = 0;
    CHECK(body != NULL && rs_parse_tx_payload(body, body_len, &seq, &id));
    CHECK(seq == 4242 && id == 9);

    /* Captured length that includes a 4-byte FCS after the body is fine. */
    CHECK(rs_espnow_body(frame, flen + 4, &body, &body_len) && body_len == RS_TX_PAYLOAD_LEN);

    /* Structural mismatches are rejected. */
    const size_t offsets[] = {0, 1, 2, 3, 8, 10, 11, 12, 13};
    for (size_t k = 0; k < sizeof offsets / sizeof offsets[0]; k++) {
        uint8_t f2[64];
        memcpy(f2, frame, flen);
        f2[offsets[k]] ^= 0x01;
        CHECK(!rs_espnow_body(f2, flen, &body, &body_len));
    }
    /* Random bytes (offset 4..7) and version (14) do not matter. */
    uint8_t f3[64];
    memcpy(f3, frame, flen);
    f3[5] ^= 0xFF;
    f3[14] = 0x02;
    CHECK(rs_espnow_body(f3, flen, &body, &body_len));

    /* Truncated capture: the element claims more bytes than we have. */
    CHECK(!rs_espnow_body(frame, flen - 1, &body, &body_len));
    CHECK(!rs_espnow_body(frame, RS_ESPNOW_BODY_OFFSET - 1, &body, &body_len));
    CHECK(!rs_espnow_body(frame, 0, &body, &body_len));
    CHECK(!rs_espnow_body(NULL, flen, &body, &body_len));
    CHECK(!rs_espnow_body(frame, flen, NULL, &body_len));

    /* Element length below the fixed 5 bytes. */
    uint8_t f4[64];
    memcpy(f4, frame, flen);
    f4[9] = 4;
    CHECK(!rs_espnow_body(f4, flen, &body, &body_len));
    f4[9] = 5; /* empty body is structurally valid ... */
    CHECK(rs_espnow_body(f4, flen, &body, &body_len) && body_len == 0);
    /* ... but is not an RSTX payload. */
    CHECK(!rs_parse_tx_payload(body, body_len, &seq, &id));

    /* Short ESP-NOW body (e.g. upstream csi_send's 4-byte counter). */
    const uint8_t counter[4] = {1, 2, 3, 4};
    uint8_t f5[64];
    const size_t f5len = build_espnow_frame_body(f5, counter, sizeof counter);
    CHECK(rs_espnow_body(f5, f5len, &body, &body_len) && body_len == 4);
    CHECK(!rs_parse_tx_payload(body, body_len, &seq, &id));
}

static void test_period_ticks(void)
{
    int ok = 1;
    for (uint32_t rate = 1; rate <= 100; rate++) {
        const uint32_t lo = 1000u / rate;
        const uint32_t hi = lo + ((1000u % rate) ? 1u : 0u);
        const uint32_t starts[] = {0u, 17u, 123456u, UINT32_MAX - 3u * rate};
        for (size_t s = 0; s < sizeof starts / sizeof starts[0]; s++) {
            uint64_t sum = 0;
            uint32_t k = starts[s];
            for (uint32_t i = 0; i < rate; i++, k++) {
                const uint32_t p = rs_period_ticks(k, rate, 1000);
                if (p < lo || p > hi) {
                    ok = 0;
                }
                sum += p;
            }
            if (sum != 1000u) {
                ok = 0; /* exactly rate events per 1000 ticks, even across the wrap */
            }
        }
    }
    CHECK(ok);
    CHECK(rs_period_ticks(0, 25, 1000) == 40);
    CHECK(rs_period_ticks(5, 0, 1000) == 0);
    CHECK(rs_period_ticks(5, 1001, 1000) == 0);
    CHECK(rs_period_ticks(UINT32_MAX, 30, 1000) >= 33 && rs_period_ticks(UINT32_MAX, 30, 1000) <= 34);
}

static int format_case(const golden_case_t *c, char *buf, size_t cap)
{
    switch (c->kind) {
    case GOLDEN_HELLO:
        return rs_format_hello(buf, cap, &c->hello);
    case GOLDEN_CSI:
        return rs_format_csi_line(buf, cap, &c->csi);
    case GOLDEN_STAT:
        return rs_format_stat(buf, cap, &c->stat);
    }
    return -1;
}

static void test_golden_lines_are_clean(void)
{
    static golden_case_t cases[GOLDEN_MAX_CASES];
    const size_t n = golden_build_cases(cases, GOLDEN_MAX_CASES);
    CHECK(n >= 10);
    static char buf[RS_CSI_LINE_MAX];
    int saw_len128 = 0, saw_len612 = 0, saw_na = 0, saw_near_wrap = 0, saw_negative = 0;
    for (size_t i = 0; i < n; i++) {
        const int len = format_case(&cases[i], buf, sizeof buf);
        CHECK(len > 0);
        if (len <= 0) {
            continue;
        }
        int clean = 1;
        for (int j = 0; j < len - 1; j++) {
            const unsigned char ch = (unsigned char)buf[j];
            if (ch < 0x20u || ch > 0x7Eu) {
                clean = 0; /* no CR, LF, NUL or non-ASCII inside a line */
            }
        }
        CHECK(clean && buf[len - 1] == '\n');
        const int expected_fields =
            cases[i].kind == GOLDEN_CSI ? 31 : (cases[i].kind == GOLDEN_HELLO ? 17 : 13);
        CHECK(split_and_verify(buf, g_fields, 40) == expected_fields);
        if (strstr(buf, ",NA") != NULL) {
            saw_na = 1;
        }
        if (cases[i].kind == GOLDEN_CSI) {
            saw_len128 |= (cases[i].csi.len == 128);
            saw_len612 |= (cases[i].csi.len == 612);
            saw_near_wrap |= (cases[i].csi.rec_seq >= 4294967294u);
            for (int j = 0; j < cases[i].csi.len; j++) {
                saw_negative |= (cases[i].csi.csi[j] < 0);
            }
        }
    }
    CHECK(saw_len128 && saw_len612 && saw_na && saw_near_wrap && saw_negative);
}

static void test_golden_file_matches(void)
{
    if (g_golden_path == NULL) {
        printf("  info: no golden path given; skipping golden-file comparison\n");
        return;
    }
    FILE *f = fopen(g_golden_path, "rb");
    if (f == NULL) {
        printf("  info: %s not found (run `make golden`); comparison skipped\n", g_golden_path);
        return;
    }
    static char file_buf[64 * 1024];
    const size_t got = fread(file_buf, 1, sizeof file_buf - 1, f);
    fclose(f);
    file_buf[got] = '\0';

    static golden_case_t cases[GOLDEN_MAX_CASES];
    const size_t n = golden_build_cases(cases, GOLDEN_MAX_CASES);
    static char expect_buf[64 * 1024];
    size_t pos = 0;
    static char line[RS_CSI_LINE_MAX];
    for (size_t i = 0; i < n; i++) {
        const int len = format_case(&cases[i], line, sizeof line);
        CHECK(len > 0 && pos + (size_t)len < sizeof expect_buf);
        if (len > 0 && pos + (size_t)len < sizeof expect_buf) {
            memcpy(expect_buf + pos, line, (size_t)len);
            pos += (size_t)len;
        }
    }
    expect_buf[pos] = '\0';
    CHECK(got == pos && memcmp(file_buf, expect_buf, pos) == 0);
    if (got != pos || memcmp(file_buf, expect_buf, pos) != 0) {
        fprintf(stderr, "  golden file %s is stale: run `make golden`\n", g_golden_path);
    }
}

static void test_typical_line_sizes(void)
{
    /* Informational: sizes used for the throughput numbers in the README. */
    static golden_case_t cases[GOLDEN_MAX_CASES];
    const size_t n = golden_build_cases(cases, GOLDEN_MAX_CASES);
    static char buf[RS_CSI_LINE_MAX];
    for (size_t i = 0; i < n; i++) {
        const int len = format_case(&cases[i], buf, sizeof buf);
        CHECK(len > 0);
        printf("  info: %-40s %4d bytes (+1 CR on the wire)\n", cases[i].name, len);
    }
}

/* ------------------------------------------------------------------------- */

typedef struct {
    const char *name;
    test_fn fn;
} test_case_t;

int main(int argc, char **argv)
{
    g_golden_path = (argc > 1) ? argv[1] : NULL;

    const test_case_t tests[] = {
        {"crc_known_answers", test_crc_known_answers},
        {"crc_matches_reference_random", test_crc_matches_reference_random},
        {"crc_incremental", test_crc_incremental},
        {"hex_encoding", test_hex_encoding},
        {"mac_parse_format", test_mac_parse_format},
        {"chip_revision", test_chip_revision},
        {"record_setters", test_record_setters},
        {"csi_line_exact_classic", test_csi_line_exact_classic},
        {"csi_line_exact_c5_na", test_csi_line_exact_c5_na},
        {"csi_line_parses_back", test_csi_line_parses_back},
        {"csi_line_worst_case_fits", test_csi_line_worst_case_fits},
        {"every_capacity_no_overflow", test_every_capacity},
        {"csi_line_rejects_oversize_len", test_csi_line_rejects_oversize_len},
        {"formatters_null_args", test_formatters_null_args},
        {"hello_exact", test_hello_exact},
        {"hello_na_and_unsafe_strings", test_hello_na_and_unsafe_strings},
        {"hello_worst_case_fits", test_hello_worst_case_fits},
        {"stat_exact", test_stat_exact},
        {"stat_worst_case_fits", test_stat_worst_case_fits},
        {"counter_wraparound_u32", test_counter_wraparound_u32},
        {"tx_payload_roundtrip_and_bytes", test_tx_payload_roundtrip_and_bytes},
        {"tx_payload_rejects", test_tx_payload_rejects},
        {"espnow_body", test_espnow_body},
        {"period_ticks", test_period_ticks},
        {"golden_lines_are_clean", test_golden_lines_are_clean},
        {"golden_file_matches", test_golden_file_matches},
        {"typical_line_sizes", test_typical_line_sizes},
    };
    const int n_tests = (int)(sizeof tests / sizeof tests[0]);
    int tests_failed = 0;
    for (int i = 0; i < n_tests; i++) {
        g_current_failed = 0;
        printf("[ RUN  ] %s\n", tests[i].name);
        tests[i].fn();
        if (g_current_failed) {
            tests_failed++;
            printf("[ FAIL ] %s (%d failed checks)\n", tests[i].name, g_current_failed);
        } else {
            printf("[  OK  ] %s\n", tests[i].name);
        }
    }
    printf("\nrs_csi_core host tests: %d/%d tests passed, %d failed; %d checks passed, %d "
           "failed\n",
           n_tests - tests_failed, n_tests, tests_failed, g_checks_passed, g_checks_failed);
    return (tests_failed == 0 && g_checks_failed == 0) ? 0 : 1;
}
