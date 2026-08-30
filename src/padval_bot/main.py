"""Command-line entry point."""

from __future__ import annotations

import argparse

from .collector import collect_snapshot, render_report
from .config import load_config
from .telegram import TelegramBot


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram homelab status bot")
    parser.add_argument("--config", required=True, help="path to the private JSON configuration")
    parser.add_argument("--once", action="store_true", help="print one report and exit")
    arguments = parser.parse_args()

    config = load_config(arguments.config)

    def build() -> str:
        return render_report(collect_snapshot(config), config.get("title", "SYSTEM STATUS"))

    if arguments.once:
        print(build())
        return
    TelegramBot(config["telegram"], build).run_forever()
