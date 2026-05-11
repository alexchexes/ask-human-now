"""Persistent state helpers for the local Telegram broker."""

import os
import platform
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_BROKER_STATE_DIR_NAME = "ask-human-for-context-mcp"
BROKER_STATE_DB_FILENAME = "telegram-broker.sqlite3"


@dataclass(frozen=True)
class TelegramBrokerIdentity:
    """Stable identity and human-facing label for one broker installation."""

    broker_id: str
    broker_label: str


@dataclass(frozen=True)
class TelegramBrokerState:
    """Persisted state that local clients can use for broker discovery."""

    identity: TelegramBrokerIdentity
    listen_url: Optional[str]


def resolve_broker_state_dir(state_dir: Optional[str] = None) -> Path:
    """Resolve the broker state directory from CLI input or platform defaults."""
    if state_dir is not None and state_dir.strip():
        expanded = state_dir.replace("{cwd}", os.getcwd())
        expanded = os.path.expandvars(expanded)
        expanded = os.path.expanduser(expanded)
        return Path(expanded).resolve()

    system_name = platform.system()
    if system_name == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return (Path(root) / DEFAULT_BROKER_STATE_DIR_NAME).resolve()
        return (Path.home() / "AppData" / "Local" / DEFAULT_BROKER_STATE_DIR_NAME).resolve()

    if system_name == "Darwin":
        return (
            Path.home() / "Library" / "Application Support" / DEFAULT_BROKER_STATE_DIR_NAME
        ).resolve()

    root = os.environ.get("XDG_STATE_HOME")
    if root:
        return (Path(root) / DEFAULT_BROKER_STATE_DIR_NAME).resolve()
    return (Path.home() / ".local" / "state" / DEFAULT_BROKER_STATE_DIR_NAME).resolve()


def resolve_default_broker_label() -> str:
    """Choose a human-friendly default label for the local broker."""
    hostname = socket.gethostname().strip()
    return hostname or "local-broker"


def _ensure_state_db(state_dir: Path) -> sqlite3.Connection:
    """Open the broker state database and initialize its schema if needed."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / BROKER_STATE_DB_FILENAME
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _get_state_value(connection: sqlite3.Connection, key: str) -> Optional[str]:
    """Read a single broker state value by key."""
    row = connection.execute(
        "SELECT value FROM broker_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _set_state_value(connection: sqlite3.Connection, key: str, value: str) -> None:
    """Persist a single broker state value."""
    connection.execute(
        """
        INSERT INTO broker_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    connection.commit()


def load_or_create_broker_identity(
    state_dir: Path,
    broker_label: Optional[str] = None,
) -> TelegramBrokerIdentity:
    """Load a stable broker identity, creating and persisting one if absent."""
    connection = _ensure_state_db(state_dir)
    try:
        broker_id = _get_state_value(connection, "broker_id")
        stored_label = _get_state_value(connection, "broker_label")

        if broker_id is None:
            broker_id = uuid.uuid4().hex
            _set_state_value(connection, "broker_id", broker_id)

        resolved_label = broker_label.strip() if broker_label is not None else None
        if not resolved_label:
            resolved_label = stored_label or resolve_default_broker_label()

        if stored_label != resolved_label:
            _set_state_value(connection, "broker_label", resolved_label)

        return TelegramBrokerIdentity(broker_id=broker_id, broker_label=resolved_label)
    finally:
        connection.close()


def persist_broker_listen_url(state_dir: Path, listen_url: str) -> None:
    """Store the broker's current listening URL for local discovery."""
    connection = _ensure_state_db(state_dir)
    try:
        _set_state_value(connection, "listen_url", listen_url)
    finally:
        connection.close()


def load_broker_state(state_dir: Path) -> Optional[TelegramBrokerState]:
    """Read the persisted broker identity and listening URL, if available."""
    connection = _ensure_state_db(state_dir)
    try:
        broker_id = _get_state_value(connection, "broker_id")
        broker_label = _get_state_value(connection, "broker_label")
        listen_url = _get_state_value(connection, "listen_url")
    finally:
        connection.close()

    if broker_id is None or broker_label is None:
        return None

    identity = TelegramBrokerIdentity(broker_id=broker_id, broker_label=broker_label)
    return TelegramBrokerState(identity=identity, listen_url=listen_url)
