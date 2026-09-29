/*
 * Golden test vectors for the RoomSense serial protocol.
 *
 * These are SYNTHETIC inputs (hand-chosen field values, deterministic CSI
 * patterns, locally administered example MAC addresses). They are not
 * measurements. The C formatter turns them into golden_rscsi_lines.txt, which
 * the Python parser tests read to check that firmware and host agree.
 */
#ifndef GOLDEN_CASES_H
#define GOLDEN_CASES_H

#include <stddef.h>

#include "rs_csi_core.h"

typedef enum { GOLDEN_HELLO, GOLDEN_CSI, GOLDEN_STAT } golden_kind_t;

typedef struct {
    golden_kind_t kind;
    const char *name; /* short id used in the expected-values JSON */
    const char *note; /* why the case exists */
    rs_hello_t hello;
    rs_csi_record_t csi;
    rs_stat_t stat;
} golden_case_t;

#define GOLDEN_MAX_CASES 16

/** Fill out[0..n) with the golden cases; returns n (<= max). */
size_t golden_build_cases(golden_case_t *out, size_t max);

#endif /* GOLDEN_CASES_H */
