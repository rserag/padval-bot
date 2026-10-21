import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from padval_bot.telegram import TelegramBot
from padval_bot.torrent import (
    PATH_CHECK_SCRIPT,
    TorrentError,
    TorrentService,
    load_locations,
)
from padval_bot.tv import TV_STORAGE_SCRIPT, season_name, valid_component
from test_torrent import LOCATIONS, Response


class TVWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.media = self.root / "library" / "media"
        self.tv = self.media / "tv"
        self.tv.mkdir(parents=True)
        (self.media / "movies").mkdir()
        self.show = "Example Show (2026)"
        (self.tv / self.show / "Season 01").mkdir(parents=True)
        locations = self.root / "locations.yaml"
        locations.write_text(
            LOCATIONS.replace("/mnt/raid1/jellyfin/media", str(self.media)).replace(
                "label: TV", "label: TV\n      kind: tv"
            ),
            encoding="utf-8",
        )
        self.service = TorrentService(
            {"base_url": "http://192.0.2.20:8080", "locations_file": str(locations)}
        )
        self.service._ssh_command = lambda script=PATH_CHECK_SCRIPT: [
            sys.executable,
            "-c",
            script,
        ]
        token = self.root / "token"
        token.write_text("123456:ExampleTokenValue", encoding="ascii")
        chat = self.root / "chat"
        chat.write_text("42\n", encoding="ascii")
        self.now = 100.0
        self.bot = TelegramBot(
            {
                "token_file": str(token),
                "allowed_chat_id_file": str(chat),
                "state_dir": str(self.root / "state"),
            },
            lambda: "ok",
            self.service,
            clock=lambda: self.now,
        )
        self.calls = []

        def api(method, **params):
            self.calls.append((method, params))
            return {"ok": True, "result": {"message_id": len(self.calls)}}

        self.bot.api = api
        self.api = api
        self.urlopen = mock.patch(
            "urllib.request.urlopen", return_value=Response()
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.message("/torrent magnet:?xt=urn:btih:" + "0" * 40)
        self.click("TV")

    def message(self, text, *, reply=None, chat_id=42):
        message = {"text": text, "chat": {"id": chat_id, "type": "private"}}
        if reply is not None:
            message["reply_to_message"] = {"message_id": reply}
        self.bot.handle_update({"message": message})

    def button(self, label):
        for method, params in reversed(self.calls):
            if method != "sendMessage" or "reply_markup" not in params:
                continue
            markup = json.loads(params["reply_markup"])
            for row in markup.get("inline_keyboard", []):
                for button in row:
                    if button["text"] == label:
                        return button["callback_data"]
        self.fail(f"Button not found: {label}")

    def callback(self, data, *, chat_id=42):
        self.bot.handle_update(
            {
                "callback_query": {
                    "id": "synthetic-callback",
                    "data": data,
                    "message": {"chat": {"id": chat_id, "type": "private"}},
                }
            }
        )

    def click(self, label):
        self.callback(self.button(label))

    def reply(self, text):
        prompt = self.bot.pending_torrents[42].tv.prompt_message_id
        self.message(text, reply=prompt)

    def sent_text(self):
        return [
            params["text"] for method, params in self.calls if method == "sendMessage"
        ]

    def choose_existing(self):
        self.click(self.show)
        self.click("Season 01")

    def test_existing_show_season_requires_confirmation_and_keeps_flat_layout(self):
        self.choose_existing()
        self.urlopen.assert_not_called()
        self.assertIn(str(self.tv / self.show / "Season 01"), self.sent_text()[-1])
        confirm = self.button("Download")
        self.callback(confirm)
        request = self.urlopen.call_args.args[0]
        self.assertIn(str(self.tv / self.show / "Season 01").encode(), request.data)
        self.assertIn(b"NoSubfolder", request.data)
        self.assertIn(b"padval-track-", request.data)
        self.assertNotIn(42, self.bot.pending_torrents)
        self.callback(confirm)
        self.assertEqual(self.urlopen.call_count, 1)
        self.assertIn(
            "Example Show",
            next(iter(self.bot.tracking_store.records.values())).destination_label,
        )

    def test_new_show_and_season_created_only_after_confirmation(self):
        self.click("New show")
        self.reply("New Show (2026)")
        self.click("New season")
        self.reply("2")
        destination = self.tv / "New Show (2026)" / "Season 02"
        self.assertFalse(destination.exists())
        self.click("Download")
        self.assertTrue(destination.is_dir())
        self.assertEqual(self.urlopen.call_count, 1)

    def test_cancel_does_not_create_folders_or_submit(self):
        self.click("New show")
        self.reply("Cancelled Show")
        self.click("New season")
        self.reply("1")
        self.click("Cancel")
        self.assertFalse((self.tv / "Cancelled Show").exists())
        self.urlopen.assert_not_called()

    def test_invalid_show_and_season_can_be_corrected(self):
        self.click("New show")
        self.reply("../escape")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "new_show")
        self.reply("Valid Show")
        self.click("New season")
        self.reply("-1")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "new_season")
        self.reply("Season 03")
        self.assertIn("Season 03", self.sent_text()[-1])

    def test_cancel_command_works_as_prompt_reply(self):
        self.click("New show")
        self.reply("/cancel")
        self.assertNotIn(42, self.bot.pending_torrents)
        self.urlopen.assert_not_called()

    def test_existing_season_number_is_reused(self):
        (self.tv / self.show / "Season 2 (BluRay)").mkdir()
        self.click(self.show)
        self.click("New season")
        self.reply("2")
        self.assertEqual(self.bot.pending_torrents[42].tv.season, "Season 2 (BluRay)")
        self.click("Download")
        self.assertFalse((self.tv / self.show / "Season 02").exists())

    def test_ambiguous_existing_season_requires_selection(self):
        (self.tv / self.show / "Season 1").mkdir()
        self.click(self.show)
        self.click("New season")
        self.reply("1")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "seasons")
        self.assertTrue(any("Several folders" in text for text in self.sent_text()))
        self.urlopen.assert_not_called()

    def test_zero_reuses_specials(self):
        (self.tv / self.show / "Specials").mkdir()
        self.click(self.show)
        self.click("New season")
        self.reply("0")
        self.assertEqual(self.bot.pending_torrents[42].tv.season, "Specials")

    def test_back_navigation_invalidates_old_buttons(self):
        old_show = self.button(self.show)
        self.choose_existing()
        self.click("Change season")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "seasons")
        self.callback(old_show)
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "seasons")
        self.click("Back")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "shows")
        self.click("Back")
        self.assertIsNone(self.bot.pending_torrents[42].tv)
        self.urlopen.assert_not_called()

    def test_long_show_names_are_paginated_with_short_callbacks(self):
        for index in range(20):
            (self.tv / (f"Show {index:02d} " + "A" * 100)).mkdir()
        self.click("TV")
        self.click("Next")
        self.assertIn("Page 2", self.sent_text()[-1])
        for method, params in self.calls:
            if method == "sendMessage" and "reply_markup" in params:
                for row in json.loads(params["reply_markup"]).get(
                    "inline_keyboard", []
                ):
                    for button in row:
                        self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_expired_or_unauthorized_confirmation_cannot_create_folders(self):
        self.choose_existing()
        confirm = self.button("Download")
        self.callback(confirm, chat_id=43)
        self.urlopen.assert_not_called()
        self.now += 601
        self.callback(confirm)
        self.assertNotIn(42, self.bot.pending_torrents)
        self.urlopen.assert_not_called()

    def test_unrelated_reply_does_not_change_show(self):
        self.click("New show")
        self.message("Wrong reply", reply=-1)
        self.assertIsNone(self.bot.pending_torrents[42].tv.show)

    def test_storage_failure_keeps_confirmation_for_retry(self):
        self.choose_existing()
        with mock.patch.object(
            self.service,
            "prepare_tv_destination",
            side_effect=TorrentError("Unavailable"),
        ):
            self.click("Download")
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "confirm")
        self.urlopen.assert_not_called()
        self.click("Download")
        self.assertEqual(self.urlopen.call_count, 1)

    def test_listing_failure_restores_previous_buttons(self):
        with mock.patch.object(
            self.service, "list_tv_folders", side_effect=TorrentError("Unavailable")
        ):
            self.click(self.show)
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "shows")
        self.click(self.show)
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "seasons")

    def test_telegram_failure_restores_selection_for_callback_replay(self):
        data = self.button(self.show)

        def api(method, **params):
            if method == "sendMessage":
                raise urllib.error.URLError("synthetic failure")
            return self.api(method, **params)

        self.bot.api = api
        with self.assertRaises(urllib.error.URLError):
            self.callback(data)
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "shows")
        self.bot.api = self.api
        self.callback(data)
        self.assertEqual(self.bot.pending_torrents[42].tv.stage, "seasons")

    def test_idle_loop_clears_expired_magnets(self):
        self.now += 601
        self.bot._poll_tracking = lambda: None

        def api(method, **params):
            if method == "getUpdates":
                raise SystemExit()
            return self.api(method, **params)

        self.bot.api = api
        with self.assertRaises(SystemExit):
            self.bot.run_forever()
        self.assertNotIn(42, self.bot.pending_torrents)

    def test_existing_file_cannot_be_used_as_show_or_season(self):
        (self.tv / "Not a Show").touch()
        with self.assertRaises(TorrentError):
            self.service.prepare_tv_destination("Not a Show", "Season 01")
        (self.tv / self.show / "Season 02").touch()
        with self.assertRaises(TorrentError):
            self.service.prepare_tv_destination(self.show, "Season 02")

    def test_typing_failure_does_not_prevent_submission(self):
        self.choose_existing()

        def api(method, **params):
            if method == "sendChatAction":
                raise urllib.error.URLError("synthetic failure")
            return self.api(method, **params)

        self.bot.api = api
        self.click("Download")
        self.assertEqual(self.urlopen.call_count, 1)

    def test_symlinked_show_or_season_is_not_used(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.tv / "Linked Show").symlink_to(outside, target_is_directory=True)
        self.assertNotIn("Linked Show", self.service.list_tv_folders())
        with self.assertRaises(TorrentError):
            self.service.prepare_tv_destination("Linked Show", "Season 01")
        self.assertFalse((outside / "Season 01").exists())
        (self.tv / self.show / "Season 02").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaises(TorrentError):
            self.service.prepare_tv_destination(self.show, "Season 02")

    def test_remote_storage_rejects_traversal_and_root_escape(self):
        for overrides in (
            {"show": "../outside"},
            {"season": "../../outside"},
            {"root": str(self.root)},
        ):
            request = {
                "root": str(self.tv),
                "roots": [str(self.media)],
                "action": "prepare",
                "show": self.show,
                "season": "Season 02",
            }
            request.update(overrides)
            result = subprocess.run(
                [sys.executable, "-c", TV_STORAGE_SCRIPT],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(json.loads(result.stdout).get("error"))
        self.assertFalse((self.tv / self.show / "Season 02").exists())

    def test_folder_creation_is_idempotent(self):
        path = self.service.prepare_tv_destination(self.show, "Season 02")
        (Path(path) / "existing.mkv").touch()
        self.assertEqual(
            path, self.service.prepare_tv_destination(self.show, "Season 02")
        )
        self.assertTrue((Path(path) / "existing.mkv").exists())

    def test_show_and_season_labels_are_used_in_downloads(self):
        destination = str(self.tv / self.show / "Season 01")
        self.assertEqual(
            self.service.destination_label(destination),
            "TV / " + self.show + " / Season 01",
        )
        self.assertEqual(
            self.service.destination_label(str(self.media / "tv-other")), "Custom"
        )

    def test_tv_kind_configuration_is_explicit(self):
        self.assertEqual(self.service.tv_location.id, "tv")
        path = self.root / "invalid.yaml"
        original = (self.root / "locations.yaml").read_text()
        path.write_text(original.replace("kind: tv", "kind: unsafe"))
        with self.assertRaises(TorrentError):
            load_locations(str(path))
        path.write_text(
            original.replace("label: Movies", "label: Movies\n      kind: tv")
        )
        with self.assertRaises(TorrentError):
            load_locations(str(path))


class TVNameTests(unittest.TestCase):
    def test_names_and_season_numbers(self):
        self.assertTrue(valid_component("Show (2026) [tvdbid-123]"))
        self.assertTrue(valid_component("Հաղորդում"))
        for value in (
            "",
            ".hidden",
            "..",
            "a/b",
            "a\\b",
            "a\n",
            "a\x00",
            "a\u202e",
            "a\ud800",
            "é" * 101,
        ):
            self.assertFalse(valid_component(value))
        self.assertEqual(season_name("season 2"), "Season 02")
        self.assertEqual(season_name("0"), "Season 00")
        for value in ("-1", "1000", "../2", "two"):
            with self.assertRaises(ValueError):
                season_name(value)


if __name__ == "__main__":
    unittest.main()
