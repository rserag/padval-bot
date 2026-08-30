import json
import tempfile
import unittest
from pathlib import Path

from padval_bot.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def write(self, data):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def valid(self):
        return {
            "telegram": {
                "token_file": "/run/token",
                "allowed_chat_id_file": "/run/chat",
                "state_dir": "/run/state",
            },
            "hosts": [{"name": "local", "mode": "local"}],
        }

    def test_minimal_valid_configuration(self):
        loaded = load_config(self.write(self.valid()))
        self.assertEqual(loaded["hosts"][0]["name"], "local")

    def test_rejects_relative_secret_path(self):
        config = self.valid()
        config["telegram"]["token_file"] = "token"
        with self.assertRaises(ConfigError):
            load_config(self.write(config))

    def test_rejects_unknown_host_mode(self):
        config = self.valid()
        config["hosts"][0]["mode"] = "telnet"
        with self.assertRaises(ConfigError):
            load_config(self.write(config))

    def test_rejects_unsafe_routeros_interface(self):
        config = self.valid()
        config["routeros"] = {
            "enabled": True,
            "host": "192.0.2.2",
            "user": "reader",
            "identity_file": "/run/key",
            "known_hosts_file": "/run/known_hosts",
            "wireguard_interface": 'wg1\"; /system reboot',
        }
        with self.assertRaises(ConfigError):
            load_config(self.write(config))
