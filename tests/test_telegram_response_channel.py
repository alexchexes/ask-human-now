"""Tests for Telegram response-channel support."""

import asyncio
import datetime as dt

import pytest

from ask_human_now import server
from ask_human_now.broker_state import TelegramBrokerIdentity
from ask_human_now.prompt_formatting import (
    build_dialog_telegram_notice,
    build_telegram_prompt_text,
)
from ask_human_now.telegram_client import TelegramPromptClient
from ask_human_now.telegram_models import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    TelegramConfig,
    parse_telegram_target,
)


def test_parse_telegram_target_parses_token_and_chat_id():
    """Parse the single CLI argument into token and chat id."""
    config = parse_telegram_target("123456:ABCDEF -1009876543210")

    assert config == TelegramConfig(
        bot_token="123456:ABCDEF",
        chat_id="-1009876543210",
    )


def test_parse_telegram_target_rejects_invalid_shape():
    """Reject telegram target values without both pieces."""
    with pytest.raises(ValueError):
        parse_telegram_target("123456:ABCDEF")


def test_build_dialog_telegram_notice_is_platform_specific():
    """Use the Windows stale-dialog warning only on Windows."""
    assert build_dialog_telegram_notice("Linux") == "📨 Also sent to Telegram."
    assert "will stay open" in build_dialog_telegram_notice("Windows")


def test_build_telegram_prompt_text_adds_prompt_id_and_reply_instruction():
    """Format Telegram prompts with compact metadata and reply guidance."""
    prompt_text = build_telegram_prompt_text(
        "Prompt text",
        "Context text.",
        prompt_id="QTEST-1234",
        timeout_seconds=300,
        include_timing_info=True,
        broker_label="Alex Laptop",
        broker_id="abcd1234",
    )

    assert "<b>📋 Context:</b>" in prompt_text
    assert "<b>❓ Question:</b>" in prompt_text
    assert "<blockquote expandable>" in prompt_text
    assert "Prompt ID: QTEST-1234" in prompt_text
    assert "Broker: Alex Laptop [abcd1234]" in prompt_text
    assert '↩️ Use "Reply" on this message to answer.' in prompt_text


def test_telegram_client_resolves_reply_to_sent_message(monkeypatch, tmp_path):
    """Resolve a Telegram reply that references the sent prompt message and ack it."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "telegram answer"
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_confirms_consumed_updates_before_stopping(monkeypatch, tmp_path):
    """Perform one final offset-advancing poll so consumed replies do not replay after restart."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 201,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]
    get_updates_payloads = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 301}
        if method == "getUpdates":
            get_updates_payloads.append(payload.copy())
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "telegram answer"
    assert [payload["offset"] for payload in get_updates_payloads] == [None, 2]
    assert [payload["timeout"] for payload in get_updates_payloads] == [
        DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
        0,
    ]


