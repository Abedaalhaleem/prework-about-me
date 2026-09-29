idf_component_register(SRCS "app_main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES rs_csi_core esp_wifi esp_netif esp_event esp_timer
                                     nvs_flash lwip protocol_examples_common)

# RSHELLO field 16 (build_id): `git describe` of this firmware tree, or NA.
# Evaluated when CMake configures the project; run `idf.py reconfigure` to refresh
# it. Only [A-Za-z0-9._-] is accepted so the value can never break the line format.
set(rs_build_id "NA")
find_package(Git QUIET)
if(GIT_FOUND)
    execute_process(
        COMMAND "${GIT_EXECUTABLE}" describe --always --dirty --abbrev=7
        WORKING_DIRECTORY "${COMPONENT_DIR}"
        OUTPUT_VARIABLE rs_git_describe
        RESULT_VARIABLE rs_git_result
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET)
    if(rs_git_result EQUAL 0 AND rs_git_describe MATCHES "^[A-Za-z0-9._-]+$")
        set(rs_build_id "${rs_git_describe}")
    endif()
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE RS_BUILD_ID="${rs_build_id}")
