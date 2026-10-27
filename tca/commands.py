"""Load chat commands from commands.json and reload them when the file changes."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from tca.replies import TWITCH_MESSAGE_LIMIT

logger = logging.getLogger("tca")

HANDLERS = {"spotify_song", "spotify_playlist", "spotify_queue"}

DEFAULT_COMMANDS: dict[str, object] = {
    "cooldown_seconds": 10,
    "!song": {"handler": "spotify_song"},
    "!playlist": {"handler": "spotify_playlist"},
    "!queue": {"handler": "spotify_queue"},
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


def command_name(raw: str) -> str:
    """The stored form of a command name: !discord."""
    name = raw.strip()
    if not name:
        raise CommandFileError("Write a command name, such as !discord.")
    if any(char.isspace() for char in name):
        raise CommandFileError("A command name is one word, such as !discord.")
    if not name.startswith("!"):
        name = f"!{name}"
    if name == "!":
        raise CommandFileError("A command name looks like !discord.")
    return name.casefold()


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
        self._lock = threading.Lock()
        self.book = CommandBook(cooldown_seconds=10, commands={})
        self._reload()

    def text_commands(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(
                (name, command.text)
                for name, command in self.book.commands.items()
                if isinstance(command, TextCommand)
            )

    def handler_names(self) -> list[str]:
        with self._lock:
            return sorted(
                name for name, command in self.book.commands.items() if isinstance(command, HandlerCommand)
            )

    def add_text_command(self, name: str, reply: str) -> str:
        """Save a text reply under this command name. Returns the stored name."""
        stored = command_name(name)
        text = " ".join(reply.split())
        if not text:
            raise CommandFileError("Write the reply chat should see.")
        if len(text) > TWITCH_MESSAGE_LIMIT:
            raise CommandFileError(f"A reply can be at most {TWITCH_MESSAGE_LIMIT} characters.")
        with self._lock:
            raw = self._read_raw()
            existing = {key.casefold() for key in raw if key != "cooldown_seconds"}
            if stored in existing:
                raise CommandFileError(f"{stored} already exists.")
            raw[stored] = text
            parse_commands(raw)
            self._write(raw)
            self.book = parse_commands(raw)
            self._mtime = self.path.stat().st_mtime
        logger.info("Added command %s.", stored)
        return stored

    def remove_text_command(self, name: str) -> None:
        """Remove a text command. Built-in handler commands stay."""
        stored = command_name(name)
        with self._lock:
            raw = self._read_raw()
            key = next((item for item in raw if isinstance(item, str) and item.casefold() == stored), None)
            if key is None:
                raise CommandFileError(f"{stored} is not a command.")
            if not isinstance(raw[key], str):
                raise CommandFileError(f"{stored} is built in and stays in the list.")
            del raw[key]
            parse_commands(raw)
            self._write(raw)
            self.book = parse_commands(raw)
            self._mtime = self.path.stat().st_mtime
        logger.info("Removed command %s.", stored)

    def reload_if_changed(self) -> bool:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                logger.exception("Could not read %s", self.path)
                return False
            if self._mtime is not None and mtime == self._mtime:
                return False
            try:
                self._reload()
            except CommandFileError as exc:
                logger.error("Left the previous commands in place. %s", exc)
                self._mtime = mtime
                return False
            return True

    def reload(self) -> None:
        with self._lock:
            self._reload()

    def _reload(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write(dict(DEFAULT_COMMANDS))
            logger.info("Created %s", self.path)

        self.book = parse_commands(self._read_raw())
        self._mtime = self.path.stat().st_mtime
        names = ", ".join(sorted(self.book.commands)) or "(none)"
        logger.info("Loaded commands from %s: %s", self.path.name, names)

    def _read_raw(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandFileError(f"{self.path} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise CommandFileError(f"Could not read {self.path.name}.") from exc
        if not isinstance(raw, dict):
            raise CommandFileError("commands.json must be a JSON object.")
        return raw

    def _write(self, raw: dict[str, object]) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise CommandFileError("Could not save commands.json.") from exc
