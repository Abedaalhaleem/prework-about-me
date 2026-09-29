#!/usr/bin/env bash
# header_syntax_check.sh: header-level check of the firmware sources. THIS IS NOT A BUILD.
#
# For each RISC-V target (esp32c3, esp32c5, esp32c6, esp32c61) it:
#   1. generates sdkconfig.h with ESP-IDF's own kconfgen (esp-idf-kconfig) from the
#      real ESP-IDF Kconfig tree plus this project's sdkconfig.defaults, and
#   2. runs `clang -fsyntax-only -Wall -Wextra -Werror -Wformat=2` on
#      roomsense_csi_rx/main/app_main.c (ESP-NOW mode and router mode) and
#      roomsense_csi_tx/main/app_main.c against the real ESP-IDF, esp-lwip and
#      newlib headers. int32_t/uint32_t are redefined as long / unsigned long, as
#      in the ESP-IDF >= 5.0 GCC toolchains, so printf-format mistakes show up.
#
# What it catches: wrong API names or signatures, missing struct members,
# undefined Kconfig symbols, type and format errors.
# What it cannot tell you: whether GCC (the real ESP-IDF compiler) accepts the
# code, whether it links, or whether it works. clang 18 has no Xtensa C
# frontend, so esp32 / esp32s2 / esp32s3 are not covered here.
#
# It never downloads or installs anything and never touches IDF_PATH.
#
# Environment:
#   IDF_PATH            ESP-IDF v5.5.5 checkout (a sparse checkout with every
#                       components/**/Kconfig* file and the needed headers is enough)
#   RS_LWIP_SRC         esp-lwip source root (default: $IDF_PATH/components/lwip/lwip)
#   RS_NEWLIB_INCLUDE   newlib "libc/include" directory from espressif/newlib-esp32,
#                       tag esp-4.3.0_20260121 (the newlib of toolchain esp-14.2.0_20260121)
#   RS_KCONFGEN_PYTHON  python that has esp-idf-kconfig installed (default: python3)
#   RS_TARGETS          default: "esp32c3 esp32c5 esp32c6 esp32c61"
#   CLANG               default: clang
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW="$(cd "$HERE/.." && pwd)"
: "${IDF_PATH:?set IDF_PATH to an ESP-IDF v5.5.5 checkout}"
IDF="$IDF_PATH"
C="$IDF/components"
LWIP_SRC="${RS_LWIP_SRC:-$IDF/components/lwip/lwip}"
: "${RS_NEWLIB_INCLUDE:?set RS_NEWLIB_INCLUDE to newlib-esp32/newlib/libc/include}"
PY="${RS_KCONFGEN_PYTHON:-python3}"
TARGETS="${RS_TARGETS:-esp32c3 esp32c5 esp32c6 esp32c61}"
CLANG="${CLANG:-clang}"
OUT_ROOT="$FW/host_tests/build/hdrcheck"

for need in "$IDF/Kconfig" "$LWIP_SRC/src/include/lwip/ip_addr.h" "$RS_NEWLIB_INCLUDE/stdio.h"; do
    [ -e "$need" ] || { echo "missing: $need" >&2; exit 2; }
done
"$PY" -c "import kconfgen" 2>/dev/null || { echo "esp-idf-kconfig not importable by $PY" >&2; exit 2; }
command -v "$CLANG" >/dev/null || { echo "clang not found" >&2; exit 2; }

mkdir -p "$OUT_ROOT"
printf 'CONFIG_RS_MODE_ROUTER_PING=y\n' > "$OUT_ROOT/router.defaults"

# gen_sdkconfig <target> <project_dir> <out_dir> [extra defaults file]
gen_sdkconfig() {
    local target=$1 proj=$2 out=$3 extra=${4:-}
    mkdir -p "$out"
    : > "$out/kconfigs.in"
    : > "$out/kconfigs_projbuild.in"
    local k
    for k in "$C"/*/Kconfig; do echo "source \"$k\"" >> "$out/kconfigs.in"; done
    for k in "$C"/*/Kconfig.projbuild; do echo "source \"$k\"" >> "$out/kconfigs_projbuild.in"; done
    echo "source \"$IDF/examples/common_components/protocol_examples_common/Kconfig.projbuild\"" \
        >> "$out/kconfigs_projbuild.in"
    echo "source \"$proj/main/Kconfig.projbuild\"" >> "$out/kconfigs_projbuild.in"
    local renames
    renames=$( (ls "$C"/*/sdkconfig.rename "$C"/*/sdkconfig.rename."$target" 2>/dev/null || true) \
        | paste -sd';')
    local defaults=(--defaults "$proj/sdkconfig.defaults")
    [ -n "$extra" ] && defaults+=(--defaults "$extra")
    rm -f "$out/sdkconfig"
    "$PY" -m kconfgen --list-separator=semicolon \
        --kconfig "$IDF/Kconfig" --sdkconfig-rename "$IDF/sdkconfig.rename" \
        --config "$out/sdkconfig" "${defaults[@]}" \
        --env "IDF_TARGET=$target" --env "IDF_PATH=$IDF" --env "IDF_TOOLCHAIN=gcc" \
        --env "IDF_VERSION=5.5.5" --env "IDF_ENV_FPGA=" --env "IDF_INIT_VERSION=5.5.5" \
        --env "COMPONENT_KCONFIGS_SOURCE_FILE=$out/kconfigs.in" \
        --env "COMPONENT_KCONFIGS_PROJBUILD_SOURCE_FILE=$out/kconfigs_projbuild.in" \
        --env "COMPONENT_SDKCONFIG_RENAMES=$renames" \
        --output header "$out/sdkconfig.h" --output config "$out/sdkconfig" \
        > "$out/kconfgen.log" 2>&1
}

