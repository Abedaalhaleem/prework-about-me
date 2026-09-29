# Host build and tests for firmware/esp32/components/rs_csi_core.
#
#   make test            build with sanitizers (if the compiler supports them) and run
#   make golden          (re)write golden_rscsi_lines.txt + golden_rscsi_expected.jsonl
#   make verify-golden   check the golden files independently with python3 + zlib
#   make strict          compile the core with extra warnings (-Wpedantic, -Wconversion, ...)
#   make cross           compile-only check of the core for rv32imc (clang, freestanding)
#   make clean
#
# Build products go to ./build (git-ignored). Only the golden files are
# written next to this Makefile, because the Python parser tests read them.

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CORE_DIR := $(HERE)../components/rs_csi_core
BUILD := $(HERE)build
GOLDEN_LINES := $(HERE)golden_rscsi_lines.txt
GOLDEN_EXPECTED := $(HERE)golden_rscsi_expected.jsonl

# The task contract names gcc; make's built-in default for CC is "cc".
ifeq ($(origin CC),default)
CC := gcc
endif
CLANG ?= clang
PYTHON ?= python3

CFLAGS_BASE := -std=c11 -Wall -Wextra -Werror -O2 -g
INCLUDES := -I$(CORE_DIR)/include -I$(HERE)

# Use ASan + UBSan only if they both link *and run* here; otherwise fall back.
SAN_FLAGS := -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all
HAVE_SAN := $(shell mkdir -p $(BUILD) && printf 'int main(void){return 0;}\n' > $(BUILD)/san_probe.c && \
	$(CC) $(SAN_FLAGS) $(BUILD)/san_probe.c -o $(BUILD)/san_probe >/dev/null 2>&1 && \
	$(BUILD)/san_probe >/dev/null 2>&1 && echo 1 || echo 0)
ifeq ($(HAVE_SAN),1)
SAN := $(SAN_FLAGS)
SAN_NOTE := AddressSanitizer+UndefinedBehaviorSanitizer enabled
else
SAN :=
SAN_NOTE := sanitizers NOT available with $(CC); built without them
endif

CORE_SRC := $(CORE_DIR)/rs_csi_core.c
CORE_HDR := $(CORE_DIR)/include/rs_csi_core.h
TEST_SRC := $(HERE)test_rs_csi_core.c $(HERE)golden_cases.c
GEN_SRC := $(HERE)gen_golden.c $(HERE)golden_cases.c
TEST_BIN := $(BUILD)/test_rs_csi_core
GEN_BIN := $(BUILD)/gen_golden

.PHONY: all test golden verify-golden strict cross clean

all: test

$(TEST_BIN): $(TEST_SRC) $(CORE_SRC) $(CORE_HDR) $(HERE)golden_cases.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS_BASE) $(SAN) $(INCLUDES) $(TEST_SRC) $(CORE_SRC) -o $@

$(GEN_BIN): $(GEN_SRC) $(CORE_SRC) $(CORE_HDR) $(HERE)golden_cases.h
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS_BASE) $(SAN) $(INCLUDES) $(GEN_SRC) $(CORE_SRC) -o $@

test: $(TEST_BIN)
	@echo "== rs_csi_core host tests ($(CC), $(SAN_NOTE)) =="
	$(TEST_BIN) $(GOLDEN_LINES)

golden: $(GEN_BIN)
	$(GEN_BIN) $(GOLDEN_LINES) $(GOLDEN_EXPECTED)

verify-golden:
	$(PYTHON) $(HERE)verify_golden.py $(GOLDEN_LINES) $(GOLDEN_EXPECTED)

# Stricter warning set for the firmware core only (compile, no link).
strict:
	@mkdir -p $(BUILD)
	$(CC) -std=c11 -Wall -Wextra -Werror -Wpedantic -Wshadow -Wconversion -Wsign-conversion \
		-Wstrict-prototypes -Wmissing-prototypes -Wcast-qual -Wundef -O2 \
		-I$(CORE_DIR)/include -c $(CORE_SRC) -o $(BUILD)/rs_csi_core_strict.o
	@echo "strict: rs_csi_core.c compiled cleanly with the extended warning set"

# The core is freestanding (stdint/stddef/stdbool only), so clang can compile
# it for the RISC-V ESP32 CPU family (C3/C5/C6/C61: rv32imc/rv32imac) without
# an ESP-IDF sysroot. The second build redefines int32_t/uint32_t as
# long / unsigned long, which is what the ESP-IDF >= 5.0 GCC toolchains use.
# This checks 32-bit portability of the core only. It is NOT a firmware build,
# and clang 18 has no Xtensa (ESP32/S2/S3) C frontend, so Xtensa is not covered.
RV32_FLAGS := --target=riscv32-unknown-elf -march=rv32imc -ffreestanding -std=c11 -Wall -Wextra \
	-Werror -O2 -I$(CORE_DIR)/include
cross:
	@mkdir -p $(BUILD)
	$(CLANG) $(RV32_FLAGS) -c $(CORE_SRC) -o $(BUILD)/rs_csi_core_rv32.o
	$(CLANG) $(RV32_FLAGS) -Wno-builtin-macro-redefined \
		-U__INT32_TYPE__ '-D__INT32_TYPE__=long int' \
		-U__UINT32_TYPE__ '-D__UINT32_TYPE__=long unsigned int' \
		-c $(CORE_SRC) -o $(BUILD)/rs_csi_core_rv32_int32long.o
	@echo "cross: rs_csi_core.c compiled for rv32imc (int32_t=int and int32_t=long) with clang"

clean:
	rm -rf $(BUILD)
