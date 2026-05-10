"""Tests for Telegram response-channel support."""

import asyncio

import pytest

from ask_human_for_context_mcp import server


def test_parse_telegram_target_parses_token_and_chat_id():
    """Parse the single CLI argument into token and chat id."""
    config = server.parse_telegram_target("123456:ABCDEF -1009876543210")

    assert config == server.TelegramConfig(
        bot_token="123456:ABCDEF",
        chat_id="-1009876543210",
    )


def test_parse_telegram_target_rejects_invalid_shape():
    """Reject telegram target values without both pieces."""
    with pytest.raises(ValueError):
        server.parse_telegram_target("123456:ABCDEF")


def test_build_dialog_telegram_notice_is_platform_specific():
    """Use the Windows stale-dialog warning only on Windows."""
    assert server.build_dialog_telegram_notice("Linux") == "📨 Also sent to Telegram."
    assert "will stay open" in server.build_dialog_telegram_notice("Windows")


def test_build_telegram_prompt_text_adds_reply_instruction():
    """Tell the user to reply to the Telegram message."""
    prompt_text = server.build_telegram_prompt_text("Prompt text")

    assert prompt_text.endswith("↩️ Reply to this message with your answer.")


def test_telegram_client_resolves_reply_to_sent_message(monkeypatch):
    """Resolve a Telegram reply that references the sent prompt message."""
    client = server.TelegramPromptClient(server.TelegramConfig("123456:ABCDEF", "-1009876543210"))
    updates = [
        [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": -1009876543210},
                    "reply_to_message": {"message_id": 101},
                    "text": "telegram answer",
                },
            }
        ],
        [],
    ]

    async def fake_bot_api_request(method, payload, timeout):
        if method == "sendMessage":
            return {"message_id": 101}
        if method == "getUpdates":
            return updates.pop(0)
        raise AssertionError(f"Unexpected method: {method}")

    monkeypatch.setattr(client, "_bot_api_request", fake_bot_api_request)

    result = asyncio.run(client.ask_question("Prompt text", 5))

    assert result == "telegram answer"


def test_tool_uses_telegram_only_mode_without_dialog(monkeypatch):
    """Skip the local dialog when the telegram-only mode is selected."""

    class StubTelegramClient:
        def __init__(self):
            self.prompt = None
            self.timeout = None

        async def ask_question(self, prompt_text, timeout):
            self.prompt = prompt_text
            self.timeout = timeout
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
    assert "📋 Context:" in stub_telegram.prompt
    assert "❓ Question:" in stub_telegram.prompt


def test_both_mode_adds_windows_warning_and_threads_dialog(monkeypatch):
    """Warn on Windows and run the local dialog path in a worker thread."""

    class StubTelegramClient:
        async def ask_question(self, prompt_text, timeout):
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
    assert "📨 Also sent to Telegram." in stub_dialog.calls[0]["question"]
    assert "will stay open" in stub_dialog.calls[0]["question"]


def test_both_mode_cancels_linux_dialog_when_telegram_wins(monkeypatch):
    """Signal subprocess-backed dialogs to close when Telegram wins the race."""

    class StubTelegramClient:
        async def ask_question(self, prompt_text, timeout):
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
    assert stub_dialog.cancel_event.is_set() is True
    assert "📨 Also sent to Telegram." in stub_dialog.question
    assert "will stay open" not in stub_dialog.question
