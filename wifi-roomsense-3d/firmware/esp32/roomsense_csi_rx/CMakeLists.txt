# RoomSense CSI receiver (ESP-IDF project). Pinned/documented: ESP-IDF v5.5.5.
# See ../README.md for build commands. Nothing here downloads managed components.
cmake_minimum_required(VERSION 3.16)

# ../components            -> rs_csi_core (protocol code shared with the host tests)
# protocol_examples_common -> example_connect() for router mode (Wi-Fi credentials
#                             are entered in menuconfig and stay in the local,
#                             git-ignored sdkconfig; they are never committed).
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../components"
    "$ENV{IDF_PATH}/examples/common_components/protocol_examples_common")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(roomsense_csi_rx)
