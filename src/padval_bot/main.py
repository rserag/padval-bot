"""Command-line entry point."""

from __future__ import annotations

import argparse
from copy import deepcopy

from .collector import collect_snapshot, render_report
from .config import load_config
from .telegram import TelegramBot
from .torrent import TorrentService


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram homelab status bot")
    parser.add_argument(
        "--config", required=True, help="path to the private JSON configuration"
    )
    parser.add_argument("--once", action="store_true", help="print one report and exit")
    parser.add_argument(
        "--check-config", action="store_true", help="validate configuration and exit"
    )
    parser.add_argument(
        "--check-torrent-paths",
        action="store_true",
        help="validate configured torrent destinations and exit",
    )
    parser.add_argument(
        "--torrent-locations",
        help="override the public torrent locations file for release validation",
    )
    arguments = parser.parse_args()

    config = load_config(arguments.config)
    torrent_config = config.get("torrent")
    if isinstance(torrent_config, dict) and arguments.torrent_locations:
        torrent_config = deepcopy(torrent_config)
        torrent_config["locations_file"] = arguments.torrent_locations
    torrent_service = (
        TorrentService(torrent_config)
        if isinstance(torrent_config, dict) and torrent_config.get("enabled")
        else None
    )

    if arguments.check_torrent_paths:
        if torrent_service is None:
            parser.error("torrent submission is not configured")
        torrent_service.preflight()
        return
    if arguments.check_config:
        return

    def build() -> str:
        return render_report(
            collect_snapshot(config), config.get("title", "SYSTEM STATUS")
        )

    if arguments.once:
        print(build())
        return
    TelegramBot(config["telegram"], build, torrent_service).run_forever()
