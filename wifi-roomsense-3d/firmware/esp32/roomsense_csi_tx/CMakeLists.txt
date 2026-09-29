# RoomSense ESP-NOW transmitter (ESP-IDF project). Pinned/documented: ESP-IDF v5.5.5.
# See ../README.md for build commands.
cmake_minimum_required(VERSION 3.16)

# ../components -> rs_csi_core (builds the 12-byte RSTX payload).
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(roomsense_csi_tx)
