import os
import tempfile
import unittest
from pathlib import Path

from padval_bot.telegram import TelegramBot


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
        self.bot = TelegramBot({
            "token_file": str(self.token),
            "allowed_chat_id_file": str(self.chat),
            "pairing_secret_file": str(self.secret),
            "state_dir": str(root / "state"),
        }, lambda: "ok")

    def test_pairing_binds_one_private_chat(self):
        self.assertTrue(self.bot.authorize(
            {"id": 42, "type": "private"}, ["correct-horse-battery-staple"]
        ))
        self.assertEqual(self.chat.read_text(encoding="ascii").strip(), "42")
        self.assertEqual(os.stat(self.chat).st_mode & 0o777, 0o600)
        self.assertFalse(self.bot.authorize({"id": 43, "type": "private"}, []))

    def test_wrong_pairing_secret_is_ignored(self):
        self.assertFalse(self.bot.authorize({"id": 42, "type": "private"}, ["wrong"]))
        self.assertFalse(self.chat.exists())

    def test_send_uses_telegram_html(self):
        calls = []
        self.bot.api = lambda method, **params: calls.append((method, params)) or {"ok": True}
        self.bot.send(42, "<b>healthy</b>")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")
