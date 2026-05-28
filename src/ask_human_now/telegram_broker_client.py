"""Client-side local Telegram broker discovery, startup, and prompt forwarding."""

import asyncio
import datetime as dt
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .broker_state import (
    TelegramBrokerHealth,
    acquire_startup_lock,
    load_broker_state,
    resolve_broker_state_dir,
    resolve_target_broker_state_dir,
)
from .prompt_formatting import build_telegram_prompt_text
from .telegram_models import TelegramConfig, TelegramPromptError


class TelegramBrokerClient:
    """Talk to a per-target local Telegram broker process."""

    def __init__(
        self,
        telegram_target: TelegramConfig,
        download_dir: Path,
        *,
        broker_state_root: Optional[Path] = None,
        broker_label: Optional[str] = None,
    ) -> None:
        self.telegram_target = telegram_target
        self.download_dir = download_dir.resolve()
        self.broker_state_root = (broker_state_root or resolve_broker_state_dir()).resolve()
        self.broker_label = broker_label
        self.target_state_dir = resolve_target_broker_state_dir(
            self.broker_state_root,
            telegram_target,
        )

    async def ask_question(
        self,
        question: str,
        context: str,
        *,
        prompt_id: str,
        timeout_seconds: int,
        include_timing_info: bool,
        issued_at: dt.datetime,
    ) -> Optional[str]:
        """Ensure a local broker exists, then forward one Telegram prompt through it."""
        broker_health = await self._ensure_local_broker()
        prompt_text = build_telegram_prompt_text(
            question,
            context,
            prompt_id=prompt_id,
            timeout_seconds=timeout_seconds,
            include_timing_info=include_timing_info,
            issued_at=issued_at,
            broker_label=broker_health.broker_label,
            broker_id=broker_health.broker_id,
        )

        response = await self._broker_request(
            broker_health.listen_url,
            "prompts",
            {
                "prompt_id": prompt_id,
                "prompt_text": prompt_text,
                "timeout_seconds": timeout_seconds,
                "download_dir": str(self.download_dir),
            },
            timeout=timeout_seconds + 30,
        )

        status = response.get("status")
        if status == "ok":
            reply_text = response.get("response")
            if not isinstance(reply_text, str):
                raise TelegramPromptError("Broker prompt response was missing a reply string.")
            return reply_text

        if status == "timeout":
            return None

        error_text = response.get("error", "unknown broker error")
        raise TelegramPromptError(f"Broker prompt failed: {error_text}")

    async def _ensure_local_broker(self) -> TelegramBrokerHealth:
        """Reuse a healthy broker for this target, or start one if needed."""
        existing_health = await self._probe_persisted_broker()
        if existing_health is not None:
            return existing_health

        with acquire_startup_lock(self.target_state_dir):
            existing_health = await self._probe_persisted_broker()
            if existing_health is not None:
                return existing_health

            self._spawn_local_broker()
            return await self._wait_for_local_broker()

    async def _probe_persisted_broker(self) -> Optional[TelegramBrokerHealth]:
        """Read persisted broker state and verify that the broker is still healthy."""
        state = load_broker_state(self.target_state_dir)
        if state is None or not state.listen_url:
            return None

        try:
            health = await self._fetch_health(state.listen_url)
        except TelegramPromptError:
            return None

        if health.broker_id != state.identity.broker_id:
            return None

        return health

    async def _wait_for_local_broker(self) -> TelegramBrokerHealth:
        """Poll broker state and health until the spawned process is ready."""
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[str] = None

        while asyncio.get_running_loop().time() < deadline:
            state = load_broker_state(self.target_state_dir)
            if state is not None and state.listen_url:
                try:
                    return await self._fetch_health(state.listen_url)
                except TelegramPromptError as exc:
                    last_error = str(exc)

            await asyncio.sleep(0.2)

        if last_error is not None:
            raise TelegramPromptError(f"Local Telegram broker did not become healthy: {last_error}")
        raise TelegramPromptError("Local Telegram broker did not become healthy in time.")

    def _spawn_local_broker(self) -> None:
        """Start a detached local broker process for this target."""
        command = [
            sys.executable,
            "-m",
            "ask_human_now",
            "--telegram-broker",
            "--telegram",
            f"{self.telegram_target.bot_token} {self.telegram_target.chat_id}",
            "--telegram-broker-state-dir",
            str(self.broker_state_root),
            "--telegram-broker-host",
            "127.0.0.1",
            "--telegram-broker-port",
            "0",
        ]
        if self.broker_label:
            command.extend(["--telegram-broker-label", self.broker_label])

        creation_flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            creationflags=creation_flags,
        )

    async def _fetch_health(self, listen_url: str) -> TelegramBrokerHealth:
        """Read and validate one broker health response."""
        response = await self._broker_request(listen_url, "health", None, timeout=10, method="GET")

        try:
            status = str(response["status"])
            broker_id = str(response["broker_id"])
            broker_label = str(response["broker_label"])
            target_key = str(response["target_key"])
            response_listen_url = str(response["listen_url"])
        except KeyError as exc:
            missing_key = exc.args[0]
            raise TelegramPromptError(
                f"Broker health response was missing {missing_key!r}."
            ) from exc

        if status != "ok":
            raise TelegramPromptError(f"Broker health status was {status!r}.")

        return TelegramBrokerHealth(
            broker_id=broker_id,
            broker_label=broker_label,
            listen_url=response_listen_url,
            target_key=target_key,
        )

    async def _broker_request(
        self,
        listen_url: str,
        path: str,
        payload: Optional[dict[str, Any]],
        *,
        timeout: int,
        method: str = "POST",
    ) -> dict[str, Any]:
        """Issue one broker HTTP request and decode the JSON response."""
        return await asyncio.to_thread(
            self._broker_request_sync,
            listen_url,
            path,
            payload,
            timeout,
            method,
        )

    def _broker_request_sync(
        self,
        listen_url: str,
        path: str,
        payload: Optional[dict[str, Any]],
        timeout: int,
        method: str,
    ) -> dict[str, Any]:
        """Perform one blocking broker HTTP request."""
        request_url = f"{listen_url.rstrip('/')}/{path.lstrip('/')}"
        request_data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        request_obj = urllib.request.Request(
            request_url,
            data=request_data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request_obj, timeout=timeout) as response:
                payload_json = json.load(response)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise TelegramPromptError(
                f"Broker {path} request failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except OSError as exc:
            raise TelegramPromptError(f"Broker {path} request failed: {exc}") from exc

        if not isinstance(payload_json, dict):
            raise TelegramPromptError(f"Broker {path} response was not a JSON object.")

        return payload_json
