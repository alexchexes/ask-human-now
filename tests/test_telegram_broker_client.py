"""Tests for client-side local Telegram broker discovery and prompting."""

import asyncio
import datetime as dt

from ask_human_now.broker_state import TelegramBrokerHealth
from ask_human_now.telegram_broker_client import TelegramBrokerClient
from ask_human_now.telegram_models import TelegramConfig


def test_broker_client_builds_prompt_with_broker_metadata(monkeypatch, tmp_path):
    """Use broker health metadata when formatting Telegram prompts."""
    client = TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    captured = {}

    async def fake_ensure_local_broker():
        return TelegramBrokerHealth(
            broker_id="abcd1234",
            broker_label="Alex Laptop",
            listen_url="http://127.0.0.1:7456",
            target_key="feedbeef",
        )

    async def fake_broker_request(listen_url, path, payload, *, timeout, method="POST"):
        captured["listen_url"] = listen_url
        captured["path"] = path
        captured["payload"] = payload
        captured["timeout"] = timeout
        captured["method"] = method
        return {"status": "ok", "response": "telegram answer"}

    monkeypatch.setattr(client, "_ensure_local_broker", fake_ensure_local_broker)
    monkeypatch.setattr(client, "_broker_request", fake_broker_request)

    result = asyncio.run(
        client.ask_question(
            "Prompt text",
            "Context text.",
            prompt_id="QTEST-1234",
            timeout_seconds=300,
            include_timing_info=True,
            issued_at=dt.datetime(2026, 5, 11, 10, 0, 0),
        )
    )

    assert result == "telegram answer"
    assert captured["listen_url"] == "http://127.0.0.1:7456"
    assert captured["path"] == "prompts"
    assert captured["method"] == "POST"
    assert "Prompt ID: QTEST-1234" in captured["payload"]["prompt_text"]
    assert "Broker: Alex Laptop [abcd1234]" in captured["payload"]["prompt_text"]
    assert captured["payload"]["download_dir"] == str((tmp_path / "downloads").resolve())


def test_broker_client_waits_for_started_broker(monkeypatch, tmp_path):
    """Start a local broker and then reuse its persisted healthy state."""
    client = TelegramBrokerClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    health = TelegramBrokerHealth(
        broker_id="abcd1234",
        broker_label="Alex Laptop",
        listen_url="http://127.0.0.1:7456",
        target_key="feedbeef",
    )
    calls = {"probe": 0, "spawn": 0}

    async def fake_probe_persisted_broker():
        calls["probe"] += 1
        if calls["probe"] <= 2:
            return None
        return health

    async def fake_wait_for_local_broker():
        return health

    monkeypatch.setattr(client, "_probe_persisted_broker", fake_probe_persisted_broker)
    monkeypatch.setattr(client, "_wait_for_local_broker", fake_wait_for_local_broker)
    monkeypatch.setattr(
        client,
        "_spawn_local_broker",
        lambda: calls.__setitem__("spawn", calls["spawn"] + 1),
    )

    result = asyncio.run(client._ensure_local_broker())

    assert result == health
    assert calls["spawn"] == 1
    assert calls["probe"] >= 2


def test_broker_client_target_state_dir_is_per_target(tmp_path):
    """Separate brokers by Telegram target so different bots can coexist locally."""
    first_client = TelegramBrokerClient(
        TelegramConfig("111:AAA", "-1001"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )
    second_client = TelegramBrokerClient(
        TelegramConfig("222:BBB", "-1001"),
        tmp_path / "downloads",
        broker_state_root=tmp_path / "state",
    )

    assert first_client.target_state_dir != second_client.target_state_dir
