import asyncio
import datetime as dt
import json
import locale
import platform
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Optional, cast

import uvicorn
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

# Initialize FastMCP server for User Prompt tools
mcp = FastMCP("ask-human-for-context")

DEFAULT_DIALOG_TIMEOUT_SECONDS = 120
DEFAULT_DIALOG_TITLE = "🤖 Cursor AI Assistant"
DEFAULT_RESPONSE_CHANNEL = "dialog"
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 25
TIMING_INFO_TIMEOUT_NOTE = "client may time out sooner"


def resolve_dialog_title(dialog_title: Optional[str] = None) -> str:
    """Resolve the dialog title from CLI input or default."""
    if dialog_title and dialog_title.strip():
        return dialog_title.strip()

    return DEFAULT_DIALOG_TITLE


def format_dialog_timestamp(moment: dt.datetime) -> str:
    """Format dialog timestamps using the current locale's short date/time format."""
    try:
        formatted = moment.astimezone().strftime("%x %X").strip()
        if formatted:
            return formatted
    except Exception:
        pass

    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def initialize_time_locale() -> None:
    """Initialize process-wide time formatting from the OS locale once at startup."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except Exception:
        pass


@dataclass(frozen=True)
class TelegramConfig:
    """Bot token and target chat for Telegram-based prompting."""

    bot_token: str
    chat_id: str


def parse_telegram_target(telegram_target: Optional[str]) -> Optional[TelegramConfig]:
    """Parse '<bot_token> <chat_id>' from a single CLI argument."""
    if telegram_target is None:
        return None

    target = telegram_target.strip()
    if not target:
        return None

    parts = target.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            "Telegram target must look like '<bot_token> <chat_id>' in a single argument."
        )

    return TelegramConfig(bot_token=parts[0].strip(), chat_id=parts[1].strip())


def build_timing_info_block(issued_at: dt.datetime, timeout_seconds: int) -> str:
    """Build the optional timing metadata shown in dialogs."""
    answer_until = issued_at + dt.timedelta(seconds=timeout_seconds)
    return (
        f"Issued at: {format_dialog_timestamp(issued_at)}"
        f" | Answer until: {format_dialog_timestamp(answer_until)}"
        f" ({TIMING_INFO_TIMEOUT_NOTE})"
    )


def build_dialog_telegram_notice(platform_name: str) -> str:
    """Explain that the prompt was also delivered through Telegram."""
    if platform_name == "Windows":
        return (
            "📨 Also sent to Telegram. ⚠️ If you reply there first, this dialog will stay "
            "open. Any later answer here will be ignored."
        )

    return "📨 Also sent to Telegram."


def build_prompt_text(
    question: str,
    context: str,
    *,
    timeout_seconds: int,
    include_timing_info: bool,
    extra_note: str = "",
) -> str:
    """Build the formatted prompt text for dialogs and Telegram messages."""
    separator = "─" * 40
    question_block = f"❓ Question:\n{question.strip()}"
    if extra_note.strip():
        question_block = f"{question_block}\n\n{extra_note.strip()}"

    if context.strip():
        full_question = f"📋 Context:\n{context.strip()}\n\n{separator}\n\n{question_block}"
    else:
        full_question = question_block

    if include_timing_info:
        return (
            f"{full_question}\n\n{separator}\n\n"
            f"{build_timing_info_block(dt.datetime.now().astimezone(), timeout_seconds)}"
        )

    return full_question


def build_telegram_prompt_text(prompt_text: str) -> str:
    """Add a minimal Telegram-specific reply instruction to the prompt."""
    return f"{prompt_text}\n\n↩️ Reply to this message with your answer."


# Custom exception classes for better error handling (Task 1.4)
class UserPromptTimeout(Exception):
    """Raised when user doesn't respond within timeout period."""

    pass


class UserPromptCancelled(Exception):
    """Raised when user cancels the prompt or interrupts the process."""

    pass


class UserPromptError(Exception):
    """Generic error for user prompt operations."""

    pass


