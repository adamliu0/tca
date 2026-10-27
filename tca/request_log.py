"""Song requests made with the Spotify Song Request channel points reward.

Spotify's queue does not say who requested a song, so that is remembered here.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tca.replies import TWITCH_MESSAGE_LIMIT

logger = logging.getLogger("tca")

QUEUE_DISPLAY_LIMIT = 5
_STORE_LIMIT = 300


class RequestLogError(Exception):
    """A song request could not be changed."""


@dataclass(frozen=True)
class SongRequest:
    track_id: str
    title: str
    artist: str
    viewer: str


class RequestLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items, self._cancellations = self._load()

    def add(self, request: SongRequest) -> None:
        stored = SongRequest(
            track_id=request.track_id,
            title=_plain(request.title) or "Unknown title",
            artist=_plain(request.artist) or "Unknown artist",
            viewer=_plain(request.viewer) or "someone",
        )
        with self._lock:
            self._items.append(stored)
            if len(self._items) > _STORE_LIMIT:
                self._items = self._items[-_STORE_LIMIT:]
            self._save()

    def upcoming(self, track_ids: list[str], limit: int = QUEUE_DISPLAY_LIMIT) -> list[SongRequest]:
        with self._lock:
            visible = _without_cancellations(track_ids, self._cancellations)
            return [item for _, item in _match_indexed(self._items, visible, limit)]

    def matched(self, track_ids: list[str]) -> list[tuple[int, SongRequest]]:
        """Waiting requests still in this Spotify queue, with their position in the log."""
        with self._lock:
            visible = _without_cancellations(track_ids, self._cancellations)
            return _match_indexed(self._items, visible, limit=None)

    def note_queue(self, track_ids: list[str]) -> list[tuple[int, SongRequest]]:
        """Remember which removals still match the live queue, then return what is waiting."""
        with self._lock:
            updated = _active_cancellations(self._cancellations, track_ids)
            if updated != self._cancellations:
                self._cancellations = updated
                self._save()
            visible = _without_cancellations(track_ids, self._cancellations)
            return _match_indexed(self._items, visible, limit=None)

    def remove_matched(self, track_ids: list[str], index: int) -> SongRequest:
        """Drop one waiting request so it leaves the list and !queue.

        Spotify has no call that deletes one queued song, so this also hides that
        queue slot. Otherwise an older copy of the same song would fill the gap.
        """
        with self._lock:
            visible = _without_cancellations(track_ids, self._cancellations)
            if not any(item_index == index for item_index, _item in _match_indexed(self._items, visible, limit=None)):
                raise RequestLogError("That song request is no longer waiting.")
            removed = self._items.pop(index)
            self._cancellations.append(removed.track_id)
            self._cancellations = _active_cancellations(self._cancellations, track_ids)
            self._save()
        logger.info("Removed song request %s - %s (%s).", removed.title, removed.artist, removed.viewer)
        return removed

    def _load(self) -> tuple[list[SongRequest], list[str]]:
        if not self.path.exists():
            return [], []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.error("Could not read %s. Song requests will start empty.", self.path.name)
            return [], []
        cancellations: list[str] = []
        items = raw
        if isinstance(raw, dict):
            items = raw.get("requests", [])
            stored = raw.get("cancellations", [])
            if isinstance(stored, list):
                cancellations = [item for item in stored if isinstance(item, str) and item.strip()]
        if not isinstance(items, list):
            return [], cancellations
        requests: list[SongRequest] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            track_id = item.get("track_id")
            title = item.get("title")
            artist = item.get("artist")
            viewer = item.get("viewer")
            if not all(isinstance(value, str) and value.strip() for value in (track_id, title, artist, viewer)):
                continue
            requests.append(
                SongRequest(
                    track_id=track_id.strip(),
                    title=_plain(title),
                    artist=_plain(artist),
                    viewer=_plain(viewer),
                )
            )
        return requests[-_STORE_LIMIT:], cancellations

    def _save(self) -> None:
        payload = {
            "requests": [
                {
                    "track_id": item.track_id,
                    "title": item.title,
                    "artist": item.artist,
                    "viewer": item.viewer,
                }
                for item in self._items
            ],
            "cancellations": list(self._cancellations),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)


class RequestBoard:
    """The waiting requests for the status window, refreshed from Spotify's queue."""

    def __init__(self, log: RequestLog, queue_ids: Callable[[], list[str] | None], interval: float = 3.0) -> None:
        self._log = log
        self._read_queue = queue_ids
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._current_ids: list[str] = []
        self._shown: list[tuple[int, SongRequest]] = []
        self._thread = threading.Thread(target=self._loop, name="tca-requests", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[tuple[int, str, str, str, str], ...]:
        with self._lock:
            return tuple(
                (index, item.track_id, item.title, item.artist, item.viewer) for index, item in self._shown
            )

    def remove(self, index: int) -> SongRequest:
        with self._lock:
            track_ids = list(self._current_ids)
        removed = self._log.remove_matched(track_ids, index)
        self._set_shown(track_ids, self._log.matched(track_ids))
        return removed

    def refresh(self) -> None:
        try:
            track_ids = self._read_queue()
        except Exception:
            logger.exception("Could not refresh the song request list.")
            return
        if track_ids is None:
            return
        self._set_shown(track_ids, self._log.note_queue(track_ids))

    def _set_shown(self, track_ids: list[str], shown: list[tuple[int, SongRequest]]) -> None:
        with self._lock:
            self._current_ids = list(track_ids)
            self._shown = shown

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._interval)


