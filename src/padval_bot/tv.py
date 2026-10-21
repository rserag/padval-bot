"""TV destination names and confined storage operations."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def valid_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not value.startswith(".")
        and not any(
            character in "/\\" or unicodedata.category(character).startswith("C")
            for character in value
        )
        and len(value.encode("utf-8")) <= 200
    )


def season_name(value: str) -> str:
    match = re.fullmatch(r"(?:season\s*)?([0-9]{1,3})", value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError("Enter a season number from 0 to 999 (0 is Specials).")
    return f"Season {int(match[1]):02d}"


@dataclass(slots=True)
class TVSelection:
    stage: str = "shows"
    show: str | None = None
    season: str | None = None
    options: tuple[str, ...] = ()
    nonce: str = ""
    prompt_message_id: int | None = None


# Executed on the storage host. User text travels only through JSON stdin.
# Directory descriptors and O_NOFOLLOW prevent symlink swaps from escaping the
# selected TV root while listing or creating a show/season directory.
TV_STORAGE_SCRIPT = r"""
import json
import os
import re
import sys
import unicodedata

def component(value):
    return (isinstance(value, str) and bool(value)
            and value == value.strip() and not value.startswith(".")
            and not any(c in "/\\" or unicodedata.category(c).startswith("C")
                        for c in value)
            and len(value.encode("utf-8")) <= 200)

def directory(parent, name, create=False):
    if not component(name):
        raise ValueError()
    if create:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                   dir_fd=parent)

def listing(parent, seasons=False):
    names = []
    with os.scandir(parent) as entries:
        for entry in entries:
            if (component(entry.name) and entry.is_dir(follow_symlinks=False)
                    and (not seasons or re.match(
                        r"^(?:Season[ ._-]*[0-9]{1,3}\b|Specials$)",
                        entry.name, re.IGNORECASE))):
                names.append(entry.name)
                if len(names) > 1000:
                    raise ValueError()
    return sorted(names, key=lambda name: (name.casefold(), name))

def main(request):
    root = os.path.realpath(request["root"])
    roots = [os.path.realpath(value) for value in request["roots"]]
    if not any(os.path.commonpath([root, allowed]) == allowed for allowed in roots):
        raise ValueError()
    action = request["action"]
    if action not in {"shows", "seasons", "prepare"}:
        raise ValueError()
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if action == "shows":
            return {"names": listing(root_fd)}
        try:
            show_fd = directory(root_fd, request["show"], action == "prepare")
        except FileNotFoundError:
            if action == "seasons":
                return {"names": []}
            raise
        try:
            if action == "seasons":
                return {"names": listing(show_fd, True)}
            season_fd = directory(show_fd, request["season"], True)
            os.close(season_fd)
            return {"ready": True}
        finally:
            os.close(show_fd)
    finally:
        os.close(root_fd)

try:
    result = main(json.load(sys.stdin))
except (OSError, ValueError, KeyError, TypeError):
    result = {"error": True}
print(json.dumps(result, separators=(",", ":")))
"""