# syntax_check <target> <source> <sdkconfig dir>
syntax_check() {
    local t=$1 src=$2 cfg=$3 march
    case "$t" in
        esp32c3) march=rv32imc_zicsr_zifencei ;;
        esp32c5 | esp32c6 | esp32c61) march=rv32imac_zicsr_zifencei ;;
        *) echo "unsupported target for this check: $t" >&2; return 2 ;;
    esac
    local inc=(
        "$cfg" "$FW/components/rs_csi_core/include"
        "$IDF/examples/common_components/protocol_examples_common/include"
        "$C/newlib/platform_include"
        "$C/freertos/config/include" "$C/freertos/config/include/freertos" "$C/freertos/config/riscv/include"
        "$C/freertos/FreeRTOS-Kernel/include" "$C/freertos/FreeRTOS-Kernel/portable/riscv/include"
        "$C/freertos/FreeRTOS-Kernel/portable/riscv/include/freertos" "$C/freertos/esp_additions/include"
        "$C/esp_hw_support/include" "$C/esp_hw_support/include/soc" "$C/esp_hw_support/include/soc/$t"
        "$C/esp_hw_support/dma/include" "$C/esp_hw_support/ldo/include" "$C/esp_hw_support/debug_probe/include"
        "$C/esp_hw_support/port/$t/." "$C/esp_hw_support/port/$t/include"
        "$C/heap/include" "$C/heap/tlsf" "$C/log/include"
        "$C/soc/include" "$C/soc/$t" "$C/soc/$t/include" "$C/soc/$t/register"
        "$C/hal/platform_port/include" "$C/hal/$t/include" "$C/hal/include"
        "$C/esp_rom/include" "$C/esp_rom/$t/include" "$C/esp_rom/$t/include/$t" "$C/esp_rom/$t"
        "$C/esp_common/include" "$C/esp_system/include" "$C/esp_system/port/soc"
        "$C/esp_system/port/include/private" "$C/riscv/include"
        "$C/esp_timer/include" "$C/esp_event/include" "$C/esp_netif/include"
        "$C/esp_wifi/include" "$C/esp_wifi/include/local" "$C/esp_wifi/wifi_apps/include"
        "$C/esp_wifi/wifi_apps/nan_app/include" "$C/esp_wifi/wifi_apps/roaming_app/include"
        "$C/esp_phy/include" "$C/esp_phy/$t/include" "$C/esp_coex/include" "$C/esp_pm/include"
        "$C/nvs_flash/include" "$C/esp_partition/include" "$C/spi_flash/include"
        "$C/lwip/include" "$C/lwip/include/apps" "$C/lwip/include/apps/sntp"
        "$LWIP_SRC/src/include" "$C/lwip/port/include" "$C/lwip/port/freertos/include"
        "$C/lwip/port/esp32xx/include" "$C/lwip/port/esp32xx/include/arch"
        "$C/lwip/port/esp32xx/include/sys"
        "$C/vfs/include" "$C/esp_driver_uart/include" "$C/esp_driver_gpio/include"
        "$C/esp_security/include" "$C/esp_mm/include" "$C/efuse/include" "$C/efuse/$t/include"
        "$C/esp_ringbuf/include"
    )
    local args=() d
    for d in "${inc[@]}"; do args+=("-I$d"); done
    "$CLANG" --target=riscv32-unknown-elf "-march=$march" -mabi=ilp32 -std=gnu17 -fsyntax-only \
        -nostdlibinc -isystem "$RS_NEWLIB_INCLUDE" \
        -Wno-builtin-macro-redefined -U__INT32_TYPE__ '-D__INT32_TYPE__=long int' \
        -U__UINT32_TYPE__ '-D__UINT32_TYPE__=long unsigned int' \
        -DESP_PLATFORM '-DIDF_VER="v5.5.5"' -D_GNU_SOURCE -D_POSIX_READER_WRITER_LOCKS \
        -DSOC_MMU_PAGE_SIZE=CONFIG_MMU_PAGE_SIZE -DSOC_XTAL_FREQ_MHZ=CONFIG_XTAL_FREQ \
        '-DRS_BUILD_ID="NA"' \
        -Wall -Wextra -Werror -Wno-unused-parameter -Wno-sign-compare -Wformat=2 \
        "${args[@]}" "$src"
}

pass=0
fail=0
for t in $TARGETS; do
    for combo in "rx_espnow:roomsense_csi_rx:" "rx_router:roomsense_csi_rx:$OUT_ROOT/router.defaults" \
        "tx:roomsense_csi_tx:"; do
        IFS=: read -r name proj extra <<< "$combo"
        out="$OUT_ROOT/$t/$name"
        if ! gen_sdkconfig "$t" "$FW/$proj" "$out" "$extra"; then
            echo "FAIL $t $name (kconfgen, see $out/kconfgen.log)"
            fail=$((fail + 1))
            continue
        fi
        if [ "$name" = "rx_router" ] && ! grep -q '^#define CONFIG_RS_MODE_ROUTER_PING 1' "$out/sdkconfig.h"; then
            echo "FAIL $t $name (router mode not selected)"
            fail=$((fail + 1))
            continue
        fi
        if syntax_check "$t" "$FW/$proj/main/app_main.c" "$out" > "$out/clang.log" 2>&1; then
            echo "PASS $t $name"
            pass=$((pass + 1))
        else
            echo "FAIL $t $name"
            sed 's/^/    /' "$out/clang.log" | head -20
            fail=$((fail + 1))
        fi
    done
done
echo "header_syntax_check: $pass passed, $fail failed (clang -fsyntax-only; NOT a firmware build)"
[ "$fail" -eq 0 ]
