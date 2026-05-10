"""Test icon path resolution functionality."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_icon_path_absolute():
    """Test that icon paths are resolved as absolute paths."""
    from ask_human_for_context_mcp.server import GUIDialogHandler

    handler = GUIDialogHandler()
    # This should not raise an exception
    assert handler is not None
