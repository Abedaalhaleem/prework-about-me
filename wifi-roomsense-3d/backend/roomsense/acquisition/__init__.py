"""RoomSense acquisition package.

Modules (import them directly; this package does not import them eagerly so
that, e.g., the parser can be used without loading pyserial or scipy):

* :mod:`.base` - source abstraction and event types (shared contract)
* :mod:`.parser` - strict line parsers and :class:`~.parser.FrameBuilder`
* :mod:`.rollover` - counter / timestamp unwrapping
* :mod:`.serial_source` - ``LIVE`` source (USB serial) and port listing
* :mod:`.replay_source` - ``REPLAY`` source for recordings
* :mod:`.synthetic` - ``SIMULATION`` source (a toy model, never a measurement)
* :mod:`.manager` - the single, explicitly selected active source
* :mod:`.alignment` - host/device clock diagnostics (no RF synchronisation)
"""

__all__ = ["base", "parser", "rollover", "serial_source", "replay_source", "synthetic", "manager", "alignment"]
