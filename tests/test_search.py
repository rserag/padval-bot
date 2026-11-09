import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from padval_bot.config import ConfigError
from padval_bot.search import (
    JACKETT_DOWNLOAD,
    KNABEN_URL,
    TORLOCK_URL,
    SearchError,
    SearchService,
)


INFO_HASH = "ef" * 20
MAGNET = f"magnet:?xt=urn:btih:{INFO_HASH}"


class SearchServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = Path(temp.name) / "ServerConfig.json"
        settings.write_text(json.dumps({"APIKey": "ExampleTestKey123"}))
        self.config = {
            "jackett_base_url": "http://127.0.0.1:9117",
            "jackett_config_file": str(settings),
        }
        self.service = SearchService(self.config)

    def test_torlock_filters_and_deduplicates_magnets(self):
        base32 = base64.b32encode(bytes.fromhex(INFO_HASH)).decode()
        payload = f"""<rss xmlns:t="urn:torznab"><channel>
          <item><title>Film 2020 720p</title><enclosure length="456"
              url="magnet:?xt=urn:btih:{base32}"/><t:attr name="seeders" value="1"/></item>
          <item><title>Film 2020 1080p</title><enclosure length="123"
              url="{MAGNET}&amp;xs=http%3A%2F%2F127.0.0.1%2Fmeta&amp;tr=http%3A%2F%2F127.0.0.1%2Fannounce"/>
              <t:attr name="seeders" value="4"/></item>
        </channel></rss>""".encode()
        with patch.object(self.service, "_request", return_value=payload) as request:
            results = self.service.search("movies", "Film 2020")
        self.assertEqual(request.call_args.args[0], TORLOCK_URL)
        self.assertEqual(request.call_args.args[1]["cat"], "2000")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Film 2020 1080p")
        self.assertEqual(results[0].size, 123)
        self.assertEqual(results[0].magnet.value, MAGNET)

    def test_knaben_only_returns_requested_episode_and_uses_live_seeds(self):
        payload = json.dumps(
            {
                "hits": [
                    {"title": "Show S03E10", "magnetUrl": MAGNET, "seeders": 500},
                    {
                        "title": "Show S03E01",
                        "magnetUrl": MAGNET,
                        "tracker": "Example tracker",
                        "liveSeeders": 0,
                        "seeders": 5,
                        "bytes": 12345,
                    },
                ]
            }
        ).encode()
        with patch.object(self.service, "_request", return_value=payload) as request:
            results = self.service.search("tv", "Show S3E1")
        self.assertEqual(request.call_args.args[0], KNABEN_URL)
        self.assertIn("2001000", request.call_args.args[1]["c"])
        self.assertEqual([row.title for row in results], ["Show S03E01"])
        self.assertEqual(results[0].seeders, 0)
        self.assertEqual(results[0].source, "Knaben / Example tracker")

    def test_jackett_only_resolves_the_selected_local_result(self):
        local = self.config["jackett_base_url"] + JACKETT_DOWNLOAD
        payload = f"""<rss xmlns:t="urn:torznab"><channel>
          <item><title>Book</title><enclosure url="{local}/?path=opaque_id&amp;jackett_apikey=old"/>
            <t:attr name="seeders" value="1"/></item>
          <item><title>External link</title><enclosure
            url="https://example.org/dl/audiobookbay/?path=opaque_id"/></item>
        </channel></rss>""".encode()
        with patch.object(
            self.service, "_request", side_effect=[payload, MAGNET.encode()]
        ) as request:
            results = self.service.search("audiobooks", "Book")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(len(results), 1)
            self.assertIsNone(results[0].seeders)
            self.assertEqual(self.service.resolve(results[0]).value, MAGNET)
        self.assertEqual(request.call_args.args[0], local)
        self.assertEqual(request.call_args.args[1]["path"], "opaque_id")
        self.assertEqual(
            request.call_args.args[1]["jackett_apikey"], "ExampleTestKey123"
        )
        self.assertTrue(request.call_args.kwargs["magnet_redirect"])

    def test_indexer_error_differs_from_empty_results(self):
        with patch.object(
            self.service, "_request", return_value=b"<error code='100' />"
        ):
            with self.assertRaises(SearchError):
                self.service.search("movies", "Film")
        with patch.object(
            self.service, "_request", return_value=b"<rss><channel /></rss>"
        ):
            self.assertEqual(self.service.search("movies", "Film"), ())

    def test_rejects_xml_with_entities(self):
        with patch.object(
            self.service,
            "_request",
            return_value=b"<!DOCTYPE rss [<!ENTITY a 'x'>]><rss><channel/></rss>",
        ):
            with self.assertRaises(SearchError):
                self.service.search("movies", "Film")

    def test_jackett_must_be_loopback(self):
        self.config["jackett_base_url"] = "http://127.0.0.1:9117@example.org"
        with self.assertRaises(ConfigError):
            SearchService(self.config)
