"""Prompt formatting helpers for dialogs and Telegram delivery."""

import datetime as dt
import html
import locale
import secrets
from typing import Optional

DEFAULT_DIALOG_TITLE = "Agent asks..."
TELEGRAM_DOWNLOAD_LIMIT_LABEL = "20 MB"
TELEGRAM_PROMPT_SEPARATOR = "─" * 12
TIMING_INFO_TIMEOUT_NOTE = "client may time out sooner"


def resolve_dialog_title(dialog_title: Optional[str] = None) -> str:
    """Resolve the dialog title from CLI input or default."""
    if dialog_title and dialog_title.strip():
        return dialog_title.strip()

    return DEFAULT_DIALOG_TITLE


def generate_prompt_id(now: Optional[dt.datetime] = None) -> str:
    """Generate a short human-readable prompt identifier for Telegram workflows."""
    moment = (now or dt.datetime.now().astimezone()).astimezone()
    return f"Q{moment.strftime('%y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


def format_dialog_timestamp(moment: dt.datetime) -> str:
    """Format dialog timestamps using the current locale's short date/time format."""
    try:
        formatted = moment.astimezone().strftime("%x %X").strip()
        if formatted:
            return formatted
    except Exception:
        pass

    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def escape_telegram_html(text: str) -> str:
    """Escape arbitrary prompt text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)


def initialize_time_locale() -> None:
    """Initialize process-wide time formatting from the OS locale once at startup."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except Exception:
        pass


def build_timing_info_lines(issued_at: dt.datetime, timeout_seconds: int) -> list[str]:
    """Build optional timing metadata lines for dialogs or Telegram prompts."""
    answer_until = issued_at + dt.timedelta(seconds=timeout_seconds)
    return [
        f"Issued at: {format_dialog_timestamp(issued_at)}",
        f"Answer until: {format_dialog_timestamp(answer_until)}",
        f"({TIMING_INFO_TIMEOUT_NOTE})",
    ]


def build_timing_info_block(issued_at: dt.datetime, timeout_seconds: int) -> str:
    """Build the optional timing metadata shown in dialogs."""
    timing_lines = build_timing_info_lines(issued_at, timeout_seconds)
    return f"{timing_lines[0]} | {timing_lines[1]} {timing_lines[2]}"


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
    issued_at: Optional[dt.datetime] = None,
) -> str:
    """Build the formatted prompt text for native dialogs."""
    separator = "─" * 40
    question_block = f"❓ Question:\n{question.strip()}"
    if extra_note.strip():
        question_block = f"{question_block}\n\n{extra_note.strip()}"

    if context.strip():
        full_question = f"📋 Context:\n{context.strip()}\n\n{separator}\n\n{question_block}"
    else:
        full_question = question_block

    if include_timing_info:
        effective_issued_at = issued_at or dt.datetime.now().astimezone()
        return (
            f"{full_question}\n\n{separator}\n\n"
            f"{build_timing_info_block(effective_issued_at, timeout_seconds)}"
        )

    return full_question


def build_telegram_prompt_text(
    question: str,
    context: str,
    *,
    prompt_id: str,
    timeout_seconds: int,
    include_timing_info: bool,
    issued_at: Optional[dt.datetime] = None,
    broker_label: Optional[str] = None,
    broker_id: Optional[str] = None,
) -> str:
    """Build a Telegram-specific prompt using HTML parse mode and compact metadata."""
    effective_issued_at = issued_at or dt.datetime.now().astimezone()
    parts: list[str] = []

    if context.strip():
        parts.extend(
            [
                "<b>📋 Context:</b>",
                escape_telegram_html(context.strip()),
                "",
                TELEGRAM_PROMPT_SEPARATOR,
                "",
            ]
        )

    parts.extend(
        [
            "<b>❓ Question:</b>",
            escape_telegram_html(question.strip()),
            "",
            TELEGRAM_PROMPT_SEPARATOR,
            "",
        ]
    )

    metadata_lines: list[str] = []
    if include_timing_info:
        metadata_lines.extend(build_timing_info_lines(effective_issued_at, timeout_seconds))

    metadata_lines.extend(
        [
            f"Answers support text or files up to {TELEGRAM_DOWNLOAD_LIMIT_LABEL}.",
            f"Prompt ID: {prompt_id}",
        ]
    )
    if broker_label and broker_id:
        metadata_lines.append(f"Broker: {broker_label} [{broker_id}]")
    metadata_block = "\n".join(escape_telegram_html(line) for line in metadata_lines)
    parts.extend(
        [
            f"<blockquote expandable>{metadata_block}</blockquote>",
            "",
            '↩️ Use "Reply" on this message to answer.',
        ]
    )

    return "\n".join(parts)
