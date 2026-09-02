import json
import os
import tempfile
import unittest
from pathlib import Path

from padval_bot.scan_tracking import ScanTrackingStateError, ScanTrackingStore


class ScanTrackingStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jellyfin-scan-tracking.json"

    def test_active_scan_survives_restart_with_private_permissions(self):
        store = ScanTrackingStore(self.path)
        store.start(
            chat_id=42,
            message_id=123,
            requested_at=100.5,
            triggered_by_bot=True,
            baseline_last_execution_end="2026-09-02T11:32:11Z",
            observed_running=True,
        )

        restored = ScanTrackingStore(self.path)

        self.assertEqual(restored.record.chat_id, 42)
        self.assertTrue(restored.record.observed_running)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertNotIn("api", self.path.read_text(encoding="utf-8").lower())

    def test_observe_and_clear_are_durable(self):
        store = ScanTrackingStore(self.path)
        store.start(
            chat_id=42,
            message_id=123,
            requested_at=100,
            triggered_by_bot=False,
            baseline_last_execution_end=None,
            observed_running=False,
        )
        store.observe(active=False)
        store.observe(active=True)
        self.assertTrue(store.record.observed_running)
        self.assertEqual(store.record.idle_observations, 0)

        store.clear()

        self.assertIsNone(store.record)
        self.assertFalse(self.path.exists())

    def test_invalid_state_is_rejected(self):
        self.path.write_text(
            json.dumps({"version": 1, "active": {"chat_id": "wrong"}}),
            encoding="utf-8",
        )
        with self.assertRaises(ScanTrackingStateError):
            ScanTrackingStore(self.path)


if __name__ == "__main__":
    unittest.main()
