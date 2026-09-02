"""Safe, one-chat qBittorrent magnet submission."""

from __future__ import annotations

import base64
import json
import re
import secrets
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml


class TorrentError(ValueError):
    """Raised when a torrent request is invalid or cannot be completed."""


class TorrentSubmissionUncertain(TorrentError):
    """Raised when qBittorrent may have accepted a timed-out request."""


@dataclass(frozen=True, slots=True)
class SaveLocation:
    id: str
    label: str
    path: str


@dataclass(frozen=True, slots=True)
class MediaRefreshSettings:
    enabled: bool = False
    debounce_seconds: int = 60
    retry_base_seconds: int = 300
    retry_max_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class TorrentTrackingSettings:
    enabled: bool = False
    poll_interval_seconds: int = 25
    auto_track_new: bool = True
    notify_on_complete: bool = True
    import_incomplete_tagged_on_start: bool = True
    completed_retention_hours: int = 72
    media_refresh: MediaRefreshSettings = field(default_factory=MediaRefreshSettings)


@dataclass(frozen=True, slots=True)
class TorrentLocations:
    save_locations: tuple[SaveLocation, ...]
    custom_enabled: bool
    allowed_roots: tuple[str, ...]
    pending_ttl_seconds: int
    tracking: TorrentTrackingSettings = field(default_factory=TorrentTrackingSettings)

    def by_id(self, location_id: str) -> SaveLocation | None:
        return next(
            (item for item in self.save_locations if item.id == location_id), None
        )


@dataclass(frozen=True, slots=True)
class Magnet:
    value: str
    display_hash: str


@dataclass(frozen=True, slots=True)
class TorrentSnapshot:
    qbit_hash: str
    name: str
    progress: float
    state: str
    download_speed: int
    eta: int
    amount_left: int
    size: int
    completion_on: int
    save_path: str
    tags: frozenset[str]

    @property
    def complete(self) -> bool:
        return self.completion_on > 0 or (
            self.size > 0 and self.amount_left == 0 and self.progress >= 1.0
        )


