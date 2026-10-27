"""Decide whether a channel-point redemption is a Spotify song to queue.

A Spotify link in ordinary chat is never considered here. Only the text a viewer
typed into the Spotify Song Request reward is checked.
"""

from __future__ import annotations

import re

SONG_REQUEST_REWARD = "spotify song request"

# Track ids are 22 characters. Playlist, album, and artist links do not match.
_TRACK_LINK = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?track/|spotify:track:)"
    r"([0-9A-Za-z]{22})",
    re.IGNORECASE,
)


def is_song_request_reward(title: str) -> bool:
    return " ".join(title.casefold().split()) == SONG_REQUEST_REWARD


def track_id_from_text(text: str) -> str | None:
    match = _TRACK_LINK.search(text)
    if match is None:
        return None
    return match.group(1)


def song_request_track(reward_title: str, user_input: str) -> str | None:
    """Return a track id only when this reward is Spotify Song Request and the input has a song link."""
    if not is_song_request_reward(reward_title):
        return None
    return track_id_from_text(user_input)
