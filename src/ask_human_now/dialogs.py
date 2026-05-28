"""Cross-platform GUI dialog handling for ask-human prompts."""

import asyncio
import platform
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional, cast

from .prompt_formatting import resolve_dialog_title

DEFAULT_DIALOG_TIMEOUT_SECONDS = 120
PACKAGE_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class UserPromptCancelled(Exception):
    """Raised when user cancels the prompt or interrupts the process."""

    pass


class UserPromptError(Exception):
    """Generic error for user prompt operations."""

    pass


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
        """Get user input via native GUI dialog with timeout."""
        try:
            if self.platform == "Darwin":
                return await self._macos_dialog(question, timeout, cancel_event=cancel_event)
            if self.platform == "Linux":
                return await self._linux_dialog(question, timeout, cancel_event=cancel_event)
            return await self._windows_dialog(
                question,
                timeout,
                run_in_thread=run_in_thread,
            )
        except KeyboardInterrupt:
            raise UserPromptCancelled("User interrupted the dialog with Ctrl+C")
        except Exception as exc:
            raise UserPromptError(
                f"GUI dialog failed: {exc}. Ensure osascript (macOS), zenity (Linux), or "
                "tkinter (Windows) is available."
            ) from exc

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

        tasks: set[asyncio.Task[Any]] = {communicate_task}
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

            windll = getattr(ctypes, "windll", None)
            if windll is None:
                return

            try:
                windll.shcore.SetProcessDpiAwareness(2)
                return
            except Exception:
                pass

            try:
                windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        except Exception:
            pass

    def _configure_windows_tk_scaling(self, root: Any) -> None:
        """Match Tk scaling to the current monitor DPI when available."""
        try:
            import ctypes

            windll = getattr(ctypes, "windll", None)
            dpi = 0
            try:
                if windll is not None:
                    dpi = windll.user32.GetDpiForWindow(root.winfo_id())
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
        """macOS dialog using osascript."""
        icon_path = PACKAGE_ASSETS_DIR / "agent-asks.icns"

        if icon_path.exists():
            icon_clause = f'with icon file (POSIX file "{icon_path}")'
        else:
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

            stdout, _stderr, was_cancelled = await self._communicate_or_cancel(
                process, cancel_event
            )
            if was_cancelled:
                return None

            if process.returncode == 0:
                output = stdout.decode().strip()
                if "text returned:" in output:
                    text_part = output.split("text returned:")[1]
                    if ", " in text_part:
                        return text_part.split(", ")[0].strip()
                    return text_part.strip()
                if "gave up:true" in output:
                    return None
                if "button returned:" in output and "text returned:" not in output:
                    return ""
            return None
        except Exception:
            return None

    async def _linux_dialog(
        self, question: str, timeout: int, *, cancel_event: Optional[asyncio.Event] = None
    ) -> Optional[str]:
        """Linux dialog using zenity."""
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

            stdout, _stderr, was_cancelled = await self._communicate_or_cancel(
                process, cancel_event
            )
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
        """Windows dialog using tkinter."""
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
            root = tk.Tk()
            self._configure_windows_tk_scaling(root)
            root.withdraw()
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

    def _get_linux_icon_args(self) -> list[str]:
        """Get icon arguments for Linux zenity dialog."""
        icon_path = PACKAGE_ASSETS_DIR / "agent-asks.png"
        if icon_path.exists():
            print(f"✅ Using Ask Human icon for Linux: {icon_path}")
            return [f"--window-icon={icon_path}"]

        return ["--question"]

    def _set_windows_icon(self, root: Any) -> None:
        """Set icon for Windows tkinter dialog."""
        icon_path = PACKAGE_ASSETS_DIR / "agent-asks.ico"
        if not icon_path.exists():
            return

        try:
            print(f"✅ Using Ask Human icon for Windows: {icon_path}")
            root.iconbitmap(icon_path)
        except Exception:
            pass
