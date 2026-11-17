"""Read the signed-in Spotify account's current track and playlist."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import requests
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth

from tca.media_session import desktop_spotify
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
        self._signed_out = False
        self._desktop_artwork: bytes | None = None
        self._desktop = desktop_spotify
        self._logged_desktop = False
        # A chat command must not open a browser or wait for keyboard input.
        self._block_browser()

    def has_saved_login(self) -> bool:
        with self._lock:
            if self._signed_out:
                return False
            try:
                token_info = self.auth.validate_token(self.auth.cache_handler.get_cached_token())
            except (SpotifyOauthError, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            return bool(token_info)

    def forget_login(self) -> None:
        """Drop the saved Spotify sign-in. The app credentials stay."""
        with self._lock:
            self._signed_out = True
            path = Path(self.auth.cache_handler.cache_path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.info("Could not remove the saved Spotify login.")

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
            self._signed_out = False
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

    def playing_track(self):
        """The track Spotify is on right now, or nothing for an ad, a podcast, or silence."""
        from tca.now_playing import track_from_playback

        track = track_from_playback(self._current())
        if track is None or track.image_url:
            return track
        with self._lock:
            artwork = self._desktop_artwork
        if not artwork:
            return track
        return replace(track, artwork=artwork, image_url=f"local://desktop/{track.track_id}")

    def playlist_reply(self) -> str:
        playback = self._current()
        name = None
        # A paused or finished session still carries the last playlist. Only look
        # up a name when that playlist is the one playing right now.
        if playback and playback.get("is_playing") is True:
            playlist = playlist_id(playback)
            if playlist:
                name = self._playlist_name(playlist)
        return format_playlist(playback, name)

    def queue_track(self, track_id: str) -> QueuedTrack:
        """Add one track to Spotify's queue. The song already playing keeps going."""
        uri = f"spotify:track:{track_id}"
        with self._lock:
            if self._signed_out:
                raise SpotifyUnavailable(
                    "Spotify needs to be linked again before a song can be queued.",
                    needs_login=True,
                )
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
            self._add_to_queue(uri)
        return QueuedTrack(track_id=track_id, title=title, artist=artist)

    def upcoming_track_ids(self) -> list[str]:
        """Track ids still waiting in the Spotify queue, in playback order."""
        return self.upcoming_queue()[0]

    def upcoming_queue(self) -> tuple[list[str], dict[str, str]]:
        """Queue track ids, and an album-art URL for each id Spotify included."""
        with self._lock:
            if self._signed_out:
                raise SpotifyUnavailable(
                    "Spotify needs to be linked again before the queue can be read.",
                    needs_login=True,
                )
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
        return queue_listing(payload)

    def _allow_browser(self) -> None:
        self.auth.open_browser = True

        def get_auth_response(open_browser: bool | None = None) -> str:
            return SpotifyOAuth.get_auth_response(self.auth, open_browser)

        self.auth.get_auth_response = get_auth_response

    def _block_browser(self) -> None:
        self.auth.get_auth_response = _refuse_browser_login

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

    def _current(self) -> dict | None:
        with self._lock:
            if self._signed_out:
                raise SpotifyUnavailable("Spotify is not linked. Use Link Spotify in the TCA window.", needs_login=True)
            try:
                playback = self._read_api()
            except SpotifyOauthError as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            except (SpotifyException, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc)) from exc
            if _reports_track(playback):
                self._desktop_artwork = None
                self._logged_desktop = False
                return playback
        desktop = self._desktop()
        if desktop is not None:
            with self._lock:
                self._desktop_artwork = desktop.artwork
                if not self._logged_desktop:
                    self._logged_desktop = True
                    logger.info("Spotify's API did not report a song. Using the Spotify app on this PC.")
            return desktop.playback
        if playback is None or isinstance(playback, dict):
            return playback
        raise SpotifyUnavailable("Spotify returned an unexpected playback payload.")

    def _read_api(self) -> dict | None:
        """Prefer the player endpoint, then the currently-playing endpoint."""
        first_error: Exception | None = None
        empty: dict | None = None
        for call in (
            lambda: self.client.current_playback(additional_types="episode"),
            lambda: self.client.currently_playing(additional_types="episode"),
        ):
            try:
                playback = call()
            except (SpotifyOauthError, SpotifyException, requests.RequestException) as exc:
                first_error = first_error or exc
                continue
            if not isinstance(playback, dict):
                continue
            if _reports_track(playback) or playback.get("currently_playing_type") == "ad":
                return playback
            empty = playback
        if first_error is not None and empty is None:
            raise first_error
        return empty

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


def _reports_track(playback: object) -> bool:
    return isinstance(playback, dict) and isinstance(playback.get("item"), dict)


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


def album_image_url(album: object) -> str | None:
    """A Spotify album image large enough for a card, preferring the smaller sharp one."""
    if not isinstance(album, dict):
        return None
    images = album.get("images")
    if not isinstance(images, list):
        return None
    usable: list[tuple[int, str]] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not isinstance(url, str) or not url:
            continue
        width = image.get("width")
        usable.append((width if isinstance(width, int) and width > 0 else 0, url))
    if not usable:
        return None
    large = [item for item in usable if item[0] >= 300]
    if large:
        large.sort(key=lambda item: item[0])
        return large[0][1]
    usable.sort(key=lambda item: item[0], reverse=True)
    return usable[0][1]


def queue_listing(payload: object) -> tuple[list[str], dict[str, str]]:
    """Track ids in queue order, with album art URLs when Spotify sent them."""
    if not isinstance(payload, dict):
        return [], {}
    queue = payload.get("queue")
    if not isinstance(queue, list):
        return [], {}
    ids: list[str] = []
    images: dict[str, str] = {}
    for item in queue:
        if not isinstance(item, dict) or item.get("type") not in (None, "track"):
            continue
        track_id = item.get("id")
        if not isinstance(track_id, str) or not track_id:
            continue
        ids.append(track_id)
        image = album_image_url(item.get("album"))
        if image:
            images[track_id] = image
    return ids, images


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
