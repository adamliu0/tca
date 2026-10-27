"""Read the signed-in Spotify account's current track and playlist."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import requests
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import CacheFileHandler, SpotifyOAuth

from tca.replies import format_playlist, format_song, playlist_id

logger = logging.getLogger("tca")

SCOPES = "user-read-currently-playing playlist-read-private"
REDIRECT_URI = "http://127.0.0.1:8888/callback"


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

    def link_account(self) -> None:
        """Open the browser so the user can approve Spotify. Called from the Link button."""
        with self._lock:
            self._allow_browser()
            try:
                token = self.auth.get_access_token(as_dict=False, check_cache=False)
            except (SpotifyOauthError, requests.RequestException) as exc:
                raise SpotifyUnavailable(str(exc), needs_login=True) from exc
            finally:
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

    def _allow_browser(self) -> None:
        self.auth.open_browser = True

        def get_auth_response(open_browser: bool | None = None) -> str:
            return SpotifyOAuth.get_auth_response(self.auth, open_browser)

        self.auth.get_auth_response = get_auth_response

    def _block_browser(self) -> None:
        self.auth.get_auth_response = _refuse_browser_login

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
