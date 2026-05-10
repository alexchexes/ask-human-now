"""Tests for macOS dialog behavior."""

import asyncio
from unittest.mock import patch

from ask_human_for_context_mcp.server import GUIDialogHandler


class FakeProcess:
    """Minimal async subprocess stub for dialog tests."""

    returncode = 0

    async def communicate(self):
        """Return a timeout-style AppleScript response."""
        return (b"gave up:true", b"")


def test_macos_dialog_uses_configured_title():
    """Build the macOS dialog script without missing imports."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    handler = GUIDialogHandler("Custom Title")

    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        result = asyncio.run(handler._macos_dialog("Question?", 10))

    assert result is None
    assert captured["args"][0] == "osascript"
    assert captured["args"][1] == "-e"
    script = captured["args"][2]
    assert 'with title "Custom Title"' in script
    assert "giving up after 10" in script
