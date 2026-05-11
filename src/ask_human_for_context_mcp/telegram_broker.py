"""HTTP broker process for safe Telegram prompt concurrency on one machine."""

import asyncio
import socket
from pathlib import Path
from typing import Optional

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
) -> dict[str, str]:
    """Build the broker health response."""
    return {
        "status": "ok",
        "broker_id": identity.broker_id,
        "broker_label": identity.broker_label,
        "listen_url": listen_url,
        "version": __version__,
    }


def create_telegram_broker_app(
    identity: TelegramBrokerIdentity,
    *,
    listen_url: str,
) -> Starlette:
    """Create the broker HTTP app."""

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(build_broker_health_payload(identity, listen_url=listen_url))

    return Starlette(debug=False, routes=[Route("/health", endpoint=health)])


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
    broker_label: Optional[str] = None,
) -> None:
    """Run the Telegram broker service and persist its local discovery info."""
    identity = load_or_create_broker_identity(state_dir, broker_label=broker_label)
    server_socket = _create_bound_socket(host, port)

    try:
        actual_port = int(server_socket.getsockname()[1])
        listen_url = build_broker_listen_url(host, actual_port)
        persist_broker_listen_url(state_dir, listen_url)
        app = create_telegram_broker_app(identity, listen_url=listen_url)
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
