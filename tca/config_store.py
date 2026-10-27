"""Local app credentials saved by the status window."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tca")


@dataclass(frozen=True)
class Config:
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    @property
    def twitch_ready(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)

    @property
    def spotify_ready(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("config.json could not be read. TCA will ask for setup again.")
        return Config()
    if not isinstance(raw, dict):
        return Config()
    return Config(
        twitch_client_id=_text(raw.get("twitch_client_id")),
        twitch_client_secret=_text(raw.get("twitch_client_secret")),
        spotify_client_id=_text(raw.get("spotify_client_id")),
        spotify_client_secret=_text(raw.get("spotify_client_secret")),
    )


def save_config(path: Path, config: Config) -> None:
    payload = {
        "twitch_client_id": config.twitch_client_id,
        "twitch_client_secret": config.twitch_client_secret,
        "spotify_client_id": config.spotify_client_id,
        "spotify_client_secret": config.spotify_client_secret,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def saved_twitch_user_id(path: Path) -> str | None:
    """User id already stored by Twitch login, so a restart can skip asking who to sign in."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    for key in raw:
        if isinstance(key, str) and key.strip():
            return key
    return None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
