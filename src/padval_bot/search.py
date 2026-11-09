"""Bounded, on-demand search across Padval Bot's configured sources."""

from __future__ import annotations

import base64
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError
from .torrent import Magnet, TorrentError, validate_magnet


CATEGORIES = {"movies": "Movies", "tv": "TV", "audiobooks": "Audiobooks"}
TORLOCK_URL = "https://www.torlock.com/torznab/api"
KNABEN_URL = "https://api.knaben.org/v2/search"
JACKETT_SEARCH = "/api/v2.0/indexers/audiobookbay/results/torznab/api"
JACKETT_DOWNLOAD = "/dl/audiobookbay"
TV_CATEGORIES = ",".join(str(2000000 + 1000 * value) for value in range(9))
MAX_RESULTS = 24
MAX_RESPONSE = 1024 * 1024

# Keep the info hash and standard public trackers while excluding web seeds
# and arbitrary destinations supplied by the search results.
PUBLIC_TRACKERS = {
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
}
EPISODE = re.compile(r"\bs(\d{1,3})e(\d{1,3})(?!\d)", re.I)


class SearchError(ValueError):
    """An indexer returned an error or could not be reached."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    size: int
    seeders: int | None
    source: str
    magnet: Magnet | None = None
    jackett_path: str | None = None


def _nonnegative(value: object) -> int | None:
    if type(value) is int and 0 <= value < 10**18:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        return int(value)
    return None


def _search_magnet(value: str) -> Magnet:
    magnet = validate_magnet(value)
    parts = urllib.parse.parse_qsl(urllib.parse.urlsplit(magnet.value).query)
    safe = [
        (key, item)
        for key, item in parts
        if key.lower() in {"xt", "dn"} or (key == "tr" and item in PUBLIC_TRACKERS)
    ]
    return validate_magnet("magnet:?" + urllib.parse.urlencode(safe, safe=":"))


def _magnet_key(magnet: Magnet) -> str:
    digest = magnet.display_hash
    if len(digest) == 32:
        digest = base64.b32decode(digest).hex()
    return digest.lower()


class SearchService:
    def __init__(self, config: dict[str, Any]) -> None:
        # A loopback-only Jackett origin keeps its UI and API key off the LAN.
        self.jackett_base_url = config["jackett_base_url"].rstrip("/")
        if not re.fullmatch(
            r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", self.jackett_base_url
        ):
            raise ConfigError("Jackett URL must be a loopback HTTP origin")
        if int(self.jackett_base_url.rsplit(":", 1)[1]) > 65535:
            raise ConfigError("Jackett port is out of range")
        try:
            settings = json.loads(
                Path(config["jackett_config_file"]).read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError):
            raise ConfigError("Could not read the Jackett config file") from None
        key = settings.get("APIKey") if isinstance(settings, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", key):
            raise ConfigError("Jackett API key has an invalid format")
        self._jackett_key = key
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _request(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: int = 8,
        magnet_redirect: bool = False,
    ) -> bytes:
        request = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "Padval-Bot/1"},
        )
        try:
            # The URL is a fixed provider endpoint or the validated local Jackett origin.
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            with exc:
                location = exc.headers.get("Location", "")
                if (
                    magnet_redirect
                    and exc.code == 302
                    and location.startswith("magnet:?")
                ):
                    return location.encode("utf-8")
            raise SearchError("The indexer did not complete the request.") from None
        except (OSError, http.client.HTTPException):
            raise SearchError("The indexer is temporarily unreachable.") from None
        if magnet_redirect or len(payload) > MAX_RESPONSE:
            raise SearchError("The indexer returned an unexpected response.")
        return payload

    def _jackett_path(self, value: str) -> str | None:
        if len(value) > 8192:
            return None
        try:
            url = urllib.parse.urlsplit(value)
        except ValueError:
            return None
        if (
            f"{url.scheme}://{url.netloc}" != self.jackett_base_url
            or url.path.rstrip("/") != JACKETT_DOWNLOAD
            or url.fragment
        ):
            return None
        path = urllib.parse.parse_qs(url.query).get("path", [""])[0]
        return path if re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", path) else None

    def _torznab(self, payload: bytes, *, audiobooks: bool) -> list[SearchResult]:
        try:
            xml = payload.decode("utf-8-sig")
            if "<!DOCTYPE" in xml or "\x00" in xml:
                raise ValueError("Unsupported XML")
            root = ET.fromstring(xml)  # nosec B314
            channel = root.find("channel")
        except (ValueError, ET.ParseError):
            raise SearchError("The indexer returned invalid search data.") from None
        if channel is None or channel.find("error") is not None:
            raise SearchError("The indexer did not return search results.")
        results = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            attrs = {
                node.get("name"): node.get("value", "")
                for node in item
                if node.tag.endswith("}attr")
            }
            enclosure = item.find("enclosure")
            link = enclosure.get("url", "") if enclosure is not None else ""
            size = (
                _nonnegative(attrs.get("size"))
                or _nonnegative(
                    enclosure.get("length") if enclosure is not None else None
                )
                or 0
            )
            if audiobooks:
                path = self._jackett_path(link or item.findtext("link") or "")
                if path:
                    results.append(
                        SearchResult(
                            title, size, None, "AudioBook Bay", jackett_path=path
                        )
                    )
                continue
            try:
                magnet = _search_magnet(attrs.get("magneturl") or link)
            except TorrentError:
                continue
            results.append(
                SearchResult(
                    title,
                    size,
                    _nonnegative(attrs.get("seeders")),
                    "Torlock",
                    magnet=magnet,
                )
            )
        if not results and channel.find("item") is not None:
            raise SearchError("The indexer returned no usable release links.")
        return results

    def _knaben(self, payload: bytes) -> list[SearchResult]:
        try:
            data = json.loads(payload)
        except ValueError:
            raise SearchError("Knaben returned invalid search data.") from None
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise SearchError("Knaben did not return search results.")
        results = []
        for row in data["hits"]:
            if not isinstance(row, dict):
                continue
            title, link, tracker = (
                row.get(key) for key in ("title", "magnetUrl", "tracker")
            )
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(link, str)
            ):
                continue
            try:
                magnet = _search_magnet(link)
            except TorrentError:
                continue
            seeders = _nonnegative(row.get("liveSeeders"))
            if seeders is None:
                seeders = _nonnegative(row.get("seeders"))
            source = (
                f"Knaben / {tracker[:60]}"
                if isinstance(tracker, str) and tracker
                else "Knaben"
            )
            results.append(
                SearchResult(
                    title,
                    _nonnegative(row.get("bytes")) or 0,
                    seeders,
                    source,
                    magnet=magnet,
                )
            )
        if not results and data["hits"]:
            raise SearchError("Knaben returned no usable release links.")
        return results

    def search(self, category: str, query: str) -> tuple[SearchResult, ...]:
        if category == "movies":
            payload = self._request(
                TORLOCK_URL,
                {"t": "search", "cat": "2000", "q": query, "limit": str(MAX_RESULTS)},
            )
            results = self._torznab(payload, audiobooks=False)
        elif category == "tv":
            payload = self._request(
                KNABEN_URL,
                {"q": query, "c": TV_CATEGORIES, "o": "seeders", "s": str(MAX_RESULTS)},
            )
            results = self._knaben(payload)
            episode = EPISODE.search(query)
            if episode:
                expected = tuple(map(int, episode.groups()))
                results = [
                    result
                    for result in results
                    if any(
                        tuple(map(int, match.groups())) == expected
                        for match in EPISODE.finditer(result.title)
                    )
                ]
        elif category == "audiobooks":
            payload = self._request(
                self.jackett_base_url + JACKETT_SEARCH,
                {"apikey": self._jackett_key, "t": "search", "cat": "3030", "q": query},
                timeout=20,
            )
            results = self._torznab(payload, audiobooks=True)
        else:
            raise SearchError("Unknown search category.")

        words = set(re.findall(r"[^\W_]+", query.casefold()))
        results.sort(
            key=lambda row: (
                len(words.intersection(re.findall(r"[^\W_]+", row.title.casefold()))),
                row.seeders or 0,
            ),
            reverse=True,
        )
        unique = {}
        for result in results:
            key = _magnet_key(result.magnet) if result.magnet else result.jackett_path
            unique.setdefault(key, result)
        return tuple(unique.values())[:MAX_RESULTS]

    def resolve(self, result: SearchResult) -> Magnet:
        if result.magnet is not None:
            return result.magnet
        if not result.jackett_path:
            raise SearchError("This release has no usable magnet.")
        payload = self._request(
            self.jackett_base_url + JACKETT_DOWNLOAD,
            {
                "path": result.jackett_path,
                "jackett_apikey": self._jackett_key,
                "file": "release",
            },
            timeout=20,
            magnet_redirect=True,
        )
        try:
            return _search_magnet(payload.decode("utf-8"))
        except (UnicodeError, TorrentError):
            raise SearchError("This release has no usable magnet.") from None
