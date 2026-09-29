idf_component_register(SRCS "app_main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES rs_csi_core esp_wifi esp_netif esp_event nvs_flash)
