import os
import tempfile
import unittest
from pathlib import Path

from padval_bot.jellyfin import JellyfinError, LibraryScanStatus
from padval_bot.telegram import TelegramBot
from padval_bot.torrent import (
    Magnet,
    MediaRefreshSettings,
    SaveLocation,
    TorrentLocations,
    TorrentSnapshot,
    TorrentTrackingSettings,
)


class FakeTorrentService:
    def __init__(self, *, tracking=False, media_refresh=False):
        self.locations = TorrentLocations(
            (
                SaveLocation("movies", "Movies", "/mnt/raid1/jellyfin/media/movies"),
                SaveLocation("tv", "TV", "/mnt/raid1/jellyfin/media/tv"),
            ),
            True,
            ("/mnt/raid1/jellyfin/media",),
            600,
            TorrentTrackingSettings(
                enabled=tracking,
                media_refresh=MediaRefreshSettings(enabled=media_refresh),
            ),
        )
        self.submissions = []
        self.snapshots = ()
        self.tag = "padval-bot"
        self.removed_tags = []

    def validate_magnet(self, value):
        if not value.startswith("magnet:?"):
            raise ValueError("invalid")
        return Magnet(value, "0123456789ABCDEF0123456789ABCDEF01234567")

    def validate_custom_path(self, value):
        if not value.startswith("/mnt/raid1/jellyfin/media/"):
            raise ValueError("outside")
        return value

    def submit(self, magnet, path, *, tracking_tag=None):
        self.submissions.append((magnet.value, path))
        self.tracking_tag = tracking_tag

    def list_torrents(self, *, tag=None, hashes=()):
        del tag, hashes
        return self.snapshots

    def destination_label(self, path):
        return "Movies" if path.endswith("/movies") else "Custom"

    def remove_tag(self, qbit_hash, tag):
        self.removed_tags.append((qbit_hash, tag))