class TelegramPromptError(Exception):
    """Error while sending a prompt or waiting for a Telegram reply."""

    pass


class TelegramPromptClient:
    """Minimal long-polling Telegram client for prompt/response workflows."""

    def __init__(self, config: TelegramConfig) -> None:
        self.bot_token = config.bot_token
        self.chat_id = config.chat_id
        self._lock = asyncio.Lock()
        self._next_update_offset: Optional[int] = None
        self._pending_by_message_id: dict[int, asyncio.Future[str]] = {}
        self._poller_task: Optional[asyncio.Task[None]] = None

    async def ask_question(self, prompt_text: str, timeout: int) -> Optional[str]:
        """Send a prompt message and wait for a reply to that specific message."""
        message_id = await self._send_prompt(prompt_text)
        response_future = asyncio.get_running_loop().create_future()

        async with self._lock:
            self._pending_by_message_id[message_id] = response_future
            self._ensure_poller_locked()

        try:
            return await asyncio.wait_for(response_future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            async with self._lock:
                self._pending_by_message_id.pop(message_id, None)

    async def _send_prompt(self, prompt_text: str) -> int:
        """Send the outbound Telegram message and return its message id."""
        result = await self._bot_api_request(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": build_telegram_prompt_text(prompt_text),
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
                    if not self._pending_by_message_id:
                        self._poller_task = None
                        return

                    offset = self._next_update_offset

                updates = await self._bot_api_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
                        "allowed_updates": ["message"],
                    },
                    timeout=DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS + 10,
                )

                if not isinstance(updates, list):
                    raise TelegramPromptError("Telegram getUpdates returned an unexpected payload.")

                async with self._lock:
                    for update in updates:
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            self._next_update_offset = update_id + 1

                        resolved = self._resolve_update_locked(update)
                        if resolved is not None:
                            message_id, response_text = resolved
                            future = self._pending_by_message_id.get(message_id)
                            if future is not None and not future.done():
                                future.set_result(response_text)
        except Exception as exc:
            async with self._lock:
                pending = list(self._pending_by_message_id.values())
                self._pending_by_message_id.clear()
                self._poller_task = None

            for future in pending:
                if not future.done():
                    future.set_exception(TelegramPromptError(f"Telegram polling failed: {exc}"))

    def _resolve_update_locked(self, update: dict[str, Any]) -> Optional[tuple[int, str]]:
        """Extract a reply to a pending prompt message from a Telegram update."""
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

        if reply_message_id not in self._pending_by_message_id:
            return None

        response_text = message.get("text")
        if not isinstance(response_text, str) or not response_text.strip():
            return None

        return reply_message_id, response_text.strip()

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


