"""Load chat commands from commands.json and reload them when the file changes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tca")

HANDLERS = {"spotify_song", "spotify_playlist"}

DEFAULT_COMMANDS: dict[str, object] = {
    "cooldown_seconds": 10,
    "!song": {"handler": "spotify_song"},
    "!playlist": {"handler": "spotify_playlist"},
}


class CommandFileError(Exception):
    """commands.json could not be used."""


@dataclass(frozen=True)
class TextCommand:
    text: str


@dataclass(frozen=True)
class HandlerCommand:
    handler: str


Command = TextCommand | HandlerCommand


@dataclass(frozen=True)
class CommandBook:
    cooldown_seconds: float
    commands: dict[str, Command]

    def match(self, text: str) -> tuple[str, Command] | None:
        token = command_token(text)
        if token is None:
            return None
        command = self.commands.get(token)
        if command is None:
            return None
        return token, command


def command_token(text: str) -> str | None:
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    return stripped.split(maxsplit=1)[0].casefold()


def on_cooldown(last_sent_at: float | None, now: float, cooldown_seconds: float) -> bool:
    if cooldown_seconds <= 0 or last_sent_at is None:
        return False
    return now - last_sent_at < cooldown_seconds


def parse_commands(raw: object) -> CommandBook:
    if not isinstance(raw, dict):
        raise CommandFileError("commands.json must be a JSON object.")

    cooldown = raw.get("cooldown_seconds", 10)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise CommandFileError("cooldown_seconds must be a number that is zero or greater.")

    commands: dict[str, Command] = {}
    for key, value in raw.items():
        if key == "cooldown_seconds":
            continue
        if not isinstance(key, str) or not key.startswith("!") or any(char.isspace() for char in key):
            raise CommandFileError(f"{key!r} is not a command name. Names look like !song.")

        name = key.casefold()
        if name in commands:
            raise CommandFileError(f"{key} repeats another command once case is ignored.")

        if isinstance(value, str):
            text = " ".join(value.split())
            if not text:
                raise CommandFileError(f"{key} has an empty reply.")
            commands[name] = TextCommand(text)
            continue

        if isinstance(value, dict):
            handler = value.get("handler")
            if not isinstance(handler, str) or handler not in HANDLERS:
                allowed = ", ".join(sorted(HANDLERS))
                raise CommandFileError(f"{key} handler must be one of: {allowed}.")
            commands[name] = HandlerCommand(handler)
            continue

        raise CommandFileError(f"{key} must be a text reply or a handler object.")

    return CommandBook(cooldown_seconds=float(cooldown), commands=commands)


class CommandFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self.book = CommandBook(cooldown_seconds=10, commands={})
        self.reload()

    def reload_if_changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            logger.exception("Could not read %s", self.path)
            return False
        if self._mtime is not None and mtime == self._mtime:
            return False
        try:
            self.reload()
        except CommandFileError as exc:
            logger.error("Left the previous commands in place. %s", exc)
            self._mtime = mtime
            return False
        return True

    def reload(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(DEFAULT_COMMANDS, indent=2) + "\n", encoding="utf-8")
            logger.info("Created %s", self.path)

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandFileError(f"{self.path} is not valid JSON: {exc}") from exc

        self.book = parse_commands(raw)
        self._mtime = self.path.stat().st_mtime
        names = ", ".join(sorted(self.book.commands)) or "(none)"
        logger.info("Loaded commands from %s: %s", self.path.name, names)
