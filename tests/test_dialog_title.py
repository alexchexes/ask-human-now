"""Tests for dialog title configuration."""

import os
import subprocess
import sys

from ask_human_now.dialogs import GUIDialogHandler
from ask_human_now.prompt_formatting import DEFAULT_DIALOG_TITLE, resolve_dialog_title


def test_resolve_dialog_title_defaults():
    """Use the built-in title when no override is present."""
    assert resolve_dialog_title() == DEFAULT_DIALOG_TITLE


def test_resolve_dialog_title_uses_explicit_value():
    """Use the explicit startup option when provided."""
    assert resolve_dialog_title("CLI Title") == "CLI Title"


def test_handler_uses_resolved_dialog_title():
    """Initialize handlers with the resolved title."""
    assert GUIDialogHandler().dialog_title == DEFAULT_DIALOG_TITLE
    assert GUIDialogHandler("Custom Title").dialog_title == "Custom Title"


def test_help_mentions_dialog_title_option():
    """Expose the persistent title option in CLI help."""
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
    assert "--dialog-title" in result.stdout
