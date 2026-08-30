"""Minimal Telegram Bot API client with one-chat authorization."""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


class TelegramBot:
    def __init__(self, config: dict[str, Any], status_builder: Callable[[], str]) -> None:
        self.config = config
        self.status_builder = status_builder
        token = Path(config["token_file"]).read_text(encoding="ascii").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise RuntimeError("Telegram token file has an invalid format")
        self.base_url = f"https://api.telegram.org/bot{token}/"
        self.state_dir = Path(config["state_dir"])
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.chat_file = Path(config["allowed_chat_id_file"])
        self.offset_file = self.state_dir / "telegram_offset"

    def api(self, method: str, **params: object) -> dict[str, Any]:
        body = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(self.base_url + method, data=body)
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API method {method} failed")
        return result

    def send(self, chat_id: int, text: str) -> None:
        self.api("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview="true")

    @staticmethod
    def read_int(path: Path, default: int = 0) -> int:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return default

    @staticmethod
    def write_int(path: Path, value: int) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{value}\n")
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def authorize(self, chat: dict[str, Any], arguments: list[str]) -> bool:
        chat_id = int(chat.get("id", 0))
        allowed = self.read_int(self.chat_file)
        if allowed:
            return chat_id == allowed
        secret_path = self.config.get("pairing_secret_file")
        if chat.get("type") != "private" or not arguments or not secret_path:
            return False
        try:
            expected = Path(secret_path).read_text(encoding="ascii").strip()
        except OSError:
            return False
        if not expected or not hmac.compare_digest(arguments[0], expected):
            return False
        self.write_int(self.chat_file, chat_id)
        return True

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        chat = message.get("chat")
        if not isinstance(text, str) or not isinstance(chat, dict) or not text.startswith("/"):
            return
        parts = text.strip().split()
        command = parts[0].split("@", 1)[0].lower()
        if not self.authorize(chat, parts[1:]):
            return
        chat_id = int(chat["id"])
        if command == "/status":
            try:
                self.api("sendChatAction", chat_id=chat_id, action="typing")
                self.send(chat_id, self.status_builder())
            except Exception:
                self.send(chat_id, "Status collection failed. Check the service journal.")
        elif command in {"/start", "/help"}:
            self.send(chat_id, "Send /status for one live infrastructure summary.")

    def run_forever(self) -> None:
        self.api(
            "setMyCommands",
            commands=json.dumps([
                {"command": "status", "description": "Full infrastructure status"},
                {"command": "help", "description": "Show available commands"},
            ]),
        )
        offset = self.read_int(self.offset_file)
        while True:
            try:
                result = self.api("getUpdates", offset=offset, timeout=25, allowed_updates='["message"]')
                for update in result.get("result", []):
                    if not isinstance(update, dict):
                        continue
                    update_id = int(update.get("update_id", 0))
                    self.handle_update(update)
                    offset = max(offset, update_id + 1)
                    self.write_int(self.offset_file, offset)
            except (OSError, urllib.error.URLError, RuntimeError, ValueError):
                time.sleep(5)
