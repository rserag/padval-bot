import tempfile
import unittest
from pathlib import Path

from padval_bot.torrent import (
    MediaRefreshSettings,
    TorrentSnapshot,
    TorrentTrackingSettings,
)
from padval_bot.tracking import TrackingStore


HASH = "0123456789abcdef0123456789abcdef01234567"


def snapshot(*, complete=False, tags=frozenset({"padval-bot"})):
    return TorrentSnapshot(
        qbit_hash=HASH,
        name="Example Movie",
        progress=1.0 if complete else 0.5,
        state="uploading" if complete else "downloading",
        download_speed=0 if complete else 1024,
        eta=0 if complete else 120,
        amount_left=0 if complete else 50,
        size=100,
        completion_on=200 if complete else 0,
        save_path="/mnt/raid1/jellyfin/media/movies",
        tags=tags,
    )


class TrackingStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "torrent-tracking.json"
        self.settings = TorrentTrackingSettings(enabled=True)

    def test_pending_torrent_is_discovered_and_notified_once(self):
        store = TrackingStore(self.path, self.settings)
        store.register_pending(
            "1234abcd",
            chat_id=42,
            destination_label="Movies",
            discovery_tag="padval-track-1234abcd",
            now=100,
        )
        active = snapshot(tags=frozenset({"padval-bot", "padval-track-1234abcd"}))
        events, discovered = store.reconcile(
            (active,),
            chat_id=42,
            now=110,
            destination_label=lambda path: "Movies",
        )
        self.assertEqual(events, ())
        self.assertEqual(discovered, ((HASH, "padval-track-1234abcd"),))

        events, _ = store.reconcile(
            (snapshot(complete=True),),
            chat_id=42,
            now=200,
            destination_label=lambda path: "Movies",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].chat_id, 42)
        store.mark_notified(events[0].record_id, completion_on=200, now=201)

        reloaded = TrackingStore(self.path, self.settings)
        events, _ = reloaded.reconcile(
            (snapshot(complete=True),),
            chat_id=42,
            now=220,
            destination_label=lambda path: "Movies",
        )
        self.assertEqual(events, ())
        self.assertNotIn("magnet:", self.path.read_text(encoding="utf-8"))

    def test_imports_only_incomplete_existing_torrents(self):
        store = TrackingStore(self.path, self.settings)
        events, _ = store.reconcile(
            (snapshot(),),
            chat_id=42,
            now=100,
            destination_label=lambda path: "Movies",
        )
        self.assertEqual(events, ())
        self.assertTrue(store.notification_enabled(HASH))
        self.assertTrue(store.imported_existing)

    def test_notification_can_be_toggled(self):
        store = TrackingStore(self.path, self.settings)
        enabled = store.toggle_notification(
            snapshot(),
            chat_id=42,
            destination_label="Movies",
            now=100,
        )
        self.assertTrue(enabled)
        enabled = store.toggle_notification(
            snapshot(),
            chat_id=42,
            destination_label="Movies",
            now=101,
        )
        self.assertFalse(enabled)

    def test_media_refresh_is_debounced_and_retried_durably(self):
        settings = TorrentTrackingSettings(
            enabled=True,
            notify_on_complete=False,
            media_refresh=MediaRefreshSettings(
                enabled=True,
                debounce_seconds=60,
                retry_base_seconds=300,
                retry_max_seconds=3600,
            ),
        )
        store = TrackingStore(self.path, settings)
        store.reconcile(
            (snapshot(),),
            chat_id=42,
            now=100,
            destination_label=lambda path: "Movies",
        )
        events, _ = store.reconcile(
            (snapshot(complete=True),),
            chat_id=42,
            now=200,
            destination_label=lambda path: "Movies",
        )
        self.assertEqual(events, ())
        self.assertIsNone(store.refresh_due(now=259))
        batch = store.refresh_due(now=260)
        self.assertIsNotNone(batch)
        store.mark_refresh_attempt(batch, now=260, success=False)

        reloaded = TrackingStore(self.path, settings)
        self.assertIsNone(reloaded.refresh_due(now=559))
        retry = reloaded.refresh_due(now=560)
        self.assertIsNotNone(retry)
        reloaded.mark_refresh_attempt(retry, now=560, success=True)
        self.assertIsNone(reloaded.refresh_due(now=1000))

    def test_version_one_notified_completion_is_not_refreshed(self):
        self.path.write_text(
            '{"version":1,"imported_existing":true,"records":{'
            '"old":{"record_id":"old","chat_id":42,'
            '"destination_label":"Movies","notifications_enabled":true,'
            '"created_at":100,"qbit_hash":"' + HASH + '",'
            '"discovery_tag":null,"completion_notified_at":200,'
            '"completion_on":200}}}\n',
            encoding="utf-8",
        )
        settings = TorrentTrackingSettings(
            enabled=True,
            media_refresh=MediaRefreshSettings(enabled=True),
        )
        store = TrackingStore(self.path, settings)
        self.assertIsNone(store.refresh_due(now=1000))


if __name__ == "__main__":
    unittest.main()
