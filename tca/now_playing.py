"""Now-playing card for the status window and an OBS browser source."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests

from tca.request_log import RequestLog
from tca.spotify import SpotifyNow, SpotifyUnavailable, album_image_url

logger = logging.getLogger("tca")

WIDGET_HOST = "127.0.0.1"
WIDGET_PORT = 8756
WIDGET_URL = f"http://{WIDGET_HOST}:{WIDGET_PORT}/"
PREVIEW_URL = f"{WIDGET_URL}?preview=1"
_ARTWORK_LIMIT = 2_000_000
_IMAGE_HOSTS = {"i.scdn.co", "mosaic.scdn.co"}


@dataclass(frozen=True)
class PlayingTrack:
    track_id: str
    title: str
    artist: str
    image_url: str | None
    progress_ms: int
    duration_ms: int
    is_playing: bool
    artwork: bytes | None = None


@dataclass(frozen=True)
class NowPlaying:
    track_id: str
    title: str
    artist: str
    requester: str | None
    image_url: str | None
    progress_ms: int
    duration_ms: int
    is_playing: bool
    polled_at_ms: int


def track_from_playback(playback: dict | None) -> PlayingTrack | None:
    """A music track Spotify is on, including while paused. Ads and podcasts are skipped."""
    if not isinstance(playback, dict):
        return None
    if playback.get("currently_playing_type") in {"ad", "episode"}:
        return None
    item = playback.get("item")
    if not isinstance(item, dict) or item.get("type") not in (None, "track"):
        return None
    title = item.get("name")
    if not isinstance(title, str) or not title.strip():
        title = "Unknown title"
    artists = ", ".join(
        artist["name"]
        for artist in item.get("artists") or []
        if isinstance(artist, dict) and isinstance(artist.get("name"), str) and artist["name"].strip()
    ) or "Unknown artist"
    track_id = item.get("id")
    if not isinstance(track_id, str):
        track_id = ""
    progress = playback.get("progress_ms")
    duration = item.get("duration_ms")
    return PlayingTrack(
        track_id=track_id,
        title=title.strip(),
        artist=artists,
        image_url=album_image_url(item.get("album")),
        progress_ms=progress if isinstance(progress, int) and progress >= 0 else 0,
        duration_ms=duration if isinstance(duration, int) and duration > 0 else 0,
        is_playing=bool(playback.get("is_playing")),
    )


def snapshot_payload(snapshot: NowPlaying | None) -> dict[str, object]:
    if snapshot is None:
        return {}
    return {
        "title": snapshot.title,
        "artist": snapshot.artist,
        "requester": snapshot.requester,
        "track_id": snapshot.track_id,
        "progress_ms": snapshot.progress_ms,
        "duration_ms": snapshot.duration_ms,
        "is_playing": snapshot.is_playing,
        "polled_at_ms": snapshot.polled_at_ms,
        "artwork_url": snapshot.image_url,
    }


def artwork_request_allowed(requested: str | None, current: str | None) -> bool:
    """True only for the current track's own Spotify image."""
    if not requested or not current or requested != current:
        return False
    parsed = urlsplit(requested)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not parsed.path:
        return False
    if host in _IMAGE_HOSTS or host.endswith(".scdn.co"):
        return True
    return host == "spotifycdn.com" or host.endswith(".spotifycdn.com")


def widget_page() -> str:
    return _page_path().read_text(encoding="utf-8")


def widget_response(path: str, feed: NowPlayingFeed) -> tuple[int, str, bytes]:
    if path in {"/", "/index.html"}:
        return 200, "text/html; charset=utf-8", widget_page().encode("utf-8")
    if path == "/now-playing.json":
        body = json.dumps(feed.payload()).encode("utf-8")
        return 200, "application/json; charset=utf-8", body
    if path == "/artwork":
        image = feed.artwork()
        if image is None:
            return 404, "text/plain; charset=utf-8", b""
        body, kind = image
        return 200, kind, body
    return 404, "text/plain; charset=utf-8", b""


