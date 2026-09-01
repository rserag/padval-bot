import tempfile
import unittest
from pathlib import Path
from unittest import mock

from padval_bot.torrent import (
    TorrentError,
    TorrentService,
    load_locations,
    validate_magnet,
)


LOCATIONS = """\
version: 1
torrent:
  save_locations:
    - id: movies
      label: Movies
      path: /mnt/raid1/jellyfin/media/movies
    - id: tv
      label: TV
      path: /mnt/raid1/jellyfin/media/tv
  custom_paths:
    enabled: true
    allowed_roots:
      - /mnt/raid1/jellyfin/media
  pending_request_ttl_seconds: 600
"""


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        del limit
        return b"Ok."


class TorrentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.locations_path = Path(self.directory.name) / "locations.yaml"
        self.locations_path.write_text(LOCATIONS, encoding="utf-8")

    def service(self):
        return TorrentService(
            {
                "base_url": "http://192.0.2.20:8080",
                "locations_file": str(self.locations_path),
                "tag": "padval-bot",
                "path_check_ssh": {
                    "host": "192.0.2.20",
                    "user": "data",
                    "identity_file": "/run/key",
                    "known_hosts_file": "/run/known_hosts",
                },
            }
        )

    def test_loads_locations_and_custom_policy(self):
        locations = load_locations(str(self.locations_path))
        self.assertEqual(
            [item.id for item in locations.save_locations], ["movies", "tv"]
        )
        self.assertEqual(locations.allowed_roots, ("/mnt/raid1/jellyfin/media",))
        self.assertEqual(locations.pending_ttl_seconds, 600)

    def test_rejects_broad_custom_root(self):
        self.locations_path.write_text(
            LOCATIONS.replace("/mnt/raid1/jellyfin/media", "/mnt/raid1"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(TorrentError, "specific application tree"):
            load_locations(str(self.locations_path))

    def test_validates_v1_magnet_without_echoing_it_in_errors(self):
        magnet = validate_magnet(
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Example"
        )
        self.assertEqual(
            magnet.display_hash, "0123456789ABCDEF0123456789ABCDEF01234567"
        )
        private_value = "magnet:?tr=https://tracker.example/private-passkey"
        with self.assertRaises(TorrentError) as raised:
            validate_magnet(private_value)
        self.assertNotIn("private-passkey", str(raised.exception))

    def test_custom_path_must_remain_under_allowed_root(self):
        service = self.service()
        self.assertEqual(
            service.validate_custom_path("/mnt/raid1/jellyfin/media/anime"),
            "/mnt/raid1/jellyfin/media/anime",
        )
        with self.assertRaisesRegex(TorrentError, "outside"):
            service.validate_custom_path("/mnt/raid1/pgsql/data")

    def test_submit_uses_fixed_api_and_selected_save_path(self):
        service = self.service()
        service.check_path = mock.Mock()
        magnet = validate_magnet(
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
        )
        with mock.patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            service.submit(magnet, "/mnt/raid1/jellyfin/media/movies")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.0.2.20:8080/api/v2/torrents/add")
        self.assertIn(b"/mnt/raid1/jellyfin/media/movies", request.data)
        self.assertIn(b"padval-bot", request.data)
        service.check_path.assert_called_once_with("/mnt/raid1/jellyfin/media/movies")


if __name__ == "__main__":
    unittest.main()
