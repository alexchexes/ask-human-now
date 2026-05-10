"""Tests for optional dialog timing metadata."""

import asyncio
import datetime as dt

from ask_human_for_context_mcp import server


class StubDialogHandler:
    """Capture question payloads sent to the GUI layer."""

    def __init__(self):
        self.question = None
        self.timeout = None

    async def get_user_input(self, question, timeout):
        self.question = question
        self.timeout = timeout
        return "ok"


def test_build_timing_info_block_contains_note(monkeypatch):
    """Include both timestamps and the external-timeout note."""
    monkeypatch.setattr(
        server,
        "format_dialog_timestamp",
        lambda moment: moment.strftime("%Y-%m-%d %H:%M:%S"),
    )

    block = server.build_timing_info_block(dt.datetime(2026, 5, 10, 9, 30, 0), 90)

    assert block == (
        "Issued at: 2026-05-10 09:30:00\n"
        "Answer until: 2026-05-10 09:31:30\n"
        "Note: Actual wait may be shorter if your MCP client, agent, or other tooling "
        "enforces a lower timeout."
    )


def test_tool_keeps_default_prompt_shape_without_timing_info(monkeypatch):
    """Leave the dialog text unchanged when the flag is off."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", False)

    result = asyncio.run(server.asking_user_missing_context("Question?"))

    assert result == "✅ User response: ok"
    assert stub.timeout == server.DEFAULT_DIALOG_TIMEOUT_SECONDS
    assert stub.question == "❓ Question?"


def test_tool_appends_timing_info_when_enabled(monkeypatch):
    """Append timing metadata only when the CLI flag is enabled."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", True)
    monkeypatch.setattr(
        server,
        "build_timing_info_block",
        lambda issued_at, timeout_seconds: (
            "Issued at: 2026-05-10 09:30:00\n"
            "Answer until: 2026-05-10 09:31:30\n"
            "Note: Actual wait may be shorter if your MCP client, agent, or other tooling "
            "enforces a lower timeout."
        ),
    )

    result = asyncio.run(
        server.asking_user_missing_context(
            "Should I keep the current API shape?",
            "There are two valid implementation paths and the choice is user-facing.",
        )
    )

    assert result == "✅ User response: ok"
    assert "📋 Missing Context:" in stub.question
    assert "❓ Question:\nShould I keep the current API shape?" in stub.question
    assert "Issued at: 2026-05-10 09:30:00" in stub.question
    assert "Answer until: 2026-05-10 09:31:30" in stub.question
    assert "Actual wait may be shorter" in stub.question