class GUIDialogHandler:
    """Cross-platform GUI dialog handler for asking humans for context.

    Provides native GUI dialogs on macOS (osascript), Linux (zenity), and Windows (tkinter).
    Falls back to terminal input if GUI is unavailable.
    """

    def __init__(self, dialog_title: Optional[str] = None) -> None:
        """Initialize the dialog handler with platform detection."""
        self.platform = platform.system()
        self.dialog_title = resolve_dialog_title(dialog_title)

    async def get_user_input(
        self,
        question: str,
        timeout: int = DEFAULT_DIALOG_TIMEOUT_SECONDS,
        *,
        cancel_event: Optional[asyncio.Event] = None,
        run_in_thread: bool = False,
    ) -> Optional[str]:
        """Get user input via native GUI dialog with timeout.

        Args:
            question: The question to ask the user
            timeout: Timeout in seconds

        Returns:
            The user's response as a string, or None if timeout/cancelled

        Raises:
            UserPromptError: If GUI dialog system fails
            UserPromptCancelled: If user cancels or interrupts
        """
        try:
            if self.platform == "Darwin":
                return await self._macos_dialog(question, timeout, cancel_event=cancel_event)
            elif self.platform == "Linux":
                return await self._linux_dialog(question, timeout, cancel_event=cancel_event)
            else:
                return await self._windows_dialog(
                    question,
                    timeout,
                    run_in_thread=run_in_thread,
                )
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            raise UserPromptCancelled("User interrupted the dialog with Ctrl+C")
        except Exception as e:
            # Don't fall back to terminal in MCP context - just report the error
            raise UserPromptError(
                f"GUI dialog failed: {e}. Ensure osascript (macOS), zenity (Linux), or tkinter (Windows) is available."
            )

    async def _communicate_or_cancel(
        self,
        process: asyncio.subprocess.Process,
        cancel_event: Optional[asyncio.Event],
    ) -> tuple[bytes, bytes, bool]:
        """Wait for a dialog subprocess or cancel it if another channel wins."""
        communicate_task = asyncio.create_task(process.communicate())
        cancel_task: Optional[asyncio.Task[bool]] = None
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())

        tasks = {communicate_task}
        if cancel_task is not None:
            tasks.add(cancel_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for pending_task in pending:
            pending_task.cancel()
            with suppress(asyncio.CancelledError):
                await pending_task

        if cancel_task is not None and cancel_task in done and cancel_event is not None:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

            communicate_task.cancel()
            with suppress(asyncio.CancelledError):
                await communicate_task
            return b"", b"", True

        stdout, stderr = await communicate_task
        return stdout, stderr, False

    def _enable_windows_dpi_awareness(self) -> None:
        """Enable crisp rendering for Windows dialogs on scaled displays."""
        try:
            import ctypes

            try:
                # Prefer per-monitor awareness on modern Windows versions.
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
                return
            except Exception:
                pass

            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        except Exception:
            pass

    def _configure_windows_tk_scaling(self, root: Any) -> None:
        """Match Tk scaling to the current monitor DPI when available."""
        try:
            import ctypes

            dpi = 0
            try:
                dpi = ctypes.windll.user32.GetDpiForWindow(root.winfo_id())
            except Exception:
                try:
                    dpi = root.winfo_fpixels("1i")
                except Exception:
                    dpi = 0

            if dpi:
                root.tk.call("tk", "scaling", float(dpi) / 72.0)
        except Exception:
            pass

    async def _macos_dialog(
        self, question: str, timeout: int, *, cancel_event: Optional[asyncio.Event] = None
    ) -> Optional[str]:
        """macOS dialog using osascript with custom Cursor icon."""

        # Use the custom Cursor icon from assets folder
        import os

        # Use absolute path to the icon file - more reliable than path calculation
        cursor_icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "assets",
            "cursor-icon.icns",
        )

        if os.path.exists(cursor_icon_path):
            icon_clause = f'with icon file (POSIX file "{cursor_icon_path}")'
        else:
            # Fallback to caution icon if custom icon not found
            icon_clause = "with icon caution"

        script = f"""
        display dialog "{self._escape_for_applescript(question)}" ¬
        default answer "" ¬
        with title "{self._escape_for_applescript(self.dialog_title)}" ¬
        {icon_clause} ¬
        giving up after {timeout}
        """

        try:
            process = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr, was_cancelled = await self._communicate_or_cancel(process, cancel_event)
            if was_cancelled:
                return None

            if process.returncode == 0:
                output = stdout.decode().strip()
                # Handle AppleScript output format: "button returned:OK, text returned:user_input"
                if "text returned:" in output:
                    # Extract text after "text returned:" and before any comma or end
                    text_part = output.split("text returned:")[1]
                    # Remove trailing ", gave up:false" or similar
                    if ", " in text_part:
                        return text_part.split(", ")[0].strip()
                    return text_part.strip()
                elif "gave up:true" in output:
                    # User didn't respond within timeout
                    return None
                elif "button returned:" in output and "text returned:" not in output:
                    # User clicked OK but didn't enter text
                    return ""
            return None
        except Exception as e:
            return None

    async def _linux_dialog(
        self, question: str, timeout: int, *, cancel_event: Optional[asyncio.Event] = None
    ) -> Optional[str]:
        """Linux dialog using zenity with custom Cursor logo."""
        # Use the custom Cursor logo for consistent branding
        icon_args = self._get_linux_icon_args()

        cmd = [
            "zenity",
            "--entry",
            f"--title={self.dialog_title}",
            f"--text={question}",
            f"--timeout={timeout}",
        ] + icon_args

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr, was_cancelled = await self._communicate_or_cancel(process, cancel_event)
            if was_cancelled:
                return None

            if process.returncode == 0:
                return stdout.decode().strip()
            return None
        except Exception:
            return None

    async def _windows_dialog(
        self,
        question: str,
        timeout: int,
        *,
        run_in_thread: bool = False,
    ) -> Optional[str]:
        """Windows dialog using tkinter with custom Cursor logo."""
        if run_in_thread:
            return await asyncio.to_thread(self._windows_dialog_sync, question, timeout)

        return self._windows_dialog_sync(question, timeout)

    def _windows_dialog_sync(self, question: str, timeout: int) -> Optional[str]:
        """Blocking Windows dialog implementation for the current Tk/simpledialog UI."""
        root = None
        try:
            import tkinter as tk
            from tkinter import simpledialog

            self._enable_windows_dpi_awareness()
            # Create a simple dialog using tkinter
            root = tk.Tk()
            self._configure_windows_tk_scaling(root)
            root.withdraw()  # Hide the main window

            # Try to set custom icon from PNG (converted to ICO)
            self._set_windows_icon(root)

            return self._ask_windows_string(
                root,
                simpledialog,
                self.dialog_title,
                question,
                timeout,
            )
        except Exception:
            return None
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    def _ask_windows_string(
        self,
        root: Any,
        simpledialog: Any,
        title: str,
        question: str,
        timeout: int,
    ) -> Optional[str]:
        """Ask for a Windows string response and close it when timeout expires."""
        timeout_id = root.after(timeout * 1000, root.destroy)
        try:
            return cast(Optional[str], simpledialog.askstring(title, question, parent=root))
        finally:
            try:
                root.after_cancel(timeout_id)
            except Exception:
                pass

    def _escape_for_applescript(self, text: str) -> str:
        """Escape text for AppleScript."""
        return text.replace('"', '\\"').replace("\\", "\\\\")

    def _get_macos_icon_clause(self) -> str:
        """Get the icon clause for macOS dialog with custom Cursor logo."""
        import os

        # Check for the specific Cursor Logo files (prioritize ICNS for macOS)
        custom_logo_paths = [
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "assets",
                "cursor-icon.icns",
            ),  # Try ICNS first (native macOS format)
            "./cursor-icon.icns",
            "./assets/Cursor Logo (4).png",
            "./Cursor Logo (4).png",
        ]

        for icon_path in custom_logo_paths:
            if os.path.exists(icon_path):
                if icon_path.endswith(".icns"):
                    print(f"✅ Found ICNS icon: {icon_path}")
                    abs_path = os.path.abspath(icon_path)
                    return f'with icon file (POSIX file "{abs_path}")'
                elif icon_path.endswith(".png"):
                    print(f"✅ Found custom Cursor logo: {icon_path}")
                    print("ℹ️ Note: Using application icon style for PNG logo")
                    # Use 'application' icon for software/AI assistant feel
                    return "with icon application"

        # Fall back to application icon (better for AI assistant than default)
        return "with icon application"

    def _get_linux_icon_args(self) -> list:
        """Get icon arguments for Linux zenity dialog with custom Cursor logo."""
        import os

        # Check for the specific Cursor Logo (4).png file first
        custom_logo_paths = [
            "./assets/Cursor Logo (4).png",
            "./Cursor Logo (4).png",
            "./assets/cursor-icon.png",
            "./cursor-icon.png",
        ]

        for icon_path in custom_logo_paths:
            if os.path.exists(icon_path):
                print(f"✅ Using custom Cursor logo for Linux: {icon_path}")
                return [f"--window-icon={icon_path}"]

        # Fall back to built-in question icon
        return ["--question"]

    def _set_windows_icon(self, root: Any) -> None:
        """Set icon for Windows tkinter dialog with custom Cursor logo."""
        import os

        # Check for custom Cursor icon files
        possible_icon_paths = [
            "./assets/cursor-icon.ico",
            "./cursor-icon.ico",
            "C:\\Program Files\\Cursor\\cursor.ico",
        ]

        for icon_path in possible_icon_paths:
            if os.path.exists(icon_path):
                try:
                    print(f"✅ Using custom Cursor icon for Windows: {icon_path}")
                    root.iconbitmap(icon_path)
                    return
                except Exception:
                    continue

        # Note: PNG files can't be directly used as Windows icons
        # Users would need to convert "Cursor Logo (4).png" to ICO format
        if os.path.exists("./assets/Cursor Logo (4).png"):
            print("ℹ️ Found PNG logo. For Windows, convert to ICO format for icon support.")


