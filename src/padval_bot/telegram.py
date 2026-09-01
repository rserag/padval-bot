"""Minimal Telegram Bot API client with one-chat authorization."""

from __future__ import annotations

import hmac
import html
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .torrent import (
    Magnet,
    TorrentError,
    TorrentService,
    TorrentSnapshot,
    TorrentSubmissionUncertain,
)
from .tracking import TrackingStateError, TrackingStore


@dataclass(slots=True)
class PendingTorrent:
    request_id: str
    magnet: Magnet
    created_at: float
    custom_prompt_message_id: int | None = None


class TelegramBot:
    def __init__(
        self,
        config: dict[str, Any],
        status_builder: Callable[[], str],
        torrent_service: TorrentService | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
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
        self.heartbeat_file = (
            Path(config["heartbeat_file"]) if config.get("heartbeat_file") else None
        )
        self.torrent_service = torrent_service
        self.clock = clock
        self.wall_clock = wall_clock
        self.pending_torrents: dict[int, PendingTorrent] = {}
        self.tracking_store = (
            TrackingStore(
                self.state_dir / "torrent-tracking.json",
                torrent_service.locations.tracking,
            )
            if torrent_service is not None
            and torrent_service.locations.tracking.enabled
            else None
        )

    def api(self, method: str, **params: object) -> dict[str, Any]:
        body = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(self.base_url + method, data=body)
        # The URL is always the literal Telegram HTTPS origin plus a validated token.
        with urllib.request.urlopen(request, timeout=35) as response:  # nosec B310
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API method {method} failed")
        return result

    def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        params: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        if reply_to_message_id is not None:
            params["reply_parameters"] = json.dumps(
                {"message_id": reply_to_message_id}, separators=(",", ":")
            )
        response = self.api(
            "sendMessage",
            **params,
        )
        result = response.get("result")
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
        return None

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup, separators=(",", ":"))
        try:
            self.api("editMessageText", **params)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return "0 B"

    @staticmethod
    def _format_eta(seconds: int) -> str:
        if seconds <= 0 or seconds >= 8_640_000:
            return "Unknown"
        if seconds < 60:
            return "<1 minute"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} minutes"
        hours, minutes = divmod(minutes, 60)
        if hours < 48:
            return f"{hours}h {minutes}m"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"

    @staticmethod
    def _safe_torrent_name(value: str, limit: int = 120) -> str:
        cleaned = "".join(
            character if ord(character) >= 32 and ord(character) != 127 else " "
            for character in value
        )
        cleaned = " ".join(cleaned.split()) or "Unnamed torrent"
        return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"

    @staticmethod
    def _state_label(snapshot: TorrentSnapshot) -> str:
        if snapshot.complete:
            return "Complete"
        labels = {
            "downloading": "Downloading",
            "stalledDL": "Stalled",
            "metaDL": "Fetching metadata",
            "forcedDL": "Downloading",
            "queuedDL": "Queued",
            "pausedDL": "Paused",
            "stoppedDL": "Stopped",
            "checkingDL": "Checking",
            "error": "Error",
            "missingFiles": "Missing files",
        }
        return labels.get(snapshot.state, snapshot.state or "Unknown")

    def _downloads(self) -> tuple[TorrentSnapshot, ...]:
        if self.torrent_service is None:
            return ()
        snapshots = self.torrent_service.list_torrents(tag=self.torrent_service.tag)
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (
                    item.complete,
                    self._safe_torrent_name(item.name).lower(),
                ),
            )
        )

    def _downloads_view(
        self, snapshots: tuple[TorrentSnapshot, ...]
    ) -> tuple[str, dict[str, Any]]:
        if not snapshots:
            return "<b>Downloads</b>\nNo bot-added torrents found.", {
                "inline_keyboard": []
            }
        lines = ["<b>Downloads</b>"]
        rows: list[list[dict[str, str]]] = []
        for index, snapshot in enumerate(snapshots[:10], start=1):
            name = self._safe_torrent_name(snapshot.name, 72)
            percent = snapshot.progress * 100
            if snapshot.complete:
                detail = "Complete"
            else:
                detail = (
                    f"{percent:.1f}% · {self._format_bytes(snapshot.download_speed)}/s"
                    f" · ETA {self._format_eta(snapshot.eta)}"
                )
            lines.extend((f"\n{index}. <b>{html.escape(name)}</b>", detail))
            button_name = self._safe_torrent_name(snapshot.name, 32)
            rows.append(
                [
                    {
                        "text": f"{percent:.0f}% · {button_name}",
                        "callback_data": f"dl:{snapshot.qbit_hash[:16]}:show",
                    }
                ]
            )
        if len(snapshots) > 10:
            lines.append(f"\nShowing 10 of {len(snapshots)} downloads.")
        return "\n".join(lines), {"inline_keyboard": rows}

    def _download_detail(self, snapshot: TorrentSnapshot) -> tuple[str, dict[str, Any]]:
        percent = snapshot.progress * 100
        filled = min(10, max(0, round(snapshot.progress * 10)))
        bar = "█" * filled + "░" * (10 - filled)
        downloaded = max(0, snapshot.size - snapshot.amount_left)
        notifications = bool(
            self.tracking_store
            and self.tracking_store.notification_enabled(snapshot.qbit_hash)
        )
        destination = (
            self.torrent_service.destination_label(snapshot.save_path)
            if self.torrent_service is not None
            else "Unknown"
        )
        name = self._safe_torrent_name(snapshot.name)
        text = (
            f"<b>{html.escape(name)}</b>\n\n"
            f"<code>{bar}</code> {percent:.1f}%\n"
            f"{self._format_bytes(downloaded)} / {self._format_bytes(snapshot.size)}\n"
            f"Speed: {self._format_bytes(snapshot.download_speed)}/s\n"
            f"ETA: {self._format_eta(snapshot.eta)}\n"
            f"State: {html.escape(self._state_label(snapshot))}\n"
            f"Destination: {html.escape(destination)}\n"
            f"Notifications: {'On' if notifications else 'Off'}\n\n"
            f"Checked: {time.strftime('%H:%M:%S', time.localtime(self.wall_clock()))}"
        )
        short_id = snapshot.qbit_hash[:16]
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Refresh", "callback_data": f"dl:{short_id}:refresh"},
                    {
                        "text": "Notifications: " + ("On" if notifications else "Off"),
                        "callback_data": f"dl:{short_id}:toggle",
                    },
                ],
                [{"text": "Back", "callback_data": "dl:list"}],
            ]
        }
        return text, keyboard

    @staticmethod
    def _resolve_download(
        snapshots: tuple[TorrentSnapshot, ...], prefix: str
    ) -> TorrentSnapshot | None:
        if not re.fullmatch(r"[0-9a-f]{16}", prefix):
            return None
        matches = [item for item in snapshots if item.qbit_hash.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

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

    def _expire_pending(self, chat_id: int) -> PendingTorrent | None:
        pending = self.pending_torrents.get(chat_id)
        if pending is None or self.torrent_service is None:
            return pending
        if (
            self.clock() - pending.created_at
            > self.torrent_service.locations.pending_ttl_seconds
        ):
            self.pending_torrents.pop(chat_id, None)
            return None
        return pending

    def _torrent_keyboard(self, request_id: str) -> dict[str, Any]:
        if self.torrent_service is None:
            raise RuntimeError("torrent service is unavailable")
        buttons = [
            {
                "text": location.label,
                "callback_data": f"torrent:{request_id}:{location.id}",
            }
            for location in self.torrent_service.locations.save_locations
        ]
        if self.torrent_service.locations.custom_enabled:
            buttons.append(
                {"text": "Custom", "callback_data": f"torrent:{request_id}:custom"}
            )
        buttons.append(
            {"text": "Cancel", "callback_data": f"torrent:{request_id}:cancel"}
        )
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        return {"inline_keyboard": rows}

    def _start_torrent(self, chat_id: int, argument: str) -> None:
        if self.torrent_service is None:
            self.send(chat_id, "Torrent submission is not configured.")
            return
        if not argument:
            self.send(chat_id, "Usage: <code>/torrent magnet:?xt=...</code>")
            return
        try:
            magnet = self.torrent_service.validate_magnet(argument)
        except TorrentError as exc:
            self.send(chat_id, html.escape(str(exc)))
            return
        request_id = secrets.token_hex(4)
        self.pending_torrents[chat_id] = PendingTorrent(
            request_id, magnet, self.clock()
        )
        self.send(
            chat_id,
            "Where should this torrent be saved?",
            reply_markup=self._torrent_keyboard(request_id),
        )

    def _register_tracking(
        self,
        chat_id: int,
        pending: PendingTorrent,
        destination_label: str,
        tracking_tag: str | None,
    ) -> bool:
        if self.tracking_store is None or tracking_tag is None:
            return False
        try:
            self.tracking_store.register_pending(
                pending.request_id,
                chat_id=chat_id,
                destination_label=destination_label,
                discovery_tag=tracking_tag,
                now=self.wall_clock(),
            )
        except TrackingStateError:
            return False
        return True

    def _submit_torrent(
        self,
        chat_id: int,
        pending: PendingTorrent,
        save_path: str,
        destination_label: str,
    ) -> None:
        if self.torrent_service is None:
            raise RuntimeError("torrent service is unavailable")
        self.pending_torrents.pop(chat_id, None)
        tracking_tag = (
            f"padval-track-{pending.request_id}"
            if self.tracking_store is not None
            and self.torrent_service.locations.tracking.auto_track_new
            else None
        )
        try:
            self.api("sendChatAction", chat_id=chat_id, action="typing")
            if tracking_tag is None:
                self.torrent_service.submit(pending.magnet, save_path)
            else:
                self.torrent_service.submit(
                    pending.magnet, save_path, tracking_tag=tracking_tag
                )
        except TorrentSubmissionUncertain as exc:
            tracking = self._register_tracking(
                chat_id, pending, destination_label, tracking_tag
            )
            suffix = " The bot will watch for it." if tracking else ""
            self.send(chat_id, "⚠️ " + html.escape(str(exc)) + suffix)
            return
        except TorrentError as exc:
            self.send(chat_id, "❌ " + html.escape(str(exc)))
            return
        tracking = self._register_tracking(
            chat_id, pending, destination_label, tracking_tag
        )
        display_hash = pending.magnet.display_hash
        shortened = (
            display_hash
            if len(display_hash) <= 16
            else f"{display_hash[:8]}…{display_hash[-8:]}"
        )
        self.send(
            chat_id,
            "✅ Added to qBittorrent\n"
            f"Destination: <code>{html.escape(save_path)}</code>\n"
            f"Info hash: <code>{html.escape(shortened)}</code>\n"
            + (
                "Notifications: On"
                if tracking
                else "Progress tracking is unavailable for this request."
            ),
            reply_markup=(
                {
                    "inline_keyboard": [
                        [{"text": "View downloads", "callback_data": "dl:list"}]
                    ]
                }
                if tracking
                else None
            ),
        )

    def _handle_download_callback(self, callback: dict[str, Any]) -> bool:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith("dl:"):
            return False
        callback_id = callback.get("id")
        message = callback.get("message")
        if not isinstance(callback_id, str) or not isinstance(message, dict):
            return True
        chat = message.get("chat")
        message_id = message.get("message_id")
        if (
            not isinstance(chat, dict)
            or not isinstance(message_id, int)
            or not self.authorize(chat, [])
        ):
            return True
        chat_id = int(chat["id"])
        if self.torrent_service is None:
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="Download tracking is unavailable",
            )
            return True
        try:
            snapshots = self._downloads()
        except TorrentError:
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="qBittorrent is temporarily unavailable",
            )
            return True
        if data == "dl:list":
            self.api("answerCallbackQuery", callback_query_id=callback_id)
            text, keyboard = self._downloads_view(snapshots)
            self.edit(chat_id, message_id, text, reply_markup=keyboard)
            return True
        parts = data.split(":", 2)
        snapshot = (
            self._resolve_download(snapshots, parts[1]) if len(parts) == 3 else None
        )
        if snapshot is None or parts[2] not in {"show", "refresh", "toggle"}:
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="Download is no longer available",
            )
            return True
        if parts[2] == "toggle":
            if self.tracking_store is None:
                self.api(
                    "answerCallbackQuery",
                    callback_query_id=callback_id,
                    text="Notifications are unavailable",
                )
                return True
            try:
                enabled = self.tracking_store.toggle_notification(
                    snapshot,
                    chat_id=chat_id,
                    destination_label=self.torrent_service.destination_label(
                        snapshot.save_path
                    ),
                    now=self.wall_clock(),
                )
            except TrackingStateError:
                self.api(
                    "answerCallbackQuery",
                    callback_query_id=callback_id,
                    text="Could not update notification settings",
                )
                return True
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="Notifications " + ("enabled" if enabled else "disabled"),
            )
        else:
            self.api("answerCallbackQuery", callback_query_id=callback_id)
        text, keyboard = self._download_detail(snapshot)
        self.edit(chat_id, message_id, text, reply_markup=keyboard)
        return True

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        if self._handle_download_callback(callback):
            return
        callback_id = callback.get("id")
        data = callback.get("data")
        message = callback.get("message")
        if (
            not isinstance(callback_id, str)
            or not isinstance(data, str)
            or not isinstance(message, dict)
        ):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or not self.authorize(chat, []):
            return
        chat_id = int(chat["id"])
        parts = data.split(":", 2)
        pending = self._expire_pending(chat_id)
        if (
            len(parts) != 3
            or parts[0] != "torrent"
            or pending is None
            or pending.request_id != parts[1]
        ):
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="Request expired",
            )
            return
        choice = parts[2]
        if choice == "cancel":
            self.pending_torrents.pop(chat_id, None)
            self.api(
                "answerCallbackQuery", callback_query_id=callback_id, text="Cancelled"
            )
            self.send(chat_id, "Torrent request cancelled.")
            return
        if choice == "custom":
            self.api("answerCallbackQuery", callback_query_id=callback_id)
            prompt_id = self.send(
                chat_id,
                "Reply with an existing directory below an allowed media folder.",
                reply_markup={
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder": "/mnt/raid1/jellyfin/media/…",
                },
            )
            pending.custom_prompt_message_id = prompt_id
            return
        if self.torrent_service is None:
            raise RuntimeError("torrent service is unavailable")
        location = self.torrent_service.locations.by_id(choice)
        if location is None:
            self.api(
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text="Unknown destination",
            )
            return
        self.api(
            "answerCallbackQuery",
            callback_query_id=callback_id,
            text=f"Adding to {location.label}",
        )
        self._submit_torrent(chat_id, pending, location.path, location.label)

    def _handle_custom_path(
        self, message: dict[str, Any], chat: dict[str, Any], text: str
    ) -> bool:
        if self.torrent_service is None or not self.authorize(chat, []):
            return False
        chat_id = int(chat["id"])
        pending = self._expire_pending(chat_id)
        reply = message.get("reply_to_message")
        if (
            pending is None
            or pending.custom_prompt_message_id is None
            or not isinstance(reply, dict)
            or reply.get("message_id") != pending.custom_prompt_message_id
        ):
            return False
        try:
            save_path = self.torrent_service.validate_custom_path(text)
        except TorrentError as exc:
            self.pending_torrents.pop(chat_id, None)
            self.send(chat_id, "❌ " + html.escape(str(exc)))
            return True
        self._submit_torrent(chat_id, pending, save_path, "Custom")
        return True

    def _poll_tracking(self) -> None:
        if self.tracking_store is None or self.torrent_service is None:
            return
        chat_id = self.read_int(self.chat_file)
        try:
            snapshots = self._downloads()
            events, discovered = self.tracking_store.reconcile(
                snapshots,
                chat_id=chat_id,
                now=self.wall_clock(),
                destination_label=self.torrent_service.destination_label,
            )
        except (TorrentError, TrackingStateError):
            return
        for qbit_hash, tag in discovered:
            try:
                self.torrent_service.remove_tag(qbit_hash, tag)
            except TorrentError:
                pass
        for event in events:
            name = self._safe_torrent_name(event.snapshot.name)
            message_id = self.send(
                event.chat_id,
                "✅ <b>Download complete</b>\n\n"
                f"{html.escape(name)}\n"
                f"Size: {self._format_bytes(event.snapshot.size)}\n"
                f"Destination: {html.escape(event.destination_label)}",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "View downloads", "callback_data": "dl:list"}]
                    ]
                },
            )
            if message_id is not None:
                try:
                    self.tracking_store.mark_notified(
                        event.record_id,
                        completion_on=event.snapshot.completion_on,
                        now=self.wall_clock(),
                    )
                except TrackingStateError:
                    return

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        chat = message.get("chat")
        if not isinstance(text, str) or not isinstance(chat, dict):
            return
        if self._handle_custom_path(message, chat, text.strip()):
            return
        if not text.startswith("/"):
            return
        command_text, separator, argument = text.strip().partition(" ")
        command = command_text.split("@", 1)[0].lower()
        authorization_arguments = argument.split() if separator else []
        if not self.authorize(chat, authorization_arguments):
            return
        chat_id = int(chat["id"])
        if command == "/status":
            try:
                self.api("sendChatAction", chat_id=chat_id, action="typing")
                self.send(chat_id, self.status_builder())
            except Exception:
                self.send(
                    chat_id, "Status collection failed. Check the service journal."
                )
        elif command == "/torrent":
            self._start_torrent(chat_id, argument if separator else "")
        elif command == "/downloads":
            if self.torrent_service is None:
                self.send(chat_id, "Download tracking is not configured.")
                return
            try:
                text, keyboard = self._downloads_view(self._downloads())
            except TorrentError:
                self.send(chat_id, "qBittorrent progress is temporarily unavailable.")
                return
            self.send(chat_id, text, reply_markup=keyboard)
        elif command == "/cancel":
            if self.pending_torrents.pop(chat_id, None) is not None:
                self.send(chat_id, "Torrent request cancelled.")
            else:
                self.send(chat_id, "There is no pending torrent request.")
        elif command in {"/start", "/help"}:
            commands = "Send /status for one live infrastructure summary."
            if self.torrent_service is not None:
                commands += (
                    "\nSend <code>/torrent &lt;magnet&gt;</code> to add a torrent."
                    "\nSend /downloads to view progress and notification settings."
                )
            self.send(chat_id, commands)

    def run_forever(self) -> None:
        commands = [
            {"command": "status", "description": "Full infrastructure status"},
        ]
        if self.torrent_service is not None:
            commands.extend(
                [
                    {"command": "torrent", "description": "Add a qBittorrent magnet"},
                    {
                        "command": "downloads",
                        "description": "Download progress and notifications",
                    },
                    {
                        "command": "cancel",
                        "description": "Cancel a pending torrent request",
                    },
                ]
            )
        commands.append({"command": "help", "description": "Show available commands"})
        self.api(
            "setMyCommands",
            commands=json.dumps(commands),
        )
        if self.heartbeat_file is not None:
            self.heartbeat_file.touch()
        offset = self.read_int(self.offset_file)
        next_tracking_poll = 0.0
        while True:
            try:
                if self.tracking_store is not None and self.torrent_service is not None:
                    now = self.clock()
                    if now >= next_tracking_poll:
                        self._poll_tracking()
                        next_tracking_poll = (
                            now
                            + self.torrent_service.locations.tracking.poll_interval_seconds
                        )
                    timeout = max(1, min(25, int(next_tracking_poll - self.clock())))
                else:
                    timeout = 25
                result = self.api(
                    "getUpdates",
                    offset=offset,
                    timeout=timeout,
                    allowed_updates='["message","callback_query"]',
                )
                if self.heartbeat_file is not None:
                    self.heartbeat_file.touch()
                for update in result.get("result", []):
                    if not isinstance(update, dict):
                        continue
                    update_id = int(update.get("update_id", 0))
                    self.handle_update(update)
                    offset = max(offset, update_id + 1)
                    self.write_int(self.offset_file, offset)
            except (OSError, urllib.error.URLError, RuntimeError, ValueError):
                time.sleep(5)