class NowPlayingFeed:
    """Polls Spotify and keeps the latest card. OBS reads the cache instead of Spotify."""

    def __init__(
        self,
        spotify: Callable[[], SpotifyNow | None],
        requests_log: RequestLog,
        *,
        interval: float = 3.0,
        clock: Callable[[], float] = time.time,
        fetch: Callable[[str], tuple[bytes, str] | None] | None = None,
    ) -> None:
        self._spotify = spotify
        self._requests = requests_log
        self._interval = interval
        self._clock = clock
        self._fetch = fetch or _download_image
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._snapshot: NowPlaying | None = None
        self._art_url: str | None = None
        self._art_body: bytes | None = None
        self._art_type = "image/jpeg"
        self._thread = threading.Thread(target=self._loop, name="tca-now-playing", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def payload(self) -> dict[str, object]:
        with self._lock:
            return snapshot_payload(self._snapshot)

    def artwork(self) -> tuple[bytes, str] | None:
        with self._lock:
            current = None if self._snapshot is None else self._snapshot.image_url
            if current and current == self._art_url and self._art_body is not None:
                return self._art_body, self._art_type
        if not artwork_request_allowed(current, current):
            return None
        downloaded = self._fetch(current)
        if downloaded is None:
            return None
        body, kind = downloaded
        with self._lock:
            still = None if self._snapshot is None else self._snapshot.image_url
            if still != current:
                return None
            self._art_url = current
            self._art_body = body
            self._art_type = kind
            return body, kind

    def refresh(self) -> None:
        spotify = self._spotify()
        if spotify is None:
            self._publish(None)
            return
        try:
            track = spotify.playing_track()
        except SpotifyUnavailable as exc:
            if exc.needs_login:
                self._publish(None)
            logger.debug("Spotify could not refresh the song widget.")
            return
        # progress_ms is the position at this instant. The queue read below can
        # take a second or two, and that delay changes every poll.
        polled_at_ms = int(self._clock() * 1000)
        requester = None
        if track is not None:
            try:
                queue = spotify.upcoming_track_ids()
            except SpotifyUnavailable:
                logger.debug("Spotify could not read the queue for the song widget.")
                queue = []
            else:
                self._requests.remember_waiting(queue)
                requester = self._requests.current_viewer(track.track_id or None, queue)
        if track is None:
            self._publish(None)
            return
        artwork = None
        if track.artwork and track.image_url:
            artwork = (track.artwork, _image_type(track.artwork))
        self._publish(
            NowPlaying(
                track_id=track.track_id,
                title=track.title,
                artist=track.artist,
                requester=requester,
                image_url=track.image_url,
                progress_ms=track.progress_ms,
                duration_ms=track.duration_ms,
                is_playing=track.is_playing,
                polled_at_ms=polled_at_ms,
            ),
            artwork,
        )

    def _publish(self, snapshot: NowPlaying | None, artwork: tuple[bytes, str] | None = None) -> None:
        with self._lock:
            self._snapshot = snapshot
            if artwork is not None and snapshot is not None and snapshot.image_url:
                self._art_url = snapshot.image_url
                self._art_body, self._art_type = artwork
            elif snapshot is None or snapshot.image_url != self._art_url:
                self._art_url = None
                self._art_body = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._interval)


class WidgetSite:
    """The fixed localhost page OBS and the status window both load."""

    def __init__(self, feed: NowPlayingFeed) -> None:
        self.feed = feed
        self.url = WIDGET_URL
        self.preview_url = PREVIEW_URL
        self.error: str | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._httpd = ThreadingHTTPServer((WIDGET_HOST, WIDGET_PORT), self._handler())
        except OSError:
            self.error = "Port 8756 is already in use. The song widget needs that address."
            logger.info(self.error)
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="tca-widget", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server = self._httpd
        if server is None:
            return
        self._httpd = None
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        feed = self.feed

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, content_type, body = widget_response(urlsplit(self.path).path, feed)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _download_image(url: str) -> tuple[bytes, str] | None:
    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
    except requests.RequestException:
        logger.info("Spotify did not return the album picture.")
        return None
    if response.status_code != 200:
        return None
    kind = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
    if not kind.startswith("image/"):
        return None
    body = response.content
    if not body or len(body) > _ARTWORK_LIMIT:
        return None
    return body, kind


def _page_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "assets" / "now-playing.html"
    return Path(__file__).resolve().parent.parent / "assets" / "now-playing.html"
