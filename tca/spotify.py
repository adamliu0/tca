"""Read the signed-in Spotify account's current track and playlist."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth

from tca.replies import format_playlist, format_song, playlist_id

logger = logging.getLogger("tca")

SCOPES = (
    "user-read-currently-playing user-read-playback-state "
    "playlist-read-private user-modify-playback-state"
)
# Reading the queue needs user-read-playback-state. Adding to it needs user-modify-playback-state.
PLAYBACK_SCOPES = frozenset({"user-read-playback-state", "user-modify-playback-state"})
REDIRECT_URI = "http://127.0.0.1:8888/callback"


@dataclass(frozen=True)
class _ActiveTrack:
    uri: str
    track_id: str
    progress_ms: int
    duration_ms: int | None
    is_playing: bool
    device_id: str | None
    context_uri: str | None


@dataclass(frozen=True)
class QueuedTrack:
    track_id: str
    title: str
    artist: str

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


class SpotifyUnavailable(Exception):
    """Spotify could not be asked what is playing."""

    def __init__(self, message: str, *, needs_login: bool = False) -> None:
        super().__init__(message)
        self.needs_login = needs_login


class SpotifyNow:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, cache_path: Path) -> None:
        self.auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            cache_handler=CacheFileHandler(cache_path=str(cache_path)),
            open_browser=True,
        )
        self.client = spotipy.Spotify(auth_manager=self.auth, requests_timeout=10)
        self._lock = threading.Lock()
        # A chat command must not open a browser or wait for keyboard input.
        self._block_browser()

    def has_saved_login(self) -> bool:
        with self._lock:
            try:
                token_info = self.auth.validate_token(self.auth.cache_handler.get_cached_token())
            except (SpotifyOauthError, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            return bool(token_info)

    def has_cached_login(self) -> bool:
        with self._lock:
            cached = self.auth.cache_handler.get_cached_token()
        return isinstance(cached, dict) and bool(cached.get("refresh_token"))

    def has_queue_permission(self) -> bool:
        """True when the saved login can add a song to the queue."""
        with self._lock:
            cached = self.auth.cache_handler.get_cached_token()
        if not isinstance(cached, dict):
            return False
        return grants_queue_access(cached.get("scope"))

    def link_account(self) -> None:
        """Open the browser so the user can approve Spotify. Called from the Link button."""
        with self._lock:
            self._allow_browser()
            previous_dialog = self.auth.show_dialog
            # Always show the approval page so a new permission, such as queueing, is actually granted.
            self.auth.show_dialog = True
            try:
                token = self.auth.get_access_token(as_dict=False, check_cache=False)
            except (SpotifyOauthError, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            finally:
                self.auth.show_dialog = previous_dialog
                self._block_browser()
            if not token:
                raise SpotifyUnavailable("Spotify login did not return a token.", needs_login=True)

    def song_reply(self) -> str:
        return format_song(self._current())

    def playlist_reply(self) -> str:
        playback = self._current()
        name = None
        playlist = playlist_id(playback)
        if playlist:
            name = self._playlist_name(playlist)
        return format_playlist(playback, name)

    def queue_track(self, track_id: str) -> QueuedTrack:
        """Add one track to Spotify's queue without replacing the song that is playing."""
        uri = f"spotify:track:{track_id}"
        with self._lock:
            try:
                data = self.client.track(uri)
            except SpotifyOauthError as exc:
                raise SpotifyUnavailable(
                    "Spotify needs to be linked again before a song can be queued.",
                    needs_login=True,
                ) from exc
            except SpotifyException as exc:
                raise _queue_problem(exc, missing="Spotify couldn't find that song.") from exc
            except requests.RequestException as exc:
                raise SpotifyUnavailable("Spotify isn't available right now.") from exc
            if not isinstance(data, dict):
                raise SpotifyUnavailable("Spotify couldn't find that song.")
            artist, title = _track_names(data)
            self._queue_after_current(uri, track_id)
        return QueuedTrack(track_id=track_id, title=title, artist=artist)

    def upcoming_track_ids(self) -> list[str]:
        """Track ids still waiting in the Spotify queue, in playback order."""
        with self._lock:
            try:
                payload = self.client.queue()
            except SpotifyOauthError as exc:
                raise SpotifyUnavailable(
                    "Spotify needs to be linked again before the queue can be read.",
                    needs_login=True,
                ) from exc
            except SpotifyException as exc:
                if exc.http_status in {401, 403}:
                    raise SpotifyUnavailable(
                        "Link Spotify again so the song queue can be read.",
                        needs_login=True,
                    ) from exc
                raise _queue_problem(exc, missing="Spotify needs something playing before the queue can be read.") from exc
            except requests.RequestException as exc:
                raise SpotifyUnavailable("Spotify isn't available right now.") from exc
        if not isinstance(payload, dict):
            raise SpotifyUnavailable("Spotify returned an unexpected queue.")
        queue = payload.get("queue")
        if not isinstance(queue, list):
            return []
        ids: list[str] = []
        for item in queue:
            if not isinstance(item, dict) or item.get("type") not in (None, "track"):
                continue
            track_id = item.get("id")
            if isinstance(track_id, str) and track_id:
                ids.append(track_id)
        return ids

    def _allow_browser(self) -> None:
        self.auth.open_browser = True

        def get_auth_response(open_browser: bool | None = None) -> str:
            return SpotifyOAuth.get_auth_response(self.auth, open_browser)

        self.auth.get_auth_response = get_auth_response

    def _block_browser(self) -> None:
        self.auth.get_auth_response = _refuse_browser_login

    def _queue_after_current(self, requested_uri: str, requested_id: str) -> None:
        """Add the request to Spotify's queue without replacing the playlist.

        Adding while playback is running can make Spotify skip straight to the request.
        Pausing first, adding, then resuming leaves the current song in place.
        """
        active = self._active_track()
        if active is None or not active.is_playing:
            self._add_to_queue(requested_uri)
            return
        paused = False
        try:
            self.client.pause_playback(device_id=active.device_id)
            paused = True
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.info("Could not pause before queueing.")
        try:
            self._add_to_queue(requested_uri)
        finally:
            if paused:
                self._resume_current(active, requested_uri)
        self._undo_skip(active, requested_id)

    def _resume_current(self, active: _ActiveTrack, requested_uri: str) -> None:
        try:
            self.client.start_playback(device_id=active.device_id)
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.warning("Could not resume the current song after queueing.")
            self._restore_context(active, requested_uri)
            return
        try:
            self.client.seek_track(active.progress_ms, device_id=active.device_id)
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.info("Resumed the current song, but not at the same moment.")

    def _restore_context(self, active: _ActiveTrack, requested_uri: str) -> None:
        if not active.context_uri:
            logger.warning("Spotify stayed stopped after the song request.")
            return
        try:
            self.client.start_playback(
                device_id=active.device_id,
                context_uri=active.context_uri,
                offset={"uri": active.uri},
                position_ms=active.progress_ms,
            )
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.warning("Spotify stayed stopped after the song request.")
            return
        try:
            self.client.add_to_queue(requested_uri)
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.warning("Playback resumed, but the song request was not queued.")

    def _undo_skip(self, active: _ActiveTrack, requested_id: str) -> None:
        """If Spotify still jumped to the request, go back while that song is under three seconds in."""
        deadline = time.monotonic() + 2.0
        saw_original = False
        saw_stopped = False
        while time.monotonic() < deadline:
            time.sleep(0.25)
            now = self._active_track()
            if now is None or not now.is_playing:
                saw_stopped = True
                continue
            if now.track_id == requested_id and now.progress_ms < 3500:
                try:
                    self.client.previous_track(device_id=active.device_id)
                except (SpotifyOauthError, SpotifyException, requests.RequestException):
                    logger.warning("The request started immediately and could not be moved back.")
                else:
                    logger.info("Spotify started the request immediately. Returned to the song that was playing.")
                return
            if now.track_id == active.track_id:
                saw_original = True
        if saw_original:
            return
        if saw_stopped:
            logger.warning("Playback stopped after the song request. Restoring the playlist.")
            self._restore_context(active, f"spotify:track:{requested_id}")

    def _add_to_queue(self, uri: str) -> None:
        try:
            self.client.add_to_queue(uri)
        except SpotifyOauthError as exc:
            raise SpotifyUnavailable(
                "Spotify needs to be linked again before a song can be queued.",
                needs_login=True,
            ) from exc
        except SpotifyException as exc:
            raise _queue_problem(
                exc,
                missing="Spotify needs something playing before a song can be queued.",
            ) from exc
        except requests.RequestException as exc:
            raise SpotifyUnavailable("Spotify isn't available right now.") from exc

    def _active_track(self) -> _ActiveTrack | None:
        try:
            playback = self.client.current_playback(additional_types="episode")
        except (SpotifyOauthError, SpotifyException, requests.RequestException):
            logger.info("Could not read the current song before queueing.")
            return None
        if not isinstance(playback, dict):
            return None
        item = playback.get("item")
        if not isinstance(item, dict) or item.get("type") not in (None, "track"):
            return None
        uri = item.get("uri")
        track_id = item.get("id")
        if not isinstance(uri, str) or not uri or not isinstance(track_id, str) or not track_id:
            return None
        progress = playback.get("progress_ms")
        duration = item.get("duration_ms")
        device = playback.get("device")
        device_id = device.get("id") if isinstance(device, dict) else None
        context = playback.get("context")
        context_uri = context.get("uri") if isinstance(context, dict) else None
        return _ActiveTrack(
            uri=uri,
            track_id=track_id,
            progress_ms=progress if isinstance(progress, int) and progress >= 0 else 0,
            duration_ms=duration if isinstance(duration, int) and duration > 0 else None,
            is_playing=bool(playback.get("is_playing")),
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            context_uri=context_uri if isinstance(context_uri, str) and context_uri else None,
        )

    def _current(self) -> dict | None:
        with self._lock:
            try:
                playback = self.client.currently_playing(additional_types="episode")
            except SpotifyOauthError as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            except (SpotifyException, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc)) from exc
        if playback is None or isinstance(playback, dict):
            return playback
        raise SpotifyUnavailable("Spotify returned an unexpected playback payload.")

    def _playlist_name(self, playlist: str) -> str | None:
        with self._lock:
            return self._playlist_name_unlocked(playlist)

    def _playlist_name_unlocked(self, playlist: str) -> str | None:
        try:
            data = self.client.playlist(playlist, fields="name")
        except SpotifyException as exc:
            logger.info("Spotify did not return a name for playlist %s (%s).", playlist, exc.http_status)
            return None
        except requests.RequestException:
            logger.info("Spotify could not be reached for the playlist name.")
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None