# Global dialog handler instance
dialog_handler = GUIDialogHandler()
dialog_timeout_seconds = DEFAULT_DIALOG_TIMEOUT_SECONDS
show_timing_info = False
response_channel = DEFAULT_RESPONSE_CHANNEL
telegram_client: Optional[TelegramPromptClient] = None


def _consume_detached_task_result(task: asyncio.Task[Any]) -> None:
    """Suppress result warnings for intentionally detached background tasks."""
    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def _get_first_channel_response(
    dialog_prompt: str,
    telegram_prompt: str,
    timeout_seconds: int,
) -> Optional[str]:
    """Race dialog and Telegram, returning the first successful response."""
    if telegram_client is None:
        raise UserPromptError(
            "Telegram response channel is enabled, but no Telegram target was configured."
        )

    should_thread_windows_dialog = dialog_handler.platform == "Windows"
    cancel_event = None if should_thread_windows_dialog else asyncio.Event()

    dialog_task = asyncio.create_task(
        dialog_handler.get_user_input(
            dialog_prompt,
            timeout_seconds,
            cancel_event=cancel_event,
            run_in_thread=should_thread_windows_dialog,
        )
    )
    telegram_task = asyncio.create_task(
        telegram_client.ask_question(telegram_prompt, timeout_seconds)
    )

    pending_tasks: dict[asyncio.Task[Optional[str]], str] = {
        dialog_task: "dialog",
        telegram_task: "telegram",
    }
    first_error: Optional[UserPromptError] = None

    while pending_tasks:
        done, _ = await asyncio.wait(pending_tasks.keys(), return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            channel_name = pending_tasks.pop(task)
            try:
                result = await task
            except TelegramPromptError as exc:
                if first_error is None:
                    first_error = UserPromptError(f"Telegram prompt failed: {exc}")
                continue
            except UserPromptError as exc:
                if first_error is None:
                    first_error = exc
                continue
            except asyncio.CancelledError:
                continue

            if result is None:
                continue

            if channel_name == "telegram":
                if cancel_event is not None:
                    cancel_event.set()
                    with suppress(asyncio.TimeoutError, UserPromptError):
                        await asyncio.wait_for(dialog_task, timeout=3)
                else:
                    dialog_task.add_done_callback(_consume_detached_task_result)
            else:
                telegram_task.cancel()
                with suppress(asyncio.CancelledError, TelegramPromptError):
                    await telegram_task

            return result

    if first_error is not None:
        raise first_error

    return None


async def get_user_input_from_configured_channel(
    dialog_prompt: str,
    telegram_prompt: str,
    timeout_seconds: int,
) -> Optional[str]:
    """Use the currently configured response channel(s) to collect user input."""
    if response_channel == "dialog":
        return await dialog_handler.get_user_input(dialog_prompt, timeout_seconds)

    if telegram_client is None:
        raise UserPromptError(
            "Telegram response channel is enabled, but no Telegram target was configured."
        )

    if response_channel == "telegram":
        try:
            return await telegram_client.ask_question(telegram_prompt, timeout_seconds)
        except TelegramPromptError as exc:
            raise UserPromptError(f"Telegram prompt failed: {exc}") from exc

    return await _get_first_channel_response(
        dialog_prompt,
        telegram_prompt,
        timeout_seconds,
    )


@mcp.tool()
async def asking_user_missing_context(question: str, context: str = "") -> str:
    """Ask the user to fill missing context or knowledge gaps during research and development.

    This tool enables AI assistants to pause workflows when they encounter missing context,
    need clarification on implementation choices, or require understanding of preferred
    approaches. Use this when conducting research and you need user input to proceed effectively.

    Common use cases:
    - Multiple valid implementation approaches exist (ask user for preference)
    - Need clarification on preferred tech stack or framework
    - Missing domain-specific requirements or constraints
    - Uncertain about user's specific goals or priorities
    - Need to understand existing codebase patterns or conventions

    Args:
        question: The specific question about missing context (max 1000 characters)
        context: Background info explaining why this context is needed (max 2000 characters)

    Returns:
        The user's response as a formatted string with status indicator

    Raises:
        ValueError: If parameters are invalid or out of acceptable ranges
    """

    timeout_seconds = dialog_timeout_seconds

    # Parameter validation with clear error messages
    if not question or not isinstance(question, str):
        return "❌ Error: 'question' parameter is required and must be a non-empty string"

    if len(question.strip()) == 0:
        return "❌ Error: 'question' cannot be empty or only whitespace"

    if len(question) > 1000:
        return (
            "❌ Error: 'question' is too long (max 1000 characters). Please shorten your question."
        )

    if not isinstance(timeout_seconds, int):
        return "❌ Error: 'timeout_seconds' must be an integer"

    if timeout_seconds < 1:
        return "❌ Error: 'timeout_seconds' must be at least 1 second"
    if not isinstance(context, str):
        return "❌ Error: 'context' must be a string (use empty string if no context needed)"

    if len(context) > 2000:
        return "❌ Error: 'context' is too long (max 2000 characters). Please provide a more concise context."

    try:
        telegram_notice = ""
        if response_channel == "both":
            telegram_notice = build_dialog_telegram_notice(dialog_handler.platform)

        dialog_prompt = build_prompt_text(
            question,
            context,
            timeout_seconds=timeout_seconds,
            include_timing_info=show_timing_info,
            extra_note=telegram_notice,
        )
        telegram_prompt = build_prompt_text(
            question,
            context,
            timeout_seconds=timeout_seconds,
            include_timing_info=show_timing_info,
        )

        # Get user input via the configured channel(s)
        response = await get_user_input_from_configured_channel(
            dialog_prompt,
            telegram_prompt,
            timeout_seconds,
        )

        # Handle different response scenarios with custom exceptions
        if response is None:
            # Timeout occurred
            timeout_minutes = timeout_seconds // 60
            timeout_display = f"{timeout_minutes} minute{'s' if timeout_minutes != 1 else ''}"
            raise UserPromptTimeout(f"No response received within {timeout_display}")

        if not response.strip():
            # Empty response (user clicked OK without entering text)
            return "⚠️ Empty response received. The user clicked OK but didn't enter any text. Please ask again if a response is needed."

        # Successful response
        clean_response = response.strip()

        # Format response with clear indicator
        return f"✅ User response: {clean_response}"

    except UserPromptTimeout as e:
        # Handle timeout with user-friendly message
        return f"⚠️ Timeout: {str(e)}. The dialog may have timed out or been cancelled. Please try asking again if needed."

    except UserPromptCancelled as e:
        # Handle cancellation
        return f"⚠️ Cancelled: {str(e)}. The user cancelled the prompt. Please try again or rephrase your question."

    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully without crashing the server
        raise UserPromptCancelled("User interrupted the prompt with Ctrl+C")

    except UserPromptError as e:
        # Handle custom user prompt errors
        return f"❌ User Prompt Error: {str(e)}"

    except Exception as e:
        # Comprehensive error handling with helpful context
        error_context = f"Question: {question[:100]}{'...' if len(question) > 100 else ''}"
        return f"❌ Error getting user input: {str(e)}\n\nContext: {error_context}\n\nThe GUI dialog system may not be available. Check that the required dependencies are installed (zenity on Linux, osascript on macOS, tkinter on Windows)."


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """Create a Starlette application that can serve the provided MCP server with SSE.

    Sets up a Starlette web application with routes for SSE (Server-Sent Events)
    communication with the MCP server.

    Args:
        mcp_server: The MCP server instance to connect
        debug: Whether to enable debug mode for the Starlette app

    Returns:
        A configured Starlette application
    """
    # Create an SSE transport with a base path for messages
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        """Handler for SSE connections.

        Establishes an SSE connection and connects it to the MCP server.

        Args:
            request: The incoming HTTP request
        """
        # Connect the SSE transport to the request
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            # Run the MCP server with the SSE streams
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    # Create and return the Starlette application with routes
    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),  # Endpoint for SSE connections
            Mount("/messages/", app=sse.handle_post_message),  # Endpoint for posting messages
        ],
    )


