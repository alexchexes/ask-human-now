"""Test MCP server functionality."""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_mcp_server_exists():
    """Test that MCP server can be initialized."""
    from ask_human_now.server import mcp

    assert mcp is not None
    assert mcp.name == "ask-human"


def test_asking_user_missing_context_tool():
    """Test that the main tool function exists."""
    from ask_human_now.server import asking_user_missing_context

    assert callable(asking_user_missing_context)
