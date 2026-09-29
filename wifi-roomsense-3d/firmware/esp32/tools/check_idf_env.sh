#!/usr/bin/env bash
# check_idf_env.sh: refuse to build the RoomSense firmware with anything other
# than the pinned ESP-IDF v5.5.5, unless RS_ALLOW_OTHER_IDF=1 is set.
#
# Read-only. It never downloads, installs or changes anything.
#
# Checks:
#   1. IDF_PATH is set and looks like an ESP-IDF tree (cannot be overridden).
#   2. $IDF_PATH/tools/cmake/version.cmake says 5.5.5 (parsed the same way as
#      esp-csi's tools/ci/get_idf_ver.sh, extended with the patch number).
#   3. If IDF_PATH is a git checkout: HEAD is the v5.5.5 tag commit.
# Warnings only: uncommitted changes in IDF_PATH, idf.py not on PATH.
#
# Usage:  tools/check_idf_env.sh            (exit 0 = OK to build)
#         RS_ALLOW_OTHER_IDF=1 tools/check_idf_env.sh
set -u

EXPECTED_VERSION="5.5.5"
EXPECTED_TAG="v5.5.5"
EXPECTED_COMMIT="b774170ff46c393eeb5e495ea37936038d3f4f4f"

say() { echo "check_idf_env: $*"; }
overridden=0

# A version mismatch is fatal unless the user explicitly accepts it.
mismatch() {
    say "ERROR: $*" >&2
    if [ "${RS_ALLOW_OTHER_IDF:-0}" = "1" ]; then
        say "RS_ALLOW_OTHER_IDF=1: continuing with an UNSUPPORTED ESP-IDF. The firmware was" >&2
        say "written against ${EXPECTED_TAG}; results are not comparable and nothing was verified for it." >&2
        overridden=1
        return 0
    fi
    say "refusing. Use ESP-IDF ${EXPECTED_TAG} (commit ${EXPECTED_COMMIT})," >&2
    say "or set RS_ALLOW_OTHER_IDF=1 to override at your own risk." >&2
    exit 1
}

if [ -z "${IDF_PATH:-}" ]; then
    say "ERROR: IDF_PATH is not set. Activate ESP-IDF ${EXPECTED_TAG} first:  . \$IDF_PATH/export.sh" >&2
    exit 1
fi
version_file="${IDF_PATH}/tools/cmake/version.cmake"
if [ ! -f "$version_file" ]; then
    say "ERROR: ${version_file} not found; IDF_PATH does not look like an ESP-IDF tree." >&2
    exit 1
fi

get_part() {
    grep -E "^set\(IDF_VERSION_$1 [0-9]+\)" "$version_file" | head -n1 |
        sed -E "s/.*set\(IDF_VERSION_$1 ([0-9]+)\).*/\1/"
}
major=$(get_part MAJOR)
minor=$(get_part MINOR)
patch=$(get_part PATCH)
if [ -z "$major" ] || [ -z "$minor" ] || [ -z "$patch" ]; then
    mismatch "could not read the version from ${version_file}"
    found="unknown"
else
    found="${major}.${minor}.${patch}"
    if [ "$found" != "$EXPECTED_VERSION" ]; then
        mismatch "ESP-IDF ${found} found at ${IDF_PATH}, ${EXPECTED_VERSION} required"
    fi
fi

# Only trust git if IDF_PATH is itself the root of a checkout (not a folder
# that merely sits inside some other repository).
idf_real=$(cd "$IDF_PATH" && pwd -P)
git_top=$(git -C "$IDF_PATH" rev-parse --show-toplevel 2>/dev/null || true)
if [ -n "$git_top" ] && [ "$(cd "$git_top" && pwd -P)" = "$idf_real" ]; then
    head=$(git -C "$IDF_PATH" rev-parse HEAD 2>/dev/null || echo unknown)
    describe=$(git -C "$IDF_PATH" describe --tags --dirty 2>/dev/null || echo unknown)
    if [ "$head" != "$EXPECTED_COMMIT" ]; then
        mismatch "IDF_PATH HEAD is ${head} (${describe}), expected ${EXPECTED_COMMIT} (${EXPECTED_TAG})"
    fi
    case "$describe" in
        *-dirty) say "WARNING: ${IDF_PATH} has uncommitted changes (${describe})." >&2 ;;
    esac
else
    say "note: ${IDF_PATH} is not the root of a git checkout (e.g. a release archive); only version.cmake was checked."
fi

if ! command -v idf.py >/dev/null 2>&1; then
    say "WARNING: idf.py is not on PATH. Run:  . \$IDF_PATH/export.sh" >&2
fi

if [ "$overridden" = "1" ]; then
    say "ESP-IDF ${found} at ${IDF_PATH}: UNSUPPORTED, continuing only because RS_ALLOW_OTHER_IDF=1."
else
    say "ESP-IDF ${found} at ${IDF_PATH}: OK for roomsense firmware (pinned ${EXPECTED_TAG})."
fi
exit 0