def main() -> None:
    """Main entry point for the User Prompt MCP server.

    This function serves as the primary entry point when the server is launched
    via uvx or direct Python execution. It handles argument parsing and server startup.
    """
    # Get the underlying MCP server from the FastMCP instance
    mcp_server = mcp._mcp_server  # noqa: WPS437

    import argparse

    # Set up command-line argument parsing
    parser = argparse.ArgumentParser(
        description="Run User Prompt MCP server with configurable transport"
    )
    # Allow choosing between stdio and SSE transport modes
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (stdio or sse)",
    )
    # Host configuration for SSE mode
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (for SSE mode)")
    # Port configuration for SSE mode
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (for SSE mode)")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_DIALOG_TIMEOUT_SECONDS,
        help=(
            "Dialog response timeout in seconds. Defaults to "
            f"{DEFAULT_DIALOG_TIMEOUT_SECONDS} seconds."
        ),
    )
    parser.add_argument(
        "--dialog-title",
        default=None,
        help="Dialog window title. Defaults to the built-in title.",
    )
    parser.add_argument(
        "--show-timing-info",
        action="store_true",
        help=(
            "Add Issued at / Answer until lines to dialog text. The MCP client may time "
            "out sooner than the dialog itself."
        ),
    )
    parser.add_argument(
        "--response-channel",
        choices=["dialog", "telegram", "both"],
        default=DEFAULT_RESPONSE_CHANNEL,
        help="Where to collect the reply: dialog, telegram, or both.",
    )
    parser.add_argument(
        "--telegram",
        default=None,
        help=(
            "Telegram bot target as a single argument: '<bot_token> <chat_id>'. "
            "Required for --response-channel telegram or both."
        ),
    )
    args = parser.parse_args()
    try:
        telegram_target = parse_telegram_target(args.telegram)
    except ValueError as exc:
        parser.error(str(exc))

    if args.response_channel != "dialog" and telegram_target is None:
        parser.error("--telegram is required when --response-channel is telegram or both.")

    global dialog_handler
    global dialog_timeout_seconds
    initialize_time_locale()
    global show_timing_info
    global response_channel
    global telegram_client
    dialog_handler = GUIDialogHandler(dialog_title=args.dialog_title)
    dialog_timeout_seconds = args.timeout_seconds
    show_timing_info = args.show_timing_info
    response_channel = args.response_channel
    telegram_client = TelegramPromptClient(telegram_target) if telegram_target is not None else None

    # Launch the server with the selected transport mode
    if args.transport == "stdio":
        # Run with stdio transport (default)
        # This mode communicates through standard input/output
        mcp.run(transport="stdio")
    else:
        # Run with SSE transport (web-based)
        # Create a Starlette app to serve the MCP server
        starlette_app = create_starlette_app(mcp_server, debug=True)
        # Start the web server with the configured host and port
        uvicorn.run(starlette_app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
