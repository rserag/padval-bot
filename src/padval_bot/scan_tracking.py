"""Restart-safe state for one Telegram-tracked Jellyfin library scan."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


class ScanTrackingStateError(RuntimeError):
    """Raised when Jellyfin scan tracking state is unsafe or unavailable."""


@dataclass(slots=True)
class ScanTrackingRecord:
    chat_id: int
    message_id: int
    requested_at: float
    triggered_by_bot: bool
    baseline_last_execution_end: str | None
    observed_running: bool = False
    idle_observations: int = 0


class ScanTrackingStore:
    VERSION = 1
    MAX_BYTES = 64 * 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self.record: ScanTrackingRecord | None = None
        self._load()

    @staticmethod
    def _optional_short_string(value: object) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 80
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ScanTrackingStateError("scan tracking state contains invalid values")
        return value

    @classmethod
    def _record_from_json(cls, value: object) -> ScanTrackingRecord:
        if not isinstance(value, dict):
            raise ScanTrackingStateError("scan tracking state is invalid")
        chat_id = value.get("chat_id")
        message_id = value.get("message_id")
        requested_at = value.get("requested_at")
        triggered = value.get("triggered_by_bot")
        observed = value.get("observed_running", False)
        idle_observations = value.get("idle_observations", 0)
        if (
            not isinstance(chat_id, int)
            or isinstance(chat_id, bool)
            or chat_id <= 0
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
            or isinstance(requested_at, bool)
            or not isinstance(requested_at, (int, float))
            or requested_at < 0
            or requested_at > 100_000_000_000
            or not math.isfinite(requested_at)
            or not isinstance(triggered, bool)
            or not isinstance(observed, bool)
            or isinstance(idle_observations, bool)
            or not isinstance(idle_observations, int)
            or not 0 <= idle_observations <= 1000
        ):
            raise ScanTrackingStateError("scan tracking state contains invalid values")
        return ScanTrackingRecord(
            chat_id=chat_id,
            message_id=message_id,
            requested_at=float(requested_at),
            triggered_by_bot=triggered,
            baseline_last_execution_end=cls._optional_short_string(
                value.get("baseline_last_execution_end")
            ),
            observed_running=observed,
            idle_observations=idle_observations,
        )

    def _load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ScanTrackingStateError("cannot read scan tracking state") from exc
        if len(raw) > self.MAX_BYTES:
            raise ScanTrackingStateError("scan tracking state is too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScanTrackingStateError("scan tracking state is invalid") from exc
        if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
            raise ScanTrackingStateError("scan tracking state version is unsupported")
        active = payload.get("active")
        if active is not None:
            self.record = self._record_from_json(active)

    def _save(self) -> None:
        payload = {
            "version": self.VERSION,
            "active": asdict(self.record) if self.record is not None else None,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".jellyfin-scan-tracking-"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ScanTrackingStateError("cannot update scan tracking state") from exc
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def start(
        self,
        *,
        chat_id: int,
        message_id: int,
        requested_at: float,
        triggered_by_bot: bool,
        baseline_last_execution_end: str | None,
        observed_running: bool,
    ) -> None:
        self.record = self._record_from_json(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "requested_at": requested_at,
                "triggered_by_bot": triggered_by_bot,
                "baseline_last_execution_end": baseline_last_execution_end,
                "observed_running": observed_running,
                "idle_observations": 0,
            }
        )
        self._save()

    def observe(self, *, active: bool) -> ScanTrackingRecord:
        if self.record is None:
            raise ScanTrackingStateError("there is no active scan tracking record")
        if active:
            self.record.observed_running = True
            self.record.idle_observations = 0
        else:
            self.record.idle_observations = min(1000, self.record.idle_observations + 1)
        self._save()
        return self.record

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise ScanTrackingStateError("cannot clear scan tracking state") from exc
        self.record = None
