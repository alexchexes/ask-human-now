"""Tests for dialog timeout behavior."""

import asyncio
import os
import subprocess
import sys

from ask_human_for_context_mcp import server
from ask_human_for_context_mcp.server import (
    DEFAULT_DIALOG_TIMEOUT_SECONDS,
    GUIDialogHandler,
)


class FakeRoot:
    """Minimal Tk root stand-in for visibility sequencing tests."""

    def __init__(self, viewable):
        self.viewable = viewable

    def winfo_viewable(self):
        return self.viewable


class FakeDialog:
    """Minimal Tk dialog stand-in that records method calls."""

    def __init__(self):
        self.calls = []

    def transient(self, root):
        self.calls.append("transient")

    def lift(self):
        self.calls.append("lift")

    def wait_visibility(self):
        self.calls.append("wait_visibility")

    def grab_set(self):
        self.calls.append("grab_set")


class FakeFocus:
    """Minimal focus target stand-in."""

    def __init__(self):
        self.calls = []

    def focus_set(self):
        self.calls.append("focus_set")


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


def test_windows_dialog_geometry_is_centered_and_bounded():
    """Keep the dialog a usable size on normal desktop displays."""
    handler = GUIDialogHandler()

    assert handler._get_windows_dialog_geometry(1920, 1080) == "760x520+580+280"


def test_windows_prompt_height_is_bounded():
    """Long questions should scroll instead of expanding the whole dialog."""
    handler = GUIDialogHandler()
    long_question = "x" * 4000

    assert handler._get_windows_prompt_height("Short question?") == 3
    assert handler._get_windows_prompt_height(long_question) == 8


def test_windows_dialog_does_not_make_hidden_root_transient():
    """A transient child of a withdrawn Tk root can be opened invisibly."""
    handler = GUIDialogHandler()
    root = FakeRoot(viewable=False)
    dialog = FakeDialog()
    focus = FakeFocus()

    handler._show_windows_dialog(root, dialog, focus)

    assert dialog.calls == ["lift", "wait_visibility", "grab_set"]
    assert focus.calls == ["focus_set"]


def test_windows_dialog_uses_transient_for_viewable_root():
    """Keep normal transient behavior when the parent window is visible."""
    handler = GUIDialogHandler()
    root = FakeRoot(viewable=True)
    dialog = FakeDialog()
    focus = FakeFocus()

    handler._show_windows_dialog(root, dialog, focus)

    assert dialog.calls == ["transient", "lift", "wait_visibility", "grab_set"]
    assert focus.calls == ["focus_set"]


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
        [sys.executable, "-m", "ask_human_for_context_mcp", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )

    assert result.returncode == 0
    assert "--timeout-seconds" in result.stdout
    assert str(DEFAULT_DIALOG_TIMEOUT_SECONDS) in result.stdout
