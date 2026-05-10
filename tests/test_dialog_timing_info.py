"""Tests for optional dialog timing metadata."""

import asyncio
import datetime as dt
import sys

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
        lambda moment: moment.strftime("%d.%m.%Y %H:%M:%S"),
    )

    block = server.build_timing_info_block(dt.datetime(2026, 5, 10, 9, 30, 0), 90)

    assert block == (
        "Issued at: 10.05.2026 09:30:00"
        " | Answer until: 10.05.2026 09:31:30"
        " (actual wait may be shorter)"
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


def test_initialize_time_locale_uses_system_default(monkeypatch):
    """Initialize time formatting from the OS locale once at startup."""
    calls = []

    monkeypatch.setattr(
        server.locale,
        "setlocale",
        lambda category, value=None: calls.append((category, value)) or "ok",
    )

    server.initialize_time_locale()

    assert calls == [(server.locale.LC_TIME, "")]


def test_format_dialog_timestamp_uses_locale_short_format():
    """Use locale-driven short date and time formatting when available."""

    class FakeMoment:
        def astimezone(self):
            return self

        def strftime(self, pattern):
            if pattern == "%x %X":
                return "10.05.2026 18:59:32"
            raise AssertionError(f"unexpected pattern: {pattern}")

    assert server.format_dialog_timestamp(FakeMoment()) == "10.05.2026 18:59:32"


def test_main_initializes_time_locale_before_running(monkeypatch):
    """Initialize locale once during startup before serving requests."""
    events = []

    monkeypatch.setattr(sys, "argv", ["ask-human-for-context-mcp", "--transport", "stdio"])
    monkeypatch.setattr(server, "initialize_time_locale", lambda: events.append("locale"))
    monkeypatch.setattr(server.mcp, "run", lambda transport: events.append(("run", transport)))

    server.main()

    assert events == ["locale", ("run", "stdio")]


def test_tool_appends_timing_info_when_enabled(monkeypatch):
    """Append timing metadata only when the CLI flag is enabled."""
    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "show_timing_info", True)
    monkeypatch.setattr(
        server,
        "build_timing_info_block",
        lambda issued_at, timeout_seconds: "Issued at: 10.05.2026 09:30:00 | "
        "Answer until: 10.05.2026 09:31:30 (actual wait may be shorter)",
    )

    result = asyncio.run(
        server.asking_user_missing_context(
            "Should I keep the current API shape?",
            "There are two valid implementation paths and the choice is user-facing.",
        )
    )

    assert result == "✅ User response: ok"
    separator = "─" * 40
    assert stub.question == (
        "Context:\n"
        "There are two valid implementation paths and the choice is user-facing.\n\n"
        f"{separator}\n\n"
        "Question:\n"
        "Should I keep the current API shape?\n\n"
        f"{separator}\n\n"
        "Issued at: 10.05.2026 09:30:00 | Answer until: 10.05.2026 09:31:30 "
        "(actual wait may be shorter)"
    )
