# rs_csi_core: platform-independent RoomSense protocol code (plain C11).
#
# Under ESP-IDF this is a normal component (found through EXTRA_COMPONENT_DIRS
# in ../../roomsense_csi_rx and ../../roomsense_csi_tx). It has no ESP-IDF
# dependencies on purpose, so the same sources are unit-tested on the host with
# firmware/esp32/host_tests/Makefile.
if(COMMAND idf_component_register)
    idf_component_register(SRCS "rs_csi_core.c"
                           INCLUDE_DIRS "include")
else()
    # Plain CMake fallback (not used by the host tests, which use make).
    cmake_minimum_required(VERSION 3.16)
    project(rs_csi_core C)
    add_library(rs_csi_core STATIC rs_csi_core.c)
    target_include_directories(rs_csi_core PUBLIC include)
    set_target_properties(rs_csi_core PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED ON)
endif()
