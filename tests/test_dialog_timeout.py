"""Tests for dialog timeout behavior."""

import asyncio
import os
import subprocess
import sys

from ask_human_now import server
from ask_human_now.server import (
    DEFAULT_DIALOG_TIMEOUT_SECONDS,
    GUIDialogHandler,
)


class FakeRoot:
    """Minimal Tk root stand-in for timeout scheduling tests."""

    def __init__(self):
        self.after_calls = []
        self.after_cancel_calls = []
        self.destroy_calls = 0

    def after(self, delay_ms, callback):
        self.after_calls.append((delay_ms, callback))
        return "timeout-id"

    def after_cancel(self, timeout_id):
        self.after_cancel_calls.append(timeout_id)

    def destroy(self):
        self.destroy_calls += 1


class FakeSimpleDialog:
    """Minimal simpledialog stand-in that records askstring arguments."""

    def __init__(self):
        self.calls = []

    def askstring(self, title, question, parent=None):
        self.calls.append((title, question, parent))
        return "typed answer"


def test_tool_uses_configured_dialog_timeout(monkeypatch):
    """Use the configured dialog timeout instead of a hardcoded 90 seconds."""

    class StubDialogHandler:
        def __init__(self):
            self.timeout = None

        async def get_user_input(self, question, timeout):
            self.timeout = timeout
            return "ok"

    stub = StubDialogHandler()
    monkeypatch.setattr(server, "dialog_handler", stub)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 1200)

    result = asyncio.run(server.asking_user_missing_context("Question?"))

    assert result == "✅ User response: ok"
    assert stub.timeout == 1200


def test_windows_string_dialog_schedules_timeout():
    """Schedule timeout inside Tk so Windows askstring is no longer unbounded."""
    handler = GUIDialogHandler()
    root = FakeRoot()
    simpledialog = FakeSimpleDialog()

    result = handler._ask_windows_string(
        root,
        simpledialog,
        "Title",
        "Question?",
        1200,
    )

    assert result == "typed answer"
    assert root.after_calls == [(1200 * 1000, root.destroy)]
    assert root.after_cancel_calls == ["timeout-id"]
    assert simpledialog.calls == [("Title", "Question?", root)]


def test_help_mentions_timeout_option():
    """Expose the dialog timeout option in CLI help."""
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    env = os.environ.copy()
    src_dir = os.path.join(repo_root, "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        src_dir if not existing_pythonpath else src_dir + os.pathsep + existing_pythonpath
    )

    result = subprocess.run(
        [sys.executable, "-m", "ask_human_now", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0
    assert "--timeout-seconds" in result.stdout
    assert str(DEFAULT_DIALOG_TIMEOUT_SECONDS) in result.stdout