class FakeJellyfinService:
    def __init__(self, *, fail=False, statuses=()):
        self.refreshes = 0
        self.fail = fail
        self.statuses = list(statuses)

    def refresh_library(self):
        self.refreshes += 1
        if self.fail:
            raise JellyfinError("unavailable")

    def scan_status(self):
        if self.statuses:
            return self.statuses.pop(0)
        if self.refreshes:
            return LibraryScanStatus("Running", None, "Completed", "previous")
        return LibraryScanStatus("Idle", None, "Completed", "previous")


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
        self.bot.api = lambda method, **params: (
            calls.append((method, params)) or {"ok": True}
        )
        self.bot.send(42, "<b>healthy</b>")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")

    def test_manual_scan_is_authorized_and_registered(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService()
        bot = TelegramBot(self.bot.config, lambda: "ok", None, jellyfin)
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": 1},
            }
        )

        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/scan",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        bot.handle_update(
            {
                "message": {
                    "message_id": 2,
                    "text": "/scan",
                    "chat": {"id": 43, "type": "private"},
                }
            }
        )

        self.assertEqual(jellyfin.refreshes, 1)
        self.assertIn("Jellyfin library scan", calls[-1][1]["text"])
        self.assertIn("Refresh status", calls[-1][1]["reply_markup"])
        self.assertIn("scan", [item["command"] for item in bot._bot_commands()])
        self.assertIn("scanstatus", [item["command"] for item in bot._bot_commands()])

    def test_manual_scan_failure_is_reported(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService(fail=True)
        bot = TelegramBot(self.bot.config, lambda: "ok", None, jellyfin)
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": 1},
            }
        )

        with self.assertLogs("padval_bot.telegram", level="WARNING"):
            bot.handle_update(
                {
                    "message": {
                        "message_id": 1,
                        "text": "/scan",
                        "chat": {"id": 42, "type": "private"},
                    }
                }
            )

        self.assertEqual(jellyfin.refreshes, 1)
        self.assertIn("could not be started", calls[-1][1]["text"])

    def test_scan_progress_edits_one_message_and_notifies_on_completion(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService(
            statuses=(
                LibraryScanStatus("Idle", None, "Completed", "old-end"),
                LibraryScanStatus("Running", 12.5, "Completed", "old-end"),
                LibraryScanStatus("Running", 73.2, "Completed", "old-end"),
                LibraryScanStatus("Idle", None, "Completed", "new-end"),
            )
        )
        now = [100.0]
        bot = TelegramBot(
            self.bot.config,
            lambda: "ok",
            None,
            jellyfin,
            wall_clock=lambda: now[0],
        )
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": 77},
            }
        )

        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/scan",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        now[0] = 110
        bot._update_scan_tracking()
        now[0] = 120
        bot._update_scan_tracking()

        edits = [params for method, params in calls if method == "editMessageText"]
        self.assertEqual({params["message_id"] for params in edits}, {77})
        self.assertIn("73.2%", edits[-2]["text"])
        self.assertIn("scan completed", edits[-1]["text"])
        self.assertIsNone(bot.scan_tracking_store.record)
        self.assertFalse(
            (Path(self.directory.name) / "state/jellyfin-scan-tracking.json").exists()
        )

    def test_scanstatus_follows_scan_started_elsewhere(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService(
            statuses=(LibraryScanStatus("Running", 48.0, "Completed", "old-end"),)
        )
        bot = TelegramBot(self.bot.config, lambda: "ok", None, jellyfin)
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": 88},
            }
        )

        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/scanstatus",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )

        self.assertIn("48.0%", calls[-1][1]["text"])
        self.assertEqual(bot.scan_tracking_store.record.message_id, 88)

    def test_scan_refresh_button_updates_the_tracked_message(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService(
            statuses=(LibraryScanStatus("Running", 62.0, "Completed", "old-end"),)
        )
        bot = TelegramBot(self.bot.config, lambda: "ok", None, jellyfin)
        bot.scan_tracking_store.start(
            chat_id=42,
            message_id=88,
            requested_at=100,
            triggered_by_bot=True,
            baseline_last_execution_end="old-end",
            observed_running=True,
        )
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params)) or {"ok": True, "result": {}}
        )

        bot.handle_update(
            {
                "callback_query": {
                    "id": "scan-refresh-1",
                    "data": "jfscan:refresh",
                    "message": {
                        "message_id": 88,
                        "chat": {"id": 42, "type": "private"},
                    },
                }
            }
        )

        edit = next(params for method, params in calls if method == "editMessageText")
        self.assertEqual(edit["message_id"], 88)
        self.assertIn("62.0%", edit["text"])
        self.assertTrue(any(method == "answerCallbackQuery" for method, _ in calls))

    def test_fast_scan_is_detected_from_new_execution_result(self):
        self.chat.write_text("42\n", encoding="ascii")
        jellyfin = FakeJellyfinService(
            statuses=(
                LibraryScanStatus("Idle", None, "Completed", "old-end"),
                LibraryScanStatus("Idle", None, "Completed", "new-end"),
            )
        )
        bot = TelegramBot(self.bot.config, lambda: "ok", None, jellyfin)
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params)) or {"ok": True, "result": {"message_id": 91}}
        )

        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/scan",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )

        edits = [params for method, params in calls if method == "editMessageText"]
        self.assertIn("scan completed", edits[-1]["text"])
        self.assertIsNone(bot.scan_tracking_store.record)

    def test_torrent_command_uses_destination_buttons_without_echoing_magnet(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService()
        bot = TelegramBot(self.bot.config, lambda: "ok", service)
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": 10},
            }
        )
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

    def test_download_progress_and_completion_notification(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService(tracking=True)
        active = TorrentSnapshot(
            qbit_hash="0123456789abcdef0123456789abcdef01234567",
            name="Example Movie",
            progress=0.5,
            state="downloading",
            download_speed=1048576,
            eta=120,
            amount_left=50,
            size=100,
            completion_on=0,
            save_path="/mnt/raid1/jellyfin/media/movies",
            tags=frozenset({"padval-bot"}),
        )
        service.snapshots = (active,)
        bot = TelegramBot(
            self.bot.config,
            lambda: "ok",
            service,
            wall_clock=lambda: 100,
        )
        calls = []
        bot.api = lambda method, **params: (
            calls.append((method, params))
            or {
                "ok": True,
                "result": {"message_id": len(calls)},
            }
        )
        bot.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "text": "/downloads",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
        self.assertIn("50.0%", calls[-1][1]["text"])
        self.assertIn("Example Movie", calls[-1][1]["text"])

        bot._poll_tracking()
        service.snapshots = (
            TorrentSnapshot(
                qbit_hash=active.qbit_hash,
                name=active.name,
                progress=1.0,
                state="uploading",
                download_speed=0,
                eta=0,
                amount_left=0,
                size=100,
                completion_on=200,
                save_path=active.save_path,
                tags=active.tags,
            ),
        )
        bot._poll_tracking()
        bot._poll_tracking()
        completion_messages = [
            params["text"]
            for method, params in calls
            if method == "sendMessage" and "Download complete" in params["text"]
        ]
        self.assertEqual(len(completion_messages), 1)

    def test_completed_download_triggers_one_debounced_jellyfin_refresh(self):
        self.chat.write_text("42\n", encoding="ascii")
        service = FakeTorrentService(tracking=True, media_refresh=True)
        jellyfin = FakeJellyfinService()
        active = TorrentSnapshot(
            qbit_hash="0123456789abcdef0123456789abcdef01234567",
            name="Example Movie",
            progress=0.5,
            state="downloading",
            download_speed=1048576,
            eta=120,
            amount_left=50,
            size=100,
            completion_on=0,
            save_path="/mnt/raid1/jellyfin/media/movies",
            tags=frozenset({"padval-bot"}),
        )
        service.snapshots = (active,)
        now = [100.0]
        bot = TelegramBot(
            self.bot.config,
            lambda: "ok",
            service,
            jellyfin,
            wall_clock=lambda: now[0],
        )
        bot.api = lambda method, **params: {
            "ok": True,
            "result": {"message_id": 1},
        }
        bot._poll_tracking()

        service.snapshots = (
            TorrentSnapshot(
                qbit_hash=active.qbit_hash,
                name=active.name,
                progress=1.0,
                state="uploading",
                download_speed=0,
                eta=0,
                amount_left=0,
                size=100,
                completion_on=200,
                save_path=active.save_path,
                tags=active.tags,
            ),
        )
        now[0] = 200
        bot._poll_tracking()
        self.assertEqual(jellyfin.refreshes, 0)
        now[0] = 260
        bot._poll_tracking()
        bot._poll_tracking()
        self.assertEqual(jellyfin.refreshes, 1)
