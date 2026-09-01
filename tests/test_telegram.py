import os
import tempfile
import unittest
from pathlib import Path

from padval_bot.telegram import TelegramBot
from padval_bot.torrent import Magnet, SaveLocation, TorrentLocations


class FakeTorrentService:
    def __init__(self):
        self.locations = TorrentLocations(
            (
                SaveLocation("movies", "Movies", "/mnt/raid1/jellyfin/media/movies"),
                SaveLocation("tv", "TV", "/mnt/raid1/jellyfin/media/tv"),
            ),
            True,
            ("/mnt/raid1/jellyfin/media",),
            600,
        )
        self.submissions = []

    def validate_magnet(self, value):
        if not value.startswith("magnet:?"):
            raise ValueError("invalid")
        return Magnet(value, "0123456789ABCDEF0123456789ABCDEF01234567")

    def validate_custom_path(self, value):
        if not value.startswith("/mnt/raid1/jellyfin/media/"):
            raise ValueError("outside")
        return value

    def submit(self, magnet, path):
        self.submissions.append((magnet.value, path))


class TelegramAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.token = root / "token"
        self.token.write_text("123456:ExampleTokenValue", encoding="ascii")
        self.chat = root / "allowed_chat_id"
        self.secret = root / "pairing_secret"
        self.secret.write_text("correct-horse-battery-staple", encoding="ascii")
        self.bot = TelegramBot(
            {
                "token_file": str(self.token),
                "allowed_chat_id_file": str(self.chat),
                "pairing_secret_file": str(self.secret),
                "state_dir": str(root / "state"),
            },
            lambda: "ok",
        )

    def test_pairing_binds_one_private_chat(self):
        self.assertTrue(
            self.bot.authorize(
                {"id": 42, "type": "private"}, ["correct-horse-battery-staple"]
            )
        )
        self.assertEqual(self.chat.read_text(encoding="ascii").strip(), "42")
        self.assertEqual(os.stat(self.chat).st_mode & 0o777, 0o600)
        self.assertFalse(self.bot.authorize({"id": 43, "type": "private"}, []))

    def test_wrong_pairing_secret_is_ignored(self):
        self.assertFalse(self.bot.authorize({"id": 42, "type": "private"}, ["wrong"]))
        self.assertFalse(self.chat.exists())

    def test_send_uses_telegram_html(self):
        calls = []
        self.bot.api = lambda method, **params: calls.append((method, params)) or {
            "ok": True
        }
        self.bot.send(42, "<b>healthy</b>")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")

    def test_torrent_command_uses_destination_buttons_without_echoing_magnet(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService()
        bot = TelegramBot(self.bot.config, lambda: "ok", service)
        calls = []
        bot.api = lambda method, **params: calls.append((method, params)) or {
            "ok": True,
            "result": {"message_id": 10},
        }
        private_magnet = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&tr=https://tracker.example/private-passkey"
        )
        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": f"/torrent {private_magnet}",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        send = calls[-1]
        self.assertEqual(send[0], "sendMessage")
        self.assertNotIn("private-passkey", send[1]["text"])
        self.assertNotIn("private-passkey", send[1]["reply_markup"])
        self.assertIn("Movies", send[1]["reply_markup"])
        self.assertIn("Custom", send[1]["reply_markup"])

    def test_preset_callback_submits_selected_path(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService()
        bot = TelegramBot(self.bot.config, lambda: "ok", service, clock=lambda: 100)
        bot.api = lambda method, **params: {
            "ok": True,
            "result": {"message_id": 10},
        }
        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/torrent magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        pending = bot.pending_torrents[42]
        bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-1",
                    "data": f"torrent:{pending.request_id}:movies",
                    "message": {"chat": {"id": 42, "type": "private"}},
                }
            }
        )
        self.assertEqual(
            service.submissions[0][1],
            "/mnt/raid1/jellyfin/media/movies",
        )
        self.assertNotIn(42, bot.pending_torrents)

    def test_custom_path_must_reply_to_force_reply_prompt(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService()
        bot = TelegramBot(self.bot.config, lambda: "ok", service, clock=lambda: 100)
        next_message_id = iter([20, 21, 22])

        def api(method, **params):
            del params
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": next(next_message_id)}}
            return {"ok": True, "result": {}}

        bot.api = api
        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/torrent magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        pending = bot.pending_torrents[42]
        bot.handle_update(
            {
                "callback_query": {
                    "id": "callback-2",
                    "data": f"torrent:{pending.request_id}:custom",
                    "message": {"chat": {"id": 42, "type": "private"}},
                }
            }
        )
        prompt = bot.pending_torrents[42].custom_prompt_message_id
        bot.handle_update(
            {
                "message": {
                    "message_id": 3,
                    "text": "/mnt/raid1/jellyfin/media/anime",
                    "chat": {"id": 42, "type": "private"},
                    "reply_to_message": {"message_id": prompt},
                }
            }
        )
        self.assertEqual(service.submissions[0][1], "/mnt/raid1/jellyfin/media/anime")