def _refuse_browser_login(open_browser: bool | None = None) -> str:
    raise SpotifyUnavailable("Spotify is not linked. Use Link Spotify in the TCA window.", needs_login=True)


def grants_queue_access(scope: object) -> bool:
    return PLAYBACK_SCOPES <= _scope_names(scope)


def _scope_names(scope: object) -> set[str]:
    if isinstance(scope, str):
        return set(scope.split())
    if isinstance(scope, list):
        return {item for item in scope if isinstance(item, str)}
    return set()


def _track_names(item: dict) -> tuple[str, str]:
    artists = ", ".join(
        artist["name"]
        for artist in item.get("artists") or []
        if isinstance(artist, dict) and isinstance(artist.get("name"), str) and artist["name"].strip()
    ) or "Unknown artist"
    title = item.get("name")
    if not isinstance(title, str) or not title.strip():
        title = "Unknown title"
    return artists, title.strip()


def _queue_problem(exc: SpotifyException, *, missing: str) -> SpotifyUnavailable:
    reason = f"{getattr(exc, 'reason', '')} {exc}".casefold()
    if exc.http_status == 401 or "scope" in reason or "permission" in reason:
        return SpotifyUnavailable("Spotify needs to be linked again before a song can be queued.", needs_login=True)
    if "premium" in reason:
        return SpotifyUnavailable("Spotify Premium is required to queue a song.")
    if exc.http_status == 404:
        return SpotifyUnavailable(missing)
    return SpotifyUnavailable("Spotify isn't available right now.")
