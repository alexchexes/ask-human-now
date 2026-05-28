"""Telegram polling client for prompt delivery, reply parsing, and downloads."""

import asyncio
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional, cast

from .broker_state import TelegramBrokerIdentity
from .prompt_formatting import TELEGRAM_DOWNLOAD_LIMIT_LABEL
from .telegram_models import (
    DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
    TELEGRAM_DOWNLOAD_LIMIT_BYTES,
    TelegramConfig,
    TelegramPendingPrompt,
    TelegramPromptError,
    TelegramReplyRejection,
    TelegramReplyResolution,
)


class TelegramPromptClient:
    """Minimal long-polling Telegram client for prompt/response workflows."""

    ISSUE_URL = "https://github.com/alexchexes/ask-human-now/issues"
    NON_REPLY_HINT_TEXT = "⚠️ Message is ignored. Please use Reply on the bot's message."
    STALE_REPLY_HINT_TEMPLATE = (
        "⚠️ Message is ignored. {prompt_target} is no longer active. Ask the agent to send "
        "a new question."
    )

    def __init__(
        self,
        config: TelegramConfig,
        download_dir: Optional[Path] = None,
        *,
        broker_identity: Optional[TelegramBrokerIdentity] = None,
    ) -> None:
        self.bot_token = config.bot_token
        self.chat_id = config.chat_id
        self.download_dir = download_dir
        self.broker_identity = broker_identity
        self._lock = asyncio.Lock()
        self._next_update_offset: Optional[int] = None
        self._pending_by_message_id: dict[int, TelegramPendingPrompt] = {}
        self._poller_task: Optional[asyncio.Task[None]] = None
        self._latest_prompt_message_id: Optional[int] = None
        self._last_non_reply_hint_after_prompt_message_id: Optional[int] = None

    async def ask_question(
        self,
        prompt_text: str,
        timeout: int,
        prompt_id: str,
        download_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Send a prompt message and wait for a reply to that specific message."""
        message_id = await self._send_prompt(prompt_text)
        response_future = asyncio.get_running_loop().create_future()
        resolved_download_dir = download_dir or self.download_dir
        if resolved_download_dir is None:
            raise TelegramPromptError("No Telegram download directory was configured.")

        async with self._lock:
            self._pending_by_message_id[message_id] = TelegramPendingPrompt(
                future=response_future,
                prompt_id=prompt_id,
                download_dir=resolved_download_dir,
            )
            self._latest_prompt_message_id = message_id
            self._ensure_poller_locked()

        try:
            return await asyncio.wait_for(response_future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            async with self._lock:
                self._pending_by_message_id.pop(message_id, None)
                if not self._pending_by_message_id:
                    self._latest_prompt_message_id = None
                    self._last_non_reply_hint_after_prompt_message_id = None

    async def _send_prompt(self, prompt_text: str) -> int:
        """Send the outbound Telegram message and return its message id."""
        result = await self._bot_api_request(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": prompt_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramPromptError("Telegram sendMessage did not return a message_id.")

        return message_id

    def _ensure_poller_locked(self) -> None:
        """Start the update poller while the lock is held."""
        if self._poller_task is None or self._poller_task.done():
            self._poller_task = asyncio.create_task(self._poll_updates())
            self._poller_task.add_done_callback(self._consume_task_result)

    async def _poll_updates(self) -> None:
        """Long-poll Telegram updates and resolve pending prompt futures."""
        try:
            while True:
                async with self._lock:
                    has_pending_prompts = bool(self._pending_by_message_id)
                    offset = self._next_update_offset

                    if not has_pending_prompts and offset is None:
                        self._poller_task = None
                        return

                poll_timeout = (
                    0 if not has_pending_prompts else DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS
                )
                updates = await self._bot_api_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": poll_timeout,
                        "allowed_updates": ["message"],
                    },
                    timeout=poll_timeout + 10,
                )

                if not isinstance(updates, list):
                    raise TelegramPromptError("Telegram getUpdates returned an unexpected payload.")

                async with self._lock:
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._next_update_offset = update_id + 1

                for update in updates:
                    await self._handle_update(update)

                # Telegram only considers processed updates confirmed once we perform another
                # getUpdates call with an offset higher than their update_id. When the last
                # pending prompt completes, keep draining with timeout=0 until Telegram returns
                # an empty page, then stop the poller.
                if not has_pending_prompts and not updates:
                    async with self._lock:
                        self._poller_task = None
                    return
        except Exception as exc:
            async with self._lock:
                pending = [item.future for item in self._pending_by_message_id.values()]
                self._pending_by_message_id.clear()
                self._poller_task = None

            for future in pending:
                if not future.done():
                    future.set_exception(TelegramPromptError(f"Telegram polling failed: {exc}"))

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Process one Telegram update and resolve or reject matching replies."""
        await self._maybe_hint_on_missing_reply(update)
        matched = await self._match_pending_prompt(update)
        if matched is None:
            return

        prompt_message_id, pending_prompt, message = matched
        resolution = await self._build_reply_resolution(
            message,
            pending_prompt.prompt_id,
            download_dir=pending_prompt.download_dir,
        )
        user_message_id = message.get("message_id")
        reply_to_user_message_id = user_message_id if isinstance(user_message_id, int) else None

        if isinstance(resolution, TelegramReplyRejection):
            await self._safe_send_status_message(
                resolution.user_message,
                reply_to_message_id=reply_to_user_message_id,
            )
            return

        should_ack = False
        async with self._lock:
            current_pending = self._pending_by_message_id.get(prompt_message_id)
            if (
                current_pending is not None
                and current_pending is pending_prompt
                and not current_pending.future.done()
            ):
                current_pending.future.set_result(resolution.agent_response)
                self._pending_by_message_id.pop(prompt_message_id, None)
                if not self._pending_by_message_id:
                    self._latest_prompt_message_id = None
                    self._last_non_reply_hint_after_prompt_message_id = None
                should_ack = True

        if should_ack:
            await self._safe_send_status_message(
                f"✅ Received [{pending_prompt.prompt_id}]",
                reply_to_message_id=reply_to_user_message_id,
            )

    async def _match_pending_prompt(
        self, update: dict[str, Any]
    ) -> Optional[tuple[int, TelegramPendingPrompt, dict[str, Any]]]:
        """Find a pending prompt that this Telegram update replies to."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return None

        reply_to_message = message.get("reply_to_message")
        if not isinstance(reply_to_message, dict):
            return None

        reply_message_id = reply_to_message.get("message_id")
        if not isinstance(reply_message_id, int):
            return None

        async with self._lock:
            pending_prompt = self._pending_by_message_id.get(reply_message_id)

        if pending_prompt is None:
            if await self._warn_on_stale_local_reply(message, reply_to_message):
                return None
            await self._warn_on_foreign_broker_reply(message, reply_to_message)
            return None

        return reply_message_id, pending_prompt, message

    async def _maybe_hint_on_missing_reply(self, update: dict[str, Any]) -> None:
        """Warn once after the latest local prompt if the user sends a non-reply message."""
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != self.chat_id:
            return

        if message.get("from", {}).get("is_bot"):
            return

        if isinstance(message.get("reply_to_message"), dict):
            return

        if not self._is_user_reply_candidate(message):
            return

        async with self._lock:
            if not self._pending_by_message_id or self._latest_prompt_message_id is None:
                return

            latest_prompt_message_id = self._latest_prompt_message_id
            if self._last_non_reply_hint_after_prompt_message_id == latest_prompt_message_id:
                return

        user_message_id = self._extract_message_id(message)
        hinted = await self._safe_send_status_message(
            self.NON_REPLY_HINT_TEXT,
            reply_to_message_id=user_message_id if user_message_id else None,
        )
        if not hinted:
            return

        async with self._lock:
            if self._latest_prompt_message_id == latest_prompt_message_id:
                self._last_non_reply_hint_after_prompt_message_id = latest_prompt_message_id

    async def _build_reply_resolution(
        self,
        message: dict[str, Any],
        prompt_id: str,
        *,
        download_dir: Path,
    ) -> TelegramReplyResolution | TelegramReplyRejection:
        """Turn one matched Telegram reply into an agent-facing response or a retry prompt."""
        if isinstance(message.get("media_group_id"), str):
            return TelegramReplyRejection(
                f"⚠️ Unsupported reply for [{prompt_id}]. Albums/media groups are not supported yet. "
                "Please reply again with a single text, file, media, location, venue, or contact message."
            )

        response_text = message.get("text")
        if isinstance(response_text, str) and response_text.strip():
            return TelegramReplyResolution(response_text.strip())

        caption = self._clean_optional_text(message.get("caption"))
        reply_message_id = self._extract_message_id(message)

        if isinstance(message.get("document"), dict):
            return await self._build_file_reply(
                "document",
                cast(dict[str, Any], message["document"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["document"].get("file_name")),
            )

        if isinstance(message.get("video"), dict):
            return await self._build_file_reply(
                "video",
                cast(dict[str, Any], message["video"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["video"].get("file_name")),
            )

        if isinstance(message.get("audio"), dict):
            return await self._build_file_reply(
                "audio",
                cast(dict[str, Any], message["audio"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["audio"].get("file_name")),
            )

        if isinstance(message.get("voice"), dict):
            return await self._build_file_reply(
                "voice",
                cast(dict[str, Any], message["voice"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
            )

        if isinstance(message.get("animation"), dict):
            return await self._build_file_reply(
                "animation",
                cast(dict[str, Any], message["animation"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
                original_file_name=self._clean_optional_text(message["animation"].get("file_name")),
            )

        if isinstance(message.get("video_note"), dict):
            return await self._build_file_reply(
                "video_note",
                cast(dict[str, Any], message["video_note"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                maybe_unintended=True,
            )

        if isinstance(message.get("sticker"), dict):
            return await self._build_file_reply(
                "sticker",
                cast(dict[str, Any], message["sticker"]),
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                maybe_unintended=True,
            )

        photo = self._choose_photo_variant(message.get("photo"))
        if photo is not None:
            return await self._build_file_reply(
                "photo",
                photo,
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                caption=caption,
            )

        if isinstance(message.get("location"), dict) and isinstance(message.get("venue"), dict):
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "venue",
                    [
                        f"Title: {cast(dict[str, Any], message['venue']).get('title', '')}",
                        f"Address: {cast(dict[str, Any], message['venue']).get('address', '')}",
                        f"Latitude: {cast(dict[str, Any], message['location']).get('latitude', '')}",
                        f"Longitude: {cast(dict[str, Any], message['location']).get('longitude', '')}",
                    ],
                )
            )

        if isinstance(message.get("location"), dict):
            location = cast(dict[str, Any], message["location"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "location",
                    [
                        f"Latitude: {location.get('latitude', '')}",
                        f"Longitude: {location.get('longitude', '')}",
                    ],
                )
            )

        if isinstance(message.get("contact"), dict):
            contact = cast(dict[str, Any], message["contact"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "contact",
                    [
                        f"Phone number: {contact.get('phone_number', '')}",
                        f"First name: {contact.get('first_name', '')}",
                        f"Last name: {contact.get('last_name', '')}",
                        f"User ID: {contact.get('user_id', '')}",
                        f"VCard: {contact.get('vcard', '')}",
                    ],
                )
            )

        if isinstance(message.get("poll"), dict):
            poll = cast(dict[str, Any], message["poll"])
            options = poll.get("options")
            option_labels: list[str] = []
            if isinstance(options, list):
                for option in options:
                    if isinstance(option, dict):
                        option_labels.append(
                            f"- {option.get('text', '')} ({option.get('voter_count', '')} votes)"
                        )
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "poll",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Question: {poll.get('question', '')}",
                        f"Type: {poll.get('type', '')}",
                        *option_labels,
                    ],
                )
            )

        if isinstance(message.get("dice"), dict):
            dice = cast(dict[str, Any], message["dice"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "dice",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Emoji: {dice.get('emoji', '')}",
                        f"Value: {dice.get('value', '')}",
                    ],
                )
            )

        if isinstance(message.get("game"), dict):
            game = cast(dict[str, Any], message["game"])
            return TelegramReplyResolution(
                self._format_structured_reply(
                    "game",
                    [
                        "Note: this may be unintended unless explicitly expected.",
                        f"Title: {game.get('title', '')}",
                        f"Description: {game.get('description', '')}",
                    ],
                )
            )

        return TelegramReplyRejection(
            f"⚠️ Unsupported reply for [{prompt_id}]. Please reply with text, a supported "
            "single file/media message, location, venue, or contact."
        )

    async def _build_file_reply(
        self,
        reply_type: str,
        file_payload: dict[str, Any],
        prompt_id: str,
        *,
        download_dir: Path,
        reply_message_id: int,
        caption: Optional[str] = None,
        original_file_name: Optional[str] = None,
        maybe_unintended: bool = False,
    ) -> TelegramReplyResolution | TelegramReplyRejection:
        """Download a Telegram file payload and build the agent-facing reply text."""
        file_id = file_payload.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            return TelegramReplyRejection(
                f"⚠️ Unsupported reply for [{prompt_id}]. Telegram did not provide a downloadable "
                f"{reply_type} file ID. Please reply again with text or another supported message type."
            )

        file_size = file_payload.get("file_size")
        if isinstance(file_size, int) and file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            return TelegramReplyRejection(
                f"⚠️ File too large for [{prompt_id}]. The default Telegram Bot API supports files "
                f"up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}. Please send a smaller file or a text reply."
            )

        try:
            saved_path = await self._download_telegram_file(
                file_id,
                reply_type,
                prompt_id,
                download_dir=download_dir,
                reply_message_id=reply_message_id,
                original_file_name=original_file_name,
                declared_file_size=file_size if isinstance(file_size, int) else None,
            )
        except ValueError:
            return TelegramReplyRejection(
                f"⚠️ File too large for [{prompt_id}]. The default Telegram Bot API supports files "
                f"up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}. Please send a smaller file or a text reply."
            )
        except TelegramPromptError as exc:
            return TelegramReplyRejection(
                f"⚠️ Could not consume your reply for [{prompt_id}]. {exc}. Please reply again with "
                "text or another supported message type."
            )

        lines: list[str] = []
        if maybe_unintended:
            lines.append("Note: this may be unintended unless explicitly expected.")
        if caption:
            lines.append(f"Caption: {caption}")
        lines.append(f"Saved file: {saved_path}")
        if original_file_name:
            lines.append(f"Original file name: {original_file_name}")

        return TelegramReplyResolution(self._format_structured_reply(reply_type, lines))

    async def _download_telegram_file(
        self,
        file_id: str,
        reply_type: str,
        prompt_id: str,
        *,
        download_dir: Path,
        reply_message_id: int,
        original_file_name: Optional[str] = None,
        declared_file_size: Optional[int] = None,
    ) -> str:
        """Download a Telegram file and return its saved local path."""
        if declared_file_size is not None and declared_file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            raise ValueError("Telegram file exceeds the supported download limit.")

        file_result = await self._bot_api_request("getFile", {"file_id": file_id}, timeout=20)
        if not isinstance(file_result, dict):
            raise TelegramPromptError("Telegram getFile returned an unexpected payload.")

        file_path = file_result.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise TelegramPromptError("Telegram getFile did not return a usable file path.")

        file_size = file_result.get("file_size")
        if isinstance(file_size, int) and file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
            raise ValueError("Telegram file exceeds the supported download limit.")

        target_dir = download_dir / prompt_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = self._build_download_file_name(
            reply_type,
            prompt_id,
            reply_message_id,
            original_file_name=original_file_name,
            telegram_file_path=file_path,
        )
        target_path = target_dir / target_name

        await asyncio.to_thread(self._download_telegram_file_sync, file_path, target_path)
        return str(target_path.resolve())

    def _download_telegram_file_sync(self, telegram_file_path: str, target_path: Path) -> None:
        """Download one Telegram file through the standard file endpoint."""
        file_url = (
            f"https://api.telegram.org/file/bot{self.bot_token}/"
            f"{urllib.parse.quote(telegram_file_path, safe='/')}"
        )

        try:
            with urllib.request.urlopen(file_url, timeout=60) as response:
                with target_path.open("wb") as output_file:
                    shutil.copyfileobj(response, output_file)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramPromptError(
                f"Telegram file download failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except OSError as exc:
            raise TelegramPromptError(f"Telegram file download failed: {exc}") from exc

    def _build_download_file_name(
        self,
        reply_type: str,
        prompt_id: str,
        reply_message_id: int,
        *,
        original_file_name: Optional[str],
        telegram_file_path: str,
    ) -> str:
        """Create a safe local filename for one downloaded Telegram artifact."""
        preferred_name = self._clean_optional_text(original_file_name)
        if preferred_name:
            base_name = Path(preferred_name).name
        else:
            base_name = Path(telegram_file_path).name

        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", base_name).strip(" .")
        if not safe_name:
            suffix = Path(telegram_file_path).suffix
            safe_name = f"{reply_type}-{prompt_id}-{reply_message_id}{suffix}"

        return safe_name

    @staticmethod
    def _choose_photo_variant(photo_payload: Any) -> Optional[dict[str, Any]]:
        """Pick the largest Telegram photo variant from the Message.photo list."""
        if not isinstance(photo_payload, list):
            return None

        candidates = [item for item in photo_payload if isinstance(item, dict)]
        if not candidates:
            return None

        def _photo_rank(photo: dict[str, Any]) -> tuple[int, int]:
            file_size = photo.get("file_size")
            width = photo.get("width")
            height = photo.get("height")
            return (
                file_size if isinstance(file_size, int) else -1,
                (width * height) if isinstance(width, int) and isinstance(height, int) else -1,
            )

        return max(candidates, key=_photo_rank)

    @staticmethod
    def _clean_optional_text(value: Any) -> Optional[str]:
        """Normalize an optional Telegram text/caption value."""
        if not isinstance(value, str):
            return None

        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _extract_message_id(message: dict[str, Any]) -> int:
        """Read the Telegram message id or return 0 if missing."""
        message_id = message.get("message_id")
        return message_id if isinstance(message_id, int) else 0

    @staticmethod
    def _format_structured_reply(reply_type: str, lines: list[str]) -> str:
        """Format a non-text Telegram reply for the agent as plain text."""
        cleaned_lines = [line for line in lines if line]
        if not cleaned_lines:
            return f"[telegram {reply_type} reply]"

        return f"[telegram {reply_type} reply]\n" + "\n".join(cleaned_lines)

    @staticmethod
    def _is_user_reply_candidate(message: dict[str, Any]) -> bool:
        """Decide whether a non-reply Telegram message looks like an attempted answer."""
        candidate_keys = (
            "text",
            "document",
            "video",
            "audio",
            "voice",
            "animation",
            "video_note",
            "sticker",
            "photo",
            "location",
            "venue",
            "contact",
            "poll",
            "dice",
            "game",
            "media_group_id",
        )
        return any(key in message for key in candidate_keys)

    async def _safe_send_status_message(
        self, text: str, *, reply_to_message_id: Optional[int] = None
    ) -> bool:
        """Best-effort Telegram status/ack message that should not break reply resolution."""
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        try:
            await self._bot_api_request("sendMessage", payload, timeout=20)
        except TelegramPromptError:
            return False
        return True

    async def _warn_on_foreign_broker_reply(
        self,
        message: dict[str, Any],
        reply_to_message: dict[str, Any],
    ) -> None:
        """Warn if this broker consumed a reply that appears intended for another broker."""
        if self.broker_identity is None:
            return

        replied_text = reply_to_message.get("text")
        if not isinstance(replied_text, str):
            return

        broker_reference = self._parse_broker_reference(replied_text)
        if broker_reference is None:
            return

        foreign_label, foreign_id = broker_reference
        if foreign_id == self.broker_identity.broker_id:
            return

        reply_message_id = self._extract_message_id(message)
        warning_text = (
            f"⚠️ Instance [{self.broker_identity.broker_label} [{self.broker_identity.broker_id}]] "
            f"just consumed your reply, but it appears intended for instance "
            f"[{foreign_label} [{foreign_id}]]. If you use the same bot from multiple machines "
            f"or apps at the same time, avoid doing that. Otherwise, please open an issue: "
            f"{self.ISSUE_URL}"
        )
        await self._safe_send_status_message(
            warning_text,
            reply_to_message_id=reply_message_id if reply_message_id else None,
        )

    async def _warn_on_stale_local_reply(
        self,
        message: dict[str, Any],
        reply_to_message: dict[str, Any],
    ) -> bool:
        """Warn when a reply targets one of this broker's own no-longer-active prompts."""
        if self.broker_identity is None:
            return False

        replied_text = reply_to_message.get("text")
        if not isinstance(replied_text, str):
            # Telegram's nested reply payload does not guarantee `text` at the schema level.
            # We intentionally stay silent here rather than store prompt history just to recover
            # stale-reply warnings for that edge case.
            return False

        broker_reference = self._parse_broker_reference(replied_text)
        if broker_reference is None or broker_reference[1] != self.broker_identity.broker_id:
            return False

        stale_prompt_id = self._parse_prompt_id_reference(replied_text)

        if stale_prompt_id is None:
            prompt_target = "That question"
        else:
            prompt_target = f"Prompt [{stale_prompt_id}]"

        warning_text = self.STALE_REPLY_HINT_TEMPLATE.format(prompt_target=prompt_target)
        user_message_id = self._extract_message_id(message)
        await self._safe_send_status_message(
            warning_text,
            reply_to_message_id=user_message_id if user_message_id else None,
        )
        return True

    @staticmethod
    def _parse_broker_reference(prompt_text: str) -> Optional[tuple[str, str]]:
        """Extract broker label/id metadata from one broker-formatted prompt."""
        match = re.search(r"Broker:\s*(.+?)\s*\[([a-f0-9]+)\]", prompt_text)
        if not match:
            return None

        return match.group(1).strip(), match.group(2).strip()

    @staticmethod
    def _parse_prompt_id_reference(prompt_text: str) -> Optional[str]:
        """Extract one prompt id from broker-formatted prompt text."""
        match = re.search(r"Prompt ID:\s*(\S+)", prompt_text)
        if not match:
            return None

        prompt_id = match.group(1).strip()
        return prompt_id or None

    async def _bot_api_request(self, method: str, payload: dict[str, Any], *, timeout: int) -> Any:
        """Issue a Telegram Bot API request through the standard library HTTP stack."""
        return await asyncio.to_thread(self._bot_api_request_sync, method, payload, timeout)

    def _bot_api_request_sync(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        """Perform a blocking Telegram Bot API request."""
        request_url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        request_data = json.dumps(payload).encode("utf-8")
        request_obj = urllib.request.Request(
            request_url,
            data=request_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=timeout) as response:
                payload_json = json.load(response)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramPromptError(
                f"Telegram {method} failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except OSError as exc:
            raise TelegramPromptError(f"Telegram {method} request failed: {exc}") from exc

        if not isinstance(payload_json, dict) or not payload_json.get("ok"):
            raise TelegramPromptError(
                f"Telegram {method} failed: {payload_json.get('description', 'unknown error')}"
            )

        return payload_json.get("result")

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        """Avoid noisy unhandled task warnings for background pollers."""
        with suppress(asyncio.CancelledError):
            task.result()
