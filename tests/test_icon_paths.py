"""Test icon path resolution functionality."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_icon_path_absolute():
    """Test that icon paths are resolved as absolute paths."""
    from ask_human_now.dialogs import PACKAGE_ASSETS_DIR, GUIDialogHandler

    handler = GUIDialogHandler()
    # This should not raise an exception
    assert handler is not None
    assert PACKAGE_ASSETS_DIR.is_absolute()


def test_packaged_icons_exist():
    """Keep runtime and Telegram bot icons packaged with the Python module."""
    from ask_human_now.dialogs import PACKAGE_ASSETS_DIR

    assert (PACKAGE_ASSETS_DIR / "agent-asks.icns").is_file()
    assert (PACKAGE_ASSETS_DIR / "agent-asks.ico").is_file()
    assert (PACKAGE_ASSETS_DIR / "agent-asks.png").is_file()
    assert (PACKAGE_ASSETS_DIR / "icon-color-round.png").is_file()
    assert (PACKAGE_ASSETS_DIR / "icon-color-round-alt.png").is_file()
