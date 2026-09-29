"""WiFi RoomSense 3D backend.

Local-only, experimental Wi-Fi CSI acquisition, motion detection, and gated
zone estimation. See ../README.md and ../docs/ARCHITECTURE.md.
"""

__version__ = "0.1.0"

# Version of the typed record schemas in roomsense.schemas. Bump the minor
# version for additive changes and the major version for incompatible ones.
SCHEMA_VERSION = "1.0.0"

# Version of the host-side CSI line parser (roomsense.acquisition.parser).
# Recorded on every frame so that recordings can be re-parsed/audited later.
PARSER_VERSION = "roomsense-parser/1.0.0"