def test_telegram_client_rejects_too_large_file_and_waits_for_valid_text(monkeypatch, tmp_path):
    """Send a retry error for oversized files and keep waiting for a valid reply."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "document": {
                        "file_id": "file-too-big",
                        "file_size": 25 * 1024 * 1024,
                        "file_name": "huge.zip",
                    },
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "fallback text answer",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "fallback text answer"
    assert any("File too large for [QTEST-1234]" in payload["text"] for payload in sent_messages)
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_warns_once_for_non_reply_message(monkeypatch, tmp_path):
    """Hint once when the user sends a non-reply message while a local prompt is pending."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 210,
                    "chat": {"id": -1009876543210},
                    "text": "plain message without Reply",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 211,
                    "chat": {"id": -1009876543210},
                    "text": "another plain message without Reply",
                },
            }
        ],
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 212,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "proper reply",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 302 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "proper reply"
    assert [payload["text"] for payload in sent_messages].count(
        TelegramPromptClient.NON_REPLY_HINT_TEXT
    ) == 1
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_telegram_client_warns_for_stale_local_reply(monkeypatch, tmp_path):
    """Warn when the user replies to one of this broker's own older inactive prompts."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 220,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {
                        "message_id": 100,
                        "text": (
                            "<blockquote expandable>\n"
                            "Answers support text or files up to 20 MB.\n"
                            "Prompt ID: QOLD-0001\n"
                            "Broker: Alex Laptop [abcd1234]\n"
                            "</blockquote>"
                        ),
                    },
                    "text": "answer to old prompt",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 221,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 400 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    assert any(
        payload["text"]
        == "⚠️ Message is ignored. Prompt [QOLD-0001] is no longer active. Ask the agent to send a new question."
        for payload in sent_messages
    )
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_stale_reply_warning_stays_silent_without_reply_text(monkeypatch, tmp_path):
    """Do not guess stale ownership if Telegram does not provide replied-to prompt text."""
    client = TelegramPromptClient(
        TelegramConfig("123456:ABCDEF", "-1009876543210"),
        tmp_path,
        broker_identity=TelegramBrokerIdentity("abcd1234", "Alex Laptop"),
    )
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 230,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 100},
                    "text": "reply without nested prompt text",
                },
            }
        ],
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 231,
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "answer to current prompt",
                },
            }
        ],
        [],
    ]
    sent_messages = []

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            sent_messages.append(payload)
            if "parse_mode" in payload:
                return {"message_id": 101}
            return {"message_id": 500 + len(sent_messages)}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5, "QTEST-1234"))

    assert result == "answer to current prompt"
    assert not any("is no longer active" in payload["text"] for payload in sent_messages[:-1])
    assert sent_messages[-1]["text"] == "✅ Received [QTEST-1234]"


def test_tool_uses_telegram_only_mode_without_dialog(monkeypatch):
    """Skip the local dialog when the telegram-only mode is selected."""

    class StubTelegramClient:
        def __init__(self):
            self.question = None
            self.context = None
            self.timeout = None
            self.prompt_id = None
            self.include_timing_info = None
            self.issued_at = None

        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            self.question = question
            self.context = context
            self.timeout = timeout_seconds
            self.prompt_id = prompt_id
            self.include_timing_info = include_timing_info
            self.issued_at = issued_at
            return "telegram answer"

    class StubDialogHandler:
        platform = "Linux"

        async def get_user_input(self, *args, **kwargs):
            raise AssertionError("Dialog should not be used in telegram mode")

    stub_telegram = StubTelegramClient()

    monkeypatch.setattr(server, "telegram_client", stub_telegram)
    monkeypatch.setattr(server, "dialog_handler", StubDialogHandler())
    monkeypatch.setattr(server, "response_channel", "telegram")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(
        server.asking_user_missing_context("Where should I deploy?", "Need a quick answer.")
    )

    assert result == "✅ User response: telegram answer"
    assert stub_telegram.timeout == 300
    assert stub_telegram.question == "Where should I deploy?"
    assert stub_telegram.context == "Need a quick answer."
    assert stub_telegram.prompt_id is not None
    assert stub_telegram.prompt_id.startswith("Q")
    assert stub_telegram.include_timing_info is False
    assert isinstance(stub_telegram.issued_at, dt.datetime)


def test_both_mode_adds_windows_warning_and_threads_dialog(monkeypatch):
    """Warn on Windows and run the local dialog path in a worker thread."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            return "telegram answer"

    class StubDialogHandler:
        platform = "Windows"

        def __init__(self):
            self.calls = []

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            self.calls.append(
                {
                    "question": question,
                    "timeout": timeout,
                    "cancel_event": cancel_event,
                    "run_in_thread": run_in_thread,
                }
            )
            await asyncio.sleep(3600)
            return None

    stub_dialog = StubDialogHandler()

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", stub_dialog)
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.asking_user_missing_context("Q?", "Context text."))

    assert result == "✅ User response: telegram answer"
    assert stub_dialog.calls[0]["run_in_thread"] is True
    assert stub_dialog.calls[0]["cancel_event"] is None
    assert stub_dialog.calls[0]["question"] is not None
    assert "📨 Also sent to Telegram." in stub_dialog.calls[0]["question"]
    assert "will stay open" in stub_dialog.calls[0]["question"]


def test_both_mode_cancels_linux_dialog_when_telegram_wins(monkeypatch):
    """Signal subprocess-backed dialogs to close when Telegram wins the race."""

    class StubTelegramClient:
        async def ask_question(
            self,
            question,
            context,
            *,
            prompt_id,
            timeout_seconds,
            include_timing_info,
            issued_at,
        ):
            return "telegram answer"

    class StubDialogHandler:
        platform = "Linux"

        def __init__(self):
            self.question = None
            self.cancel_event = None

        async def get_user_input(
            self,
            question,
            timeout,
            *,
            cancel_event=None,
            run_in_thread=False,
        ):
            self.question = question
            self.cancel_event = cancel_event
            assert cancel_event is not None
            await cancel_event.wait()
            return None

    stub_dialog = StubDialogHandler()

    monkeypatch.setattr(server, "telegram_client", StubTelegramClient())
    monkeypatch.setattr(server, "dialog_handler", stub_dialog)
    monkeypatch.setattr(server, "response_channel", "both")
    monkeypatch.setattr(server, "show_timing_info", False)
    monkeypatch.setattr(server, "dialog_timeout_seconds", 300)

    result = asyncio.run(server.asking_user_missing_context("Q?", "Context text."))

    assert result == "✅ User response: telegram answer"
    assert stub_dialog.cancel_event is not None
    assert stub_dialog.question is not None
    assert stub_dialog.cancel_event.is_set() is True
    assert "📨 Also sent to Telegram." in stub_dialog.question
    assert "will stay open" not in stub_dialog.question
