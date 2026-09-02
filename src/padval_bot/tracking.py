"""Durable, privacy-minimized completion tracking for bot-added torrents."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .torrent import TorrentSnapshot, TorrentTrackingSettings


class TrackingStateError(RuntimeError):
    """Raised when durable tracking state cannot be read or updated safely."""


@dataclass(slots=True)
class TrackingRecord:
    record_id: str
    chat_id: int
    destination_label: str
    notifications_enabled: bool
    created_at: float
    qbit_hash: str | None = None
    discovery_tag: str | None = None
    completion_detected_at: float | None = None
    completion_notified_at: float | None = None
    completion_on: int | None = None
    media_refresh_completed_at: float | None = None
    media_refresh_last_attempt_at: float | None = None
    media_refresh_failures: int = 0


@dataclass(frozen=True, slots=True)
class CompletionEvent:
    record_id: str
    chat_id: int
    destination_label: str
    snapshot: TorrentSnapshot


@dataclass(frozen=True, slots=True)
class MediaRefreshBatch:
    record_ids: tuple[str, ...]


class TrackingStore:
    VERSION = 2

    def __init__(self, path: Path, settings: TorrentTrackingSettings) -> None:
        self.path = path
        self.settings = settings
        self.imported_existing = False
        self.records: dict[str, TrackingRecord] = {}
        self._load()

    @staticmethod
    def _valid_hash(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{40,64}", value):
            raise TrackingStateError("torrent tracking state contains an invalid hash")
        return value.lower()

    @staticmethod
    def _valid_tag(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", value
        ):
            raise TrackingStateError("torrent tracking state contains an invalid tag")
        return value

    @classmethod
    def _record_from_json(
        cls, record_id: str, value: object, *, source_version: int
    ) -> TrackingRecord:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", record_id) or not isinstance(
            value, dict
        ):
            raise TrackingStateError(
                "torrent tracking state contains an invalid record"
            )
        chat_id = value.get("chat_id")
        destination = value.get("destination_label")
        notifications = value.get("notifications_enabled")
        created_at = value.get("created_at")
        detected_at = value.get("completion_detected_at")
        notified_at = value.get("completion_notified_at")
        completion_on = value.get("completion_on")
        refresh_completed_at = value.get("media_refresh_completed_at")
        refresh_last_attempt_at = value.get("media_refresh_last_attempt_at")
        refresh_failures = value.get("media_refresh_failures", 0)
        timestamps = (
            detected_at,
            notified_at,
            refresh_completed_at,
            refresh_last_attempt_at,
        )
        if (
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or not isinstance(destination, str)
            or not 1 <= len(destination) <= 64
            or not isinstance(notifications, bool)
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or any(
                item is not None
                and (isinstance(item, bool) or not isinstance(item, (int, float)))
                for item in timestamps
            )
            or (
                completion_on is not None
                and (
                    isinstance(completion_on, bool)
                    or not isinstance(completion_on, int)
                )
            )
            or isinstance(refresh_failures, bool)
            or not isinstance(refresh_failures, int)
            or not 0 <= refresh_failures <= 1000
        ):
            raise TrackingStateError("torrent tracking state contains invalid values")
        # Version 1 predates media refreshes. Treat already-notified historical
        # completions as satisfied so an upgrade does not rescan old downloads.
        if source_version == 1 and notified_at is not None:
            detected_at = notified_at
            refresh_completed_at = notified_at
        return TrackingRecord(
            record_id=record_id,
            chat_id=chat_id,
            destination_label=destination,
            notifications_enabled=notifications,
            created_at=float(created_at),
            qbit_hash=cls._valid_hash(value.get("qbit_hash")),
            discovery_tag=cls._valid_tag(value.get("discovery_tag")),
            completion_detected_at=(
                float(detected_at) if detected_at is not None else None
            ),
            completion_notified_at=(
                float(notified_at) if notified_at is not None else None
            ),
            completion_on=completion_on,
            media_refresh_completed_at=(
                float(refresh_completed_at)
                if refresh_completed_at is not None
                else None
            ),
            media_refresh_last_attempt_at=(
                float(refresh_last_attempt_at)
                if refresh_last_attempt_at is not None
                else None
            ),
            media_refresh_failures=refresh_failures,
        )

    def _load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TrackingStateError("cannot read torrent tracking state") from exc
        if len(raw) > 1024 * 1024:
            raise TrackingStateError("torrent tracking state is too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrackingStateError("torrent tracking state is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            raise TrackingStateError("torrent tracking state version is unsupported")
        source_version = int(payload["version"])
        imported = payload.get("imported_existing", False)
        rows = payload.get("records", {})
        if not isinstance(imported, bool) or not isinstance(rows, dict):
            raise TrackingStateError("torrent tracking state is invalid")
        self.imported_existing = imported
        self.records = {
            record_id: self._record_from_json(
                record_id, value, source_version=source_version
            )
            for record_id, value in rows.items()
        }

    def _save(self) -> None:
        payload = {
            "version": self.VERSION,
            "imported_existing": self.imported_existing,
            "records": {
                record_id: asdict(record)
                for record_id, record in sorted(self.records.items())
            },
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".torrent-tracking-"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise TrackingStateError("cannot update torrent tracking state") from exc
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def register_pending(
        self,
        record_id: str,
        *,
        chat_id: int,
        destination_label: str,
        discovery_tag: str,
        now: float,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", record_id):
            raise TrackingStateError("tracking record id is invalid")
        self.records[record_id] = TrackingRecord(
            record_id=record_id,
            chat_id=chat_id,
            destination_label=destination_label[:64],
            notifications_enabled=self.settings.notify_on_complete,
            created_at=now,
            discovery_tag=self._valid_tag(discovery_tag),
        )
        self._save()

    def reconcile(
        self,
        snapshots: tuple[TorrentSnapshot, ...],
        *,
        chat_id: int,
        now: float,
        destination_label: Callable[[str], str],
    ) -> tuple[tuple[CompletionEvent, ...], tuple[tuple[str, str], ...]]:
        changed = False
        discovered: list[tuple[str, str]] = []
        by_hash = {snapshot.qbit_hash: snapshot for snapshot in snapshots}
        for record in self.records.values():
            if record.qbit_hash is not None or record.discovery_tag is None:
                continue
            matches = [
                snapshot
                for snapshot in snapshots
                if record.discovery_tag in snapshot.tags
            ]
            if len(matches) == 1:
                record.qbit_hash = matches[0].qbit_hash
                discovered.append((matches[0].qbit_hash, record.discovery_tag))
                record.discovery_tag = None
                changed = True

        if (
            self.settings.import_incomplete_tagged_on_start
            and not self.imported_existing
            and chat_id
        ):
            known = {
                record.qbit_hash
                for record in self.records.values()
                if record.qbit_hash is not None
            }
            for snapshot in snapshots:
                if snapshot.complete or snapshot.qbit_hash in known:
                    continue
                record_id = "import-" + snapshot.qbit_hash
                self.records[record_id] = TrackingRecord(
                    record_id=record_id,
                    chat_id=chat_id,
                    destination_label=destination_label(snapshot.save_path),
                    notifications_enabled=self.settings.notify_on_complete,
                    created_at=now,
                    qbit_hash=snapshot.qbit_hash,
                )
            self.imported_existing = True
            changed = True

        cutoff = now - self.settings.completed_retention_hours * 3600
        for record_id, record in list(self.records.items()):
            if (
                record.completion_notified_at is not None
                and record.completion_notified_at < cutoff
            ):
                del self.records[record_id]
                changed = True

        for record in self.records.values():
            if record.qbit_hash is None:
                continue
            snapshot = by_hash.get(record.qbit_hash)
            if (
                snapshot is not None
                and snapshot.complete
                and record.completion_detected_at is None
            ):
                record.completion_detected_at = now
                record.completion_on = snapshot.completion_on
                changed = True

        events = tuple(
            CompletionEvent(
                record.record_id, record.chat_id, record.destination_label, snapshot
            )
            for record in self.records.values()
            if record.qbit_hash is not None
            and (snapshot := by_hash.get(record.qbit_hash)) is not None
            and snapshot.complete
            and record.notifications_enabled
            and record.completion_notified_at is None
        )
        if changed:
            self._save()
        return events, tuple(discovered)

    def notification_enabled(self, qbit_hash: str) -> bool:
        return any(
            record.qbit_hash == qbit_hash and record.notifications_enabled
            for record in self.records.values()
        )

    def toggle_notification(
        self,
        snapshot: TorrentSnapshot,
        *,
        chat_id: int,
        destination_label: str,
        now: float,
    ) -> bool:
        record = next(
            (
                item
                for item in self.records.values()
                if item.qbit_hash == snapshot.qbit_hash
            ),
            None,
        )
        if record is None:
            record_id = "manual-" + snapshot.qbit_hash
            record = TrackingRecord(
                record_id=record_id,
                chat_id=chat_id,
                destination_label=destination_label[:64],
                notifications_enabled=True,
                created_at=now,
                qbit_hash=snapshot.qbit_hash,
            )
            self.records[record_id] = record
        else:
            record.notifications_enabled = not record.notifications_enabled
        self._save()
        return record.notifications_enabled

    def mark_notified(self, record_id: str, *, completion_on: int, now: float) -> None:
        record = self.records.get(record_id)
        if record is None:
            return
        record.completion_on = completion_on
        record.completion_notified_at = now
        self._save()

    def refresh_due(self, *, now: float) -> MediaRefreshBatch | None:
        settings = self.settings.media_refresh
        if not settings.enabled:
            return None
        pending = [
            record
            for record in self.records.values()
            if record.completion_detected_at is not None
            and record.media_refresh_completed_at is None
        ]
        if not pending:
            return None
        newest_completion = max(
            record.completion_detected_at or 0.0 for record in pending
        )
        if now < newest_completion + settings.debounce_seconds:
            return None
        last_attempt = max(
            (record.media_refresh_last_attempt_at or 0.0 for record in pending),
            default=0.0,
        )
        failures = max(record.media_refresh_failures for record in pending)
        if last_attempt:
            delay = min(
                settings.retry_base_seconds * (2 ** max(0, failures - 1)),
                settings.retry_max_seconds,
            )
            if now < last_attempt + delay:
                return None
        return MediaRefreshBatch(tuple(record.record_id for record in pending))

    def mark_refresh_attempt(
        self, batch: MediaRefreshBatch, *, now: float, success: bool
    ) -> None:
        changed = False
        for record_id in batch.record_ids:
            record = self.records.get(record_id)
            if record is None or record.media_refresh_completed_at is not None:
                continue
            record.media_refresh_last_attempt_at = now
            if success:
                record.media_refresh_completed_at = now
                record.media_refresh_failures = 0
            else:
                record.media_refresh_failures = min(
                    1000, record.media_refresh_failures + 1
                )
            changed = True
        if changed:
            self._save()
