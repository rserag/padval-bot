"""Small authenticated Jellyfin client for library refreshes."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any


class JellyfinError(RuntimeError):
    """Raised when Jellyfin cannot be reached or rejects a request."""


@dataclass(frozen=True, slots=True)
class LibraryScanStatus:
    """Safe subset of Jellyfin's Scan Media Library scheduled task."""

    state: str
    progress: float | None
    last_execution_status: str | None
    last_execution_end: str | None

    @property
    def active(self) -> bool:
        return self.state.casefold() in {"running", "cancelling"}


class JellyfinService:
    MAX_RESPONSE_BYTES = 256 * 1024

    def __init__(self, config: dict[str, Any]) -> None:
        parsed = urllib.parse.urlsplit(config["base_url"])
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise JellyfinError("Jellyfin base URL is invalid")
        self.base_url = config["base_url"].rstrip("/")
        self.timeout = int(config.get("timeout_seconds", 10))
        try:
            api_key = Path(config["api_key_file"]).read_text(encoding="ascii").strip()
        except OSError as exc:
            raise JellyfinError("cannot read the Jellyfin API key file") from exc
        if not re.fullmatch(r"[A-Fa-f0-9]{32,128}", api_key):
            raise JellyfinError("Jellyfin API key file has an invalid format")
        self._api_key = api_key

    def _request(self, path: str, *, method: str = "GET") -> bytes:
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={
                "User-Agent": "Padval-Bot/1",
                "X-Emby-Token": self._api_key,
            },
        )
        try:
            # The origin and path are fixed by validated configuration and source.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout
            ) as response:
                payload = response.read(self.MAX_RESPONSE_BYTES + 1)
        except (TimeoutError, urllib.error.URLError) as exc:
            raise JellyfinError("Jellyfin request failed") from exc
        if len(payload) > self.MAX_RESPONSE_BYTES:
            raise JellyfinError("Jellyfin response is too large")
        return payload

    def preflight(self) -> None:
        payload = self._request("/System/Info")
        try:
            info = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JellyfinError("Jellyfin returned invalid system information") from exc
        if not isinstance(info, dict) or not isinstance(info.get("Version"), str):
            raise JellyfinError("Jellyfin returned invalid system information")
        self.scan_status()

    def refresh_library(self) -> None:
        self._request("/Library/Refresh", method="POST")

    @staticmethod
    def _optional_short_string(value: object) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 80
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise JellyfinError("Jellyfin returned invalid scan status")
        return value

    def scan_status(self) -> LibraryScanStatus:
        payload = self._request("/ScheduledTasks")
        try:
            tasks = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JellyfinError("Jellyfin returned invalid scan status") from exc
        if not isinstance(tasks, list):
            raise JellyfinError("Jellyfin returned invalid scan status")
        task = next(
            (
                item
                for item in tasks
                if isinstance(item, dict)
                and (
                    item.get("Key") == "RefreshLibrary"
                    or item.get("Name") == "Scan Media Library"
                )
            ),
            None,
        )
        if task is None:
            raise JellyfinError("Jellyfin scan task was not found")
        state = self._optional_short_string(task.get("State"))
        if state is None:
            raise JellyfinError("Jellyfin returned invalid scan status")
        progress_value = task.get("CurrentProgressPercentage")
        if progress_value is None:
            progress = None
        elif (
            isinstance(progress_value, bool)
            or not isinstance(progress_value, (int, float))
            or not isfinite(progress_value)
            or not 0 <= progress_value <= 100
        ):
            raise JellyfinError("Jellyfin returned invalid scan status")
        else:
            progress = float(progress_value)
        execution = task.get("LastExecutionResult")
        if execution is None:
            execution_status = None
            execution_end = None
        elif not isinstance(execution, dict):
            raise JellyfinError("Jellyfin returned invalid scan status")
        else:
            execution_status = self._optional_short_string(execution.get("Status"))
            execution_end = self._optional_short_string(execution.get("EndTimeUtc"))
        return LibraryScanStatus(
            state=state,
            progress=progress,
            last_execution_status=execution_status,
            last_execution_end=execution_end,
        )