def _canonical_absolute_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise TorrentError(f"{field} must be an absolute path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TorrentError(f"{field} contains control characters")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) != value.rstrip("/"):
        raise TorrentError(f"{field} must be normalized and cannot contain traversal")
    return str(path)


def _within(path: str, root: str) -> bool:
    candidate = PurePosixPath(path)
    parent = PurePosixPath(root)
    return candidate == parent or parent in candidate.parents


def load_locations(path: str) -> TorrentLocations:
    try:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except OSError as exc:
        raise TorrentError("cannot read torrent locations configuration") from exc
    except yaml.YAMLError as exc:
        raise TorrentError("invalid torrent locations YAML") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise TorrentError("torrent locations configuration must use version 1")
    torrent = document.get("torrent")
    if not isinstance(torrent, dict):
        raise TorrentError("torrent locations configuration is missing torrent")

    custom = torrent.get("custom_paths", {})
    if not isinstance(custom, dict) or not isinstance(
        custom.get("enabled", False), bool
    ):
        raise TorrentError(
            "custom_paths must be an object with a boolean enabled value"
        )
    roots_value = custom.get("allowed_roots", [])
    if not isinstance(roots_value, list):
        raise TorrentError("custom_paths.allowed_roots must be an array")
    roots = tuple(
        _canonical_absolute_path(value, "custom path root") for value in roots_value
    )
    if custom.get("enabled") and not roots:
        raise TorrentError("custom paths require at least one allowed root")
    for root in roots:
        # Reject filesystem-wide or broad storage roots. A reviewed code change is
        # required before the bot can write outside a specific application tree.
        if len(PurePosixPath(root).parts) < 5:
            raise TorrentError(
                "custom path roots must identify a specific application tree"
            )

    rows = torrent.get("save_locations")
    if not isinstance(rows, list) or not rows:
        raise TorrentError("at least one save location is required")
    locations: list[SaveLocation] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TorrentError(f"save location {index} must be an object")
        location_id = row.get("id")
        label = row.get("label")
        if not isinstance(location_id, str) or not re.fullmatch(
            r"[a-z0-9_-]{1,24}", location_id
        ):
            raise TorrentError(f"save location {index} has an invalid id")
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= 32
            or any(ord(character) < 32 for character in label)
        ):
            raise TorrentError(f"save location {index} has an invalid label")
        location_path = _canonical_absolute_path(
            row.get("path"), f"save location {location_id}"
        )
        if roots and not any(_within(location_path, root) for root in roots):
            raise TorrentError(
                f"save location {location_id} is outside the allowed roots"
            )
        if location_id in seen_ids or location_path in seen_paths:
            raise TorrentError("save location ids and paths must be unique")
        seen_ids.add(location_id)
        seen_paths.add(location_path)
        locations.append(SaveLocation(location_id, label, location_path))

    ttl = torrent.get("pending_request_ttl_seconds", 600)
    if not isinstance(ttl, int) or not 60 <= ttl <= 3600:
        raise TorrentError("pending_request_ttl_seconds must be between 60 and 3600")
    tracking_value = torrent.get("tracking", {})
    if not isinstance(tracking_value, dict):
        raise TorrentError("tracking must be an object")
    boolean_fields = {
        "enabled": False,
        "auto_track_new": True,
        "notify_on_complete": True,
        "import_incomplete_tagged_on_start": True,
    }
    booleans: dict[str, bool] = {}
    for name, default in boolean_fields.items():
        item = tracking_value.get(name, default)
        if not isinstance(item, bool):
            raise TorrentError(f"tracking.{name} must be boolean")
        booleans[name] = item
    poll_interval = tracking_value.get("poll_interval_seconds", 25)
    if not isinstance(poll_interval, int) or not 10 <= poll_interval <= 300:
        raise TorrentError("tracking.poll_interval_seconds must be between 10 and 300")
    retention = tracking_value.get("completed_retention_hours", 72)
    if not isinstance(retention, int) or not 1 <= retention <= 720:
        raise TorrentError(
            "tracking.completed_retention_hours must be between 1 and 720"
        )
    refresh_value = tracking_value.get("media_refresh", {})
    if not isinstance(refresh_value, dict):
        raise TorrentError("tracking.media_refresh must be an object")
    refresh_enabled = refresh_value.get("enabled", False)
    if not isinstance(refresh_enabled, bool):
        raise TorrentError("tracking.media_refresh.enabled must be boolean")
    debounce = refresh_value.get("debounce_seconds", 60)
    if not isinstance(debounce, int) or not 0 <= debounce <= 900:
        raise TorrentError(
            "tracking.media_refresh.debounce_seconds must be between 0 and 900"
        )
    retry_base = refresh_value.get("retry_base_seconds", 300)
    if not isinstance(retry_base, int) or not 30 <= retry_base <= 3600:
        raise TorrentError(
            "tracking.media_refresh.retry_base_seconds must be between 30 and 3600"
        )
    retry_max = refresh_value.get("retry_max_seconds", 3600)
    if not isinstance(retry_max, int) or not retry_base <= retry_max <= 86400:
        raise TorrentError(
            "tracking.media_refresh.retry_max_seconds must be between the base retry and 86400"
        )
    media_refresh = MediaRefreshSettings(
        enabled=refresh_enabled,
        debounce_seconds=debounce,
        retry_base_seconds=retry_base,
        retry_max_seconds=retry_max,
    )
    tracking = TorrentTrackingSettings(
        enabled=booleans["enabled"],
        poll_interval_seconds=poll_interval,
        auto_track_new=booleans["auto_track_new"],
        notify_on_complete=booleans["notify_on_complete"],
        import_incomplete_tagged_on_start=booleans["import_incomplete_tagged_on_start"],
        completed_retention_hours=retention,
        media_refresh=media_refresh,
    )
    return TorrentLocations(
        tuple(locations), bool(custom.get("enabled")), roots, ttl, tracking
    )


