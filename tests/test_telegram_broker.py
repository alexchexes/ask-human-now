"""Tests for Telegram broker state and broker-mode entrypoint behavior."""

import asyncio
import sys

from ask_human_now import server
from ask_human_now.broker_state import (
    load_broker_state,
    load_or_create_broker_identity,
    persist_broker_listen_url,
    resolve_broker_state_dir,
    resolve_target_broker_state_dir,
)
from ask_human_now.telegram_broker import (
    build_broker_health_payload,
    build_broker_listen_url,
    run_telegram_broker,
)
from ask_human_now.telegram_models import TelegramConfig, resolve_telegram_target_key


def test_broker_identity_is_stable_for_one_state_dir(tmp_path):
    """Persist one broker identity per state directory."""
    first_identity = load_or_create_broker_identity(tmp_path)
    second_identity = load_or_create_broker_identity(tmp_path)

    assert second_identity == first_identity


def test_broker_label_override_is_persisted(tmp_path):
    """Store an explicit broker label for later reuse."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="office-machine")
    reloaded_state = load_broker_state(tmp_path)

    assert identity.broker_label == "office-machine"
    assert reloaded_state is not None
    assert reloaded_state.identity.broker_label == "office-machine"


def test_broker_state_tracks_listen_url(tmp_path):
    """Persist the discovered local listen URL for health probes and reuse."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="desk")
    persist_broker_listen_url(tmp_path, "http://127.0.0.1:7456")
    state = load_broker_state(tmp_path)

    assert state is not None
    assert state.identity == identity
    assert state.listen_url == "http://127.0.0.1:7456"


def test_resolve_broker_state_dir_expands_placeholders(monkeypatch, tmp_path):
    """Support cwd placeholders in explicit broker state-dir configuration."""
    monkeypatch.chdir(tmp_path)

    resolved = resolve_broker_state_dir("{cwd}/broker-state")

    assert resolved == (tmp_path / "broker-state").resolve()


def test_build_broker_listen_url_normalizes_wildcard_host():
    """Store a loopback URL for local discovery when binding to all interfaces."""
    assert build_broker_listen_url("0.0.0.0", 7456) == "http://127.0.0.1:7456"
    assert build_broker_listen_url("127.0.0.1", 7456) == "http://127.0.0.1:7456"


def test_build_broker_health_payload_contains_identity_and_url(tmp_path):
    """Expose stable identity and listen URL in broker health responses."""
    identity = load_or_create_broker_identity(tmp_path, broker_label="laptop")

    payload = build_broker_health_payload(
        identity,
        listen_url="http://127.0.0.1:7456",
        target_key="feedbeef",
    )

    assert payload["status"] == "ok"
    assert payload["broker_id"] == identity.broker_id
    assert payload["broker_label"] == "laptop"
    assert payload["listen_url"] == "http://127.0.0.1:7456"
    assert payload["target_key"] == "feedbeef"
    assert "version" in payload


def test_main_runs_telegram_broker_mode(monkeypatch, tmp_path):
    """Dispatch to broker mode before MCP transport setup."""
    captured = {}

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ask-human",
            "--telegram-broker",
            "--telegram",
            "123456:ABCDEF -1009876543210",
            "--telegram-broker-label",
            "office",
            "--telegram-broker-state-dir",
            str(tmp_path),
            "--telegram-broker-host",
            "127.0.0.1",
            "--telegram-broker-port",
            "7456",
        ],
    )
    monkeypatch.setattr(
        server,
        "run_telegram_broker",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(server.mcp, "run", lambda transport: captured.setdefault("mcp_run", True))

    server.main()

    telegram_target = TelegramConfig("123456:ABCDEF", "-1009876543210")
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7456
    assert captured["broker_label"] == "office"
    assert captured["telegram_target"] == telegram_target
    assert captured["state_dir"] == resolve_target_broker_state_dir(
        tmp_path.resolve(), telegram_target
    )
    assert "mcp_run" not in captured


def test_run_telegram_broker_exits_cleanly_on_keyboard_interrupt(monkeypatch, tmp_path):
    """Treat Ctrl+C as a clean shutdown after Uvicorn finishes stopping."""

    class FakeSocket:
        def __init__(self):
            self.closed = False

        def setsockopt(self, *args):
            return None

        def bind(self, _address):
            return None

        def listen(self, _backlog):
            return None

        def getsockname(self):
            return ("127.0.0.1", 7456)

        def close(self):
            self.closed = True

    fake_socket = FakeSocket()

    class FakeServer:
        def __init__(self, config):
            self.config = config

        async def serve(self, sockets):
            assert sockets == [fake_socket]

    monkeypatch.setattr(
        "ask_human_now.telegram_broker.load_or_create_broker_identity",
        lambda state_dir, broker_label=None: load_or_create_broker_identity(
            state_dir, broker_label=broker_label
        ),
    )
    monkeypatch.setattr(
        "ask_human_now.telegram_broker._create_bound_socket",
        lambda host, port: fake_socket,
    )
    monkeypatch.setattr(
        "ask_human_now.telegram_broker.persist_broker_listen_url",
        lambda state_dir, listen_url: None,
    )
    monkeypatch.setattr(
        "ask_human_now.telegram_broker.create_telegram_broker_app",
        lambda identity, *, listen_url, telegram_client, target_key: object(),
    )
    monkeypatch.setattr(
        "ask_human_now.telegram_broker.uvicorn.Config",
        lambda app, host, port, log_level: {
            "app": app,
            "host": host,
            "port": port,
            "log_level": log_level,
        },
    )
    monkeypatch.setattr(
        "ask_human_now.telegram_broker.uvicorn.Server",
        FakeServer,
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda coroutine: (
            coroutine.close(),
            (_ for _ in ()).throw(KeyboardInterrupt()),
        )[1],
    )

    run_telegram_broker(
        host="127.0.0.1",
        port=0,
        state_dir=tmp_path,
        telegram_target=TelegramConfig("123456:ABCDEF", "-1009876543210"),
        broker_label="office",
    )

    assert fake_socket.closed is True
