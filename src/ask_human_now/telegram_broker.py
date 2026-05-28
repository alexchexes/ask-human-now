"""HTTP broker process for safe Telegram prompt concurrency on one machine."""

import asyncio
import socket
from pathlib import Path
from typing import Any, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .broker_state import (
    TelegramBrokerIdentity,
    load_or_create_broker_identity,
    persist_broker_listen_url,
)
from .telegram_client import TelegramPromptClient
from .telegram_models import (
    TelegramConfig,
    TelegramPromptError,
    resolve_telegram_download_dir,
    resolve_telegram_target_key,
)


def build_broker_listen_url(host: str, port: int) -> str:
    """Build the local discovery URL for a running broker."""
    if host in {"0.0.0.0", "::"}:
        resolved_host = "127.0.0.1"
    else:
        resolved_host = host
    return f"http://{resolved_host}:{port}"


def build_broker_health_payload(
    identity: TelegramBrokerIdentity,
    *,
    listen_url: str,
    target_key: str,
) -> dict[str, str]:
    """Build the broker health response."""
    return {
        "status": "ok",
        "broker_id": identity.broker_id,
        "broker_label": identity.broker_label,
        "listen_url": listen_url,
        "target_key": target_key,
        "version": __version__,
    }


def create_telegram_broker_app(
    identity: TelegramBrokerIdentity,
    *,
    listen_url: str,
    telegram_client: TelegramPromptClient,
    target_key: str,
) -> Starlette:
    """Create the broker HTTP app."""

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            build_broker_health_payload(
                identity,
                listen_url=listen_url,
                target_key=target_key,
            )
        )

    async def prompts(request: Request) -> JSONResponse:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse(
                {"status": "error", "error": "Request body must be a JSON object."},
                status_code=400,
            )

        prompt_text = payload.get("prompt_text")
        prompt_id = payload.get("prompt_id")
        timeout_seconds = payload.get("timeout_seconds")
        download_dir_raw = payload.get("download_dir")

        if not isinstance(prompt_text, str) or not prompt_text.strip():
            return JSONResponse(
                {"status": "error", "error": "prompt_text must be a non-empty string."},
                status_code=400,
            )
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            return JSONResponse(
                {"status": "error", "error": "prompt_id must be a non-empty string."},
                status_code=400,
            )
        if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            return JSONResponse(
                {"status": "error", "error": "timeout_seconds must be a positive integer."},
                status_code=400,
            )
        if not isinstance(download_dir_raw, str) or not download_dir_raw.strip():
            return JSONResponse(
                {"status": "error", "error": "download_dir must be a non-empty string."},
                status_code=400,
            )

        download_dir = resolve_telegram_download_dir(download_dir_raw)
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            response = await telegram_client.ask_question(
                prompt_text,
                timeout_seconds,
                prompt_id,
                download_dir,
            )
        except TelegramPromptError as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

        if response is None:
            return JSONResponse({"status": "timeout"})

        return JSONResponse({"status": "ok", "response": response})

    return Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route("/prompts", endpoint=prompts, methods=["POST"]),
        ],
    )


def _create_bound_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket so the chosen port is known before serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(socket.SOMAXCONN)
    return sock


def run_telegram_broker(
    *,
    host: str,
    port: int,
    state_dir: Path,
    telegram_target: TelegramConfig,
    broker_label: Optional[str] = None,
) -> None:
    """Run the Telegram broker service and persist its local discovery info."""
    identity = load_or_create_broker_identity(state_dir, broker_label=broker_label)
    server_socket = _create_bound_socket(host, port)
    target_key = resolve_telegram_target_key(telegram_target)

    try:
        actual_port = int(server_socket.getsockname()[1])
        listen_url = build_broker_listen_url(host, actual_port)
        persist_broker_listen_url(state_dir, listen_url)
        telegram_client = TelegramPromptClient(
            telegram_target,
            broker_identity=identity,
        )
        app = create_telegram_broker_app(
            identity,
            listen_url=listen_url,
            telegram_client=telegram_client,
            target_key=target_key,
        )
        config = uvicorn.Config(app, host=host, port=actual_port, log_level="info")
        server = uvicorn.Server(config)
        try:
            asyncio.run(server.serve(sockets=[server_socket]))
        except KeyboardInterrupt:
            # Uvicorn already performs a graceful shutdown on Ctrl+C. Swallow the final
            # wrapper-level KeyboardInterrupt here so the broker exits cleanly without a
            # traceback after shutdown completes.
            return
    finally:
        server_socket.close()
