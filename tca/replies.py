"""Turn a Spotify playback payload into a chat reply."""

from __future__ import annotations

from collections import OrderedDict

TWITCH_MESSAGE_LIMIT = 500


class ReplyMemory:
    """Exact replies this process already sent, so an echo cannot trigger another command."""

    def __init__(self, limit: int = 50) -> None:
        self._items: OrderedDict[str, None] = OrderedDict()
        self._limit = limit

    def remember(self, text: str) -> None:
        self._items[text] = None
        self._items.move_to_end(text)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def seen(self, text: str) -> bool:
        return text in self._items


def clamp_message(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= TWITCH_MESSAGE_LIMIT:
        return collapsed
    return collapsed[: TWITCH_MESSAGE_LIMIT - 1].rstrip() + "…"


def playlist_id(playback: dict | None) -> str | None:
    if not playback:
        return None
    context = playback.get("context") or {}
    if context.get("type") != "playlist":
        return None
    uri = context.get("uri") or ""
    marker = "spotify:playlist:"
    if isinstance(uri, str) and uri.startswith(marker):
        playlist = uri[len(marker) :]
        return playlist or None
    return None


def format_song(playback: dict | None) -> str:
    if not playback:
        return "Nothing is playing on Spotify right now."

    item = playback.get("item")
    playing_type = playback.get("currently_playing_type")
    is_playing = bool(playback.get("is_playing"))

    if playing_type == "ad":
        return "An ad is playing on Spotify right now."

    if playing_type == "episode" or (isinstance(item, dict) and item.get("type") == "episode"):
        name = item.get("name") if isinstance(item, dict) else None
        title = name or "a podcast"
        if is_playing:
            return f"A podcast is playing: {title}."
        return f"Spotify is paused on a podcast: {title}."

    if not isinstance(item, dict):
        if is_playing:
            return "Spotify is playing, but the track is hidden. A private session does this."
        return "Nothing is playing on Spotify right now."

    artists = ", ".join(
        artist["name"]
        for artist in item.get("artists") or []
        if isinstance(artist, dict) and artist.get("name")
    ) or "Unknown artist"
    title = item.get("name") or "Unknown title"
    url = (item.get("external_urls") or {}).get("spotify")
    label = f"{artists} - {title}"
    if isinstance(url, str) and url:
        label = f"{label} — {url}"
    if is_playing:
        return f"Now playing: {label}"
    return f"Spotify is paused on: {label}"


def format_playlist(playback: dict | None, playlist_name: str | None) -> str:
    # Spotify keeps the previous playlist on this payload after pause, stop,
    # or a switch to something that is not a playlist.
    url = _active_playlist_url(playback)
    if not url:
        return "Currently not listening to a playlist"
    if playlist_name:
        return f"Playlist: {playlist_name} — {url}"
    return f"Playlist: {url}"


def _active_playlist_url(playback: dict | None) -> str | None:
    if not playback or playback.get("is_playing") is not True:
        return None
    context = playback.get("context") or {}
    if context.get("type") != "playlist":
        return None
    url = (context.get("external_urls") or {}).get("spotify")
    if isinstance(url, str) and url:
        return url
    return None
