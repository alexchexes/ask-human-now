"""Tests for Windows-specific DPI handling."""

import asyncio
import sys
import types

from ask_human_for_context_mcp.server import GUIDialogHandler


def test_enable_windows_dpi_awareness_prefers_shcore(monkeypatch):
    """Use per-monitor DPI awareness when the API is available."""

    calls = []

    class FakeShcore:
        def SetProcessDpiAwareness(self, value):
            calls.append(("shcore", value))

    class FakeUser32:
        def SetProcessDPIAware(self):
            calls.append(("user32", None))

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(shcore=FakeShcore(), user32=FakeUser32())
    )

    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    handler = GUIDialogHandler()
    handler._enable_windows_dpi_awareness()

    assert calls == [("shcore", 2)]


def test_configure_windows_tk_scaling_uses_window_dpi(monkeypatch):
    """Scale Tk based on the monitor DPI reported by Windows."""

    calls = []

    class FakeUser32:
        def GetDpiForWindow(self, hwnd):
            assert hwnd == 123
            return 120

    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(user32=FakeUser32()))

    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    class FakeRoot:
        def __init__(self):
            self.tk = types.SimpleNamespace(call=lambda *args: calls.append(args))

        def winfo_id(self):
            return 123

    handler = GUIDialogHandler()
    handler._configure_windows_tk_scaling(FakeRoot())

    assert calls == [("tk", "scaling", 120 / 72)]


def test_windows_dialog_applies_dpi_setup(monkeypatch):
    """Initialize DPI handling before showing the Tk dialog."""

    events = []

    class FakeRoot:
        def after(self, delay_ms, callback):
            events.append(("after", delay_ms, callback))
            return "timeout-id"

        def after_cancel(self, timeout_id):
            events.append(("after-cancel", timeout_id))

        def withdraw(self):
            events.append("withdraw")

        def destroy(self):
            events.append("destroy")

    fake_root = FakeRoot()

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = lambda: fake_root
    fake_tk.simpledialog = types.SimpleNamespace(
        askstring=lambda title, question, parent=None: events.append(
            ("askstring", title, question, parent)
        )
        or "ok"
    )

    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)

    handler = GUIDialogHandler()
    monkeypatch.setattr(
        handler, "_enable_windows_dpi_awareness", lambda: events.append("enable-dpi")
    )
    monkeypatch.setattr(
        handler, "_configure_windows_tk_scaling", lambda root: events.append(("scale", root))
    )
    monkeypatch.setattr(handler, "_set_windows_icon", lambda root: events.append(("icon", root)))

    result = asyncio.run(handler._windows_dialog("Question?", 10))

    assert result == "ok"
    assert events == [
        "enable-dpi",
        ("scale", fake_root),
        "withdraw",
        ("icon", fake_root),
        ("after", 10000, fake_root.destroy),
        ("askstring", "🤖 Cursor AI Assistant", "Question?", fake_root),
        ("after-cancel", "timeout-id"),
        "destroy",
    ]