def match_upcoming(requests: list[SongRequest], track_ids: list[str], limit: int) -> list[SongRequest]:
    """Viewer requests that are still in Spotify's upcoming queue, in playback order."""
    return [item for _, item in _match_indexed(requests, track_ids, limit)]


def _match_indexed(
    requests: list[SongRequest], track_ids: list[str], limit: int | None
) -> list[tuple[int, SongRequest]]:
    matched: list[tuple[int, SongRequest]] = []
    used: set[int] = set()
    for track_id in track_ids:
        for index, request in enumerate(requests):
            if index in used or request.track_id != track_id:
                continue
            matched.append((index, request))
            used.add(index)
            if limit is not None and len(matched) >= limit:
                return matched
            break
    return matched


def _without_cancellations(track_ids: list[str], cancellations: list[str]) -> list[str]:
    skip = Counter(cancellations)
    kept: list[str] = []
    for track_id in track_ids:
        if skip[track_id] > 0:
            skip[track_id] -= 1
            continue
        kept.append(track_id)
    return kept


def _active_cancellations(cancellations: list[str], track_ids: list[str]) -> list[str]:
    """Cancellations that still hide a song currently in the queue."""
    skip = Counter(cancellations)
    used = Counter()
    for track_id in track_ids:
        if skip[track_id] > used[track_id]:
            used[track_id] += 1
    active: list[str] = []
    for track_id, count in used.items():
        active.extend([track_id] * count)
    return active


def format_request_queue(requests: list[SongRequest]) -> str:
    if not requests:
        return "No song requests are waiting."
    shown: list[str] = []
    for index, request in enumerate(requests, start=1):
        entry = _queue_entry(index, request)
        candidate = "Queue: " + " | ".join([*shown, entry])
        if len(candidate) > TWITCH_MESSAGE_LIMIT:
            break
        shown.append(entry)
    if not shown:
        request = requests[0]
        return f"Queue: {_queue_entry(1, request)}"
    text = "Queue: " + " | ".join(shown)
    hidden = len(requests) - len(shown)
    if hidden:
        extra = f" | and {hidden} more"
        if len(text) + len(extra) <= TWITCH_MESSAGE_LIMIT:
            text += extra
    return text


def _queue_entry(index: int, request: SongRequest) -> str:
    viewer = request.viewer.strip()
    return f"{index}. {request.title} - {request.artist} (requested by {viewer})"


def _plain(text: str) -> str:
    return " ".join(text.split())
