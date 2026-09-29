"""Log messages are bounded, but clipping keeps the end of the message,
because the last line of a traceback names the error (e.g. DATA_DIR_LOCKED)."""

from __future__ import annotations

from roomsense.logging_setup import MAX_MESSAGE_CHARS, _clip


def test_short_messages_are_untouched() -> None:
    assert _clip("hello") == "hello"


def test_long_messages_keep_head_and_tail() -> None:
    text = "Traceback (most recent call last):\n" + "x" * (3 * MAX_MESSAGE_CHARS) + "\nDATA_DIR_LOCKED: in use"
    out = _clip(text)
    assert len(out) <= MAX_MESSAGE_CHARS
    assert out.startswith("Traceback")
    assert out.endswith("DATA_DIR_LOCKED: in use")
    assert "chars omitted" in out