def validate_magnet(value: str) -> Magnet:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    elif value.startswith(("'", '"')) or value.endswith(("'", '"')):
        raise TorrentError("Send one valid magnet link.")
    if (
        not value
        or len(value) > 8192
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise TorrentError("Send one valid magnet link.")
    prefix, separator, query = value.partition("?")
    if prefix.lower() != "magnet:" or not separator or not query:
        raise TorrentError("Only magnet links are supported.")
    if re.search(r"%(?![0-9A-Fa-f]{2})", query):
        raise TorrentError("Send one valid magnet link.")
    normalized_query = urllib.parse.quote(
        query,
        safe="!$&'()*+,-./:;=?@_~%[]",
    )
    value = "magnet:?" + normalized_query
    if len(value) > 8192:
        raise TorrentError("Send one valid magnet link.")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "magnet" or not parsed.query:
        raise TorrentError("Only magnet links are supported.")
    hashes: list[str] = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() != "xt":
            continue
        lowered = item.lower()
        if lowered.startswith("urn:btih:"):
            digest = item[9:]
            if re.fullmatch(r"[0-9A-Fa-f]{40}|[A-Za-z2-7]{32}", digest):
                hashes.append(digest.upper())
        elif lowered.startswith("urn:btmh:"):
            digest = item[9:]
            if re.fullmatch(r"[0-9A-Fa-f]{68,128}", digest):
                hashes.append(digest.lower())
    if not hashes:
        raise TorrentError("The magnet link has no supported BitTorrent info hash.")
    return Magnet(value=value, display_hash=hashes[0])


PATH_CHECK_SCRIPT = r"""
import json
import os
import sys

request = json.load(sys.stdin)
path = os.path.realpath(request["path"])
roots = [os.path.realpath(root) for root in request["roots"]]
inside = any(os.path.commonpath([path, root]) == root for root in roots)
result = {
    "inside": inside,
    "directory": os.path.isdir(path),
    "writable": os.access(path, os.W_OK | os.X_OK),
}
print(json.dumps(result, separators=(",", ":")))
"""


class TorrentService:
    MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.locations = load_locations(config["locations_file"])
        base = urllib.parse.urlsplit(config["base_url"])
        if (
            base.scheme not in {"http", "https"}
            or not base.hostname
            or base.username
            or base.password
        ):
            raise TorrentError("qBittorrent base URL is invalid")
        if base.path not in {"", "/"} or base.query or base.fragment:
            raise TorrentError(
                "qBittorrent base URL cannot contain a path, query, or fragment"
            )
        self.base_url = config["base_url"].rstrip("/")
        self.tag = str(config.get("tag", "padval-bot"))

    def validate_magnet(self, value: str) -> Magnet:
        return validate_magnet(value)

    def validate_custom_path(self, value: str) -> str:
        path = _canonical_absolute_path(value.strip(), "custom path")
        if not self.locations.custom_enabled:
            raise TorrentError("Custom paths are disabled.")
        if not any(_within(path, root) for root in self.locations.allowed_roots):
            raise TorrentError("That path is outside the configured media folders.")
        return path

    def _ssh_command(self) -> list[str]:
        ssh = self.config["path_check_ssh"]
        encoded = base64.b64encode(PATH_CHECK_SCRIPT.encode("utf-8")).decode("ascii")
        remote = f"python3 -c {shlex.quote(f'import base64;exec(base64.b64decode({encoded!r}))')}"
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={ssh['known_hosts_file']}",
            "-i",
            ssh["identity_file"],
            f"{ssh['user']}@{ssh['host']}",
            remote,
        ]

    def check_path(self, path: str) -> None:
        request = json.dumps(
            {"path": path, "roots": list(self.locations.allowed_roots)},
            separators=(",", ":"),
        )
        try:
            process = subprocess.run(
                self._ssh_command(),
                input=request,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TorrentError(
                "Could not validate the destination on the storage server."
            ) from exc
        try:
            result = json.loads(process.stdout) if process.returncode == 0 else {}
        except json.JSONDecodeError:
            result = {}
        if not result.get("inside"):
            raise TorrentError(
                "The destination resolves outside the configured media folders."
            )
        if not result.get("directory"):
            raise TorrentError("The destination directory does not exist.")
        if not result.get("writable"):
            raise TorrentError("qBittorrent cannot write to that destination.")

    def preflight(self) -> None:
        for location in self.locations.save_locations:
            self.check_path(location.path)

    @staticmethod
    def _validate_tag(tag: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", tag):
            raise TorrentError("qBittorrent tag is invalid")
        return tag

    @staticmethod
    def _snapshot(row: object) -> TorrentSnapshot:
        if not isinstance(row, dict):
            raise TorrentError("qBittorrent returned invalid torrent data")
        qbit_hash = row.get("hash")
        if not isinstance(qbit_hash, str) or not re.fullmatch(
            r"[0-9A-Fa-f]{40,64}", qbit_hash
        ):
            raise TorrentError("qBittorrent returned an invalid torrent hash")

        def integer(name: str) -> int:
            value = row.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TorrentError("qBittorrent returned invalid torrent data")
            return max(0, int(value))

        progress = row.get("progress", 0.0)
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            raise TorrentError("qBittorrent returned invalid torrent data")
        name = row.get("name", "Unnamed torrent")
        state = row.get("state", "unknown")
        save_path = row.get("save_path", "")
        tags_value = row.get("tags", "")
        if not all(
            isinstance(item, str) for item in (name, state, save_path, tags_value)
        ):
            raise TorrentError("qBittorrent returned invalid torrent data")
        tags = frozenset(item.strip() for item in tags_value.split(",") if item.strip())
        return TorrentSnapshot(
            qbit_hash=qbit_hash.lower(),
            name=name[:512],
            progress=min(1.0, max(0.0, float(progress))),
            state=state[:64],
            download_speed=integer("dlspeed"),
            eta=integer("eta"),
            amount_left=integer("amount_left"),
            size=integer("size"),
            completion_on=integer("completion_on"),
            save_path=save_path[:4096],
            tags=tags,
        )

    def _json_get(self, path: str, parameters: dict[str, str]) -> object:
        query = urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            self.base_url + path + ("?" + query if query else ""),
            headers={"User-Agent": "Padval-Bot/1"},
        )
        try:
            # The origin and path are fixed by validated configuration and source.
            with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
                payload = response.read(self.MAX_API_RESPONSE_BYTES + 1)
        except (TimeoutError, urllib.error.URLError) as exc:
            raise TorrentError(
                "Could not read download progress from qBittorrent."
            ) from exc
        if len(payload) > self.MAX_API_RESPONSE_BYTES:
            raise TorrentError("qBittorrent progress response is too large.")
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TorrentError("qBittorrent returned invalid progress data.") from exc

    def list_torrents(
        self, *, tag: str | None = None, hashes: tuple[str, ...] = ()
    ) -> tuple[TorrentSnapshot, ...]:
        parameters: dict[str, str] = {}
        if tag is not None:
            parameters["tag"] = self._validate_tag(tag)
        if hashes:
            normalized: list[str] = []
            for value in hashes:
                if not re.fullmatch(r"[0-9A-Fa-f]{40,64}", value):
                    raise TorrentError("torrent hash is invalid")
                normalized.append(value.lower())
            parameters["hashes"] = "|".join(normalized)
        payload = self._json_get("/api/v2/torrents/info", parameters)
        if not isinstance(payload, list) or len(payload) > 1000:
            raise TorrentError("qBittorrent returned invalid progress data.")
        return tuple(self._snapshot(row) for row in payload)

    def remove_tag(self, qbit_hash: str, tag: str) -> None:
        if not re.fullmatch(r"[0-9A-Fa-f]{40,64}", qbit_hash):
            raise TorrentError("torrent hash is invalid")
        tag = self._validate_tag(tag)
        body = urllib.parse.urlencode(
            {"hashes": qbit_hash.lower(), "tags": tag}
        ).encode("ascii")
        request = urllib.request.Request(
            self.base_url + "/api/v2/torrents/removeTags",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Padval-Bot/1",
            },
            method="POST",
        )
        try:
            # The origin and endpoint are fixed; only validated hash/tag values vary.
            with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
                response.read(128)
                if response.status != 200:
                    raise TorrentError("qBittorrent could not remove a tracking tag.")
        except (TimeoutError, urllib.error.URLError) as exc:
            raise TorrentError("qBittorrent could not remove a tracking tag.") from exc

    def destination_label(self, save_path: str) -> str:
        location = next(
            (item for item in self.locations.save_locations if item.path == save_path),
            None,
        )
        return location.label if location is not None else "Custom"

    @staticmethod
    def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
        boundary = "----------------padvalbot" + secrets.token_hex(12)
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                        "ascii"
                    ),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks), boundary

    def submit(
        self, magnet: Magnet, save_path: str, *, tracking_tag: str | None = None
    ) -> None:
        self.check_path(save_path)
        tags = [self.tag]
        if tracking_tag is not None:
            tags.append(self._validate_tag(tracking_tag))
        body, boundary = self._multipart(
            {
                "urls": magnet.value,
                "savepath": save_path,
                "tags": ",".join(tags),
                "autoTMM": "false",
            }
        )
        request = urllib.request.Request(
            self.base_url + "/api/v2/torrents/add",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Padval-Bot/1",
            },
            method="POST",
        )
        try:
            # The base URL was restricted to a configured HTTP(S) qBittorrent origin.
            with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
                reply = response.read(128).decode("utf-8", "replace").strip()
                if response.status != 200 or reply.lower().startswith("fails"):
                    raise TorrentError("qBittorrent rejected the magnet link.")
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise TorrentError("qBittorrent denied the request.") from exc
            raise TorrentError("qBittorrent rejected the request.") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise TorrentSubmissionUncertain(
                "qBittorrent may have accepted the request; check the dashboard before retrying."
            ) from exc
