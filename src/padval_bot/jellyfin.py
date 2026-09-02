"""Small authenticated Jellyfin client for library refreshes."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class JellyfinError(RuntimeError):
    """Raised when Jellyfin cannot be reached or rejects a request."""


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

    def refresh_library(self) -> None:
        self._request("/Library/Refresh", method="POST")
