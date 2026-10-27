"""Start TCA from the project folder."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path

from tca.bot import AUTH_PORT, AUTH_URL, Autoreply
from tca.commands import CommandFile, CommandFileError
from tca.config_store import Config, load_config, save_config, saved_twitch_user_id
from tca.spotify import REDIRECT_URI, SpotifyNow, SpotifyUnavailable
from tca.status import AppStatus
from tca.ui import StatusWindow

logger = logging.getLogger("tca")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Runtime:
    root: Path
    config_path: Path
    config: Config
    commands: CommandFile
    status: AppStatus
    spotify: SpotifyNow | None = None
    service: BotService | None = None

    def apply_startup(self) -> None:
        if self.config.twitch_ready:
            self._start_bot()
        else:
            self.status.mark_twitch_needs_app()
        if self.config.spotify_ready:
            self._use_spotify()
            spotify = self.spotify
            threading.Thread(
                target=lambda: _check_spotify(spotify, self.status),
                name="tca-spotify",
                daemon=True,
            ).start()
        else:
            self.status.mark_spotify_needs_app()

    def save_twitch(self, client_id: str, secret: str) -> None:
        self.config = replace(
            self.config,
            twitch_client_id=client_id.strip(),
            twitch_client_secret=secret.strip(),
        )
        save_config(self.config_path, self.config)
        logger.info("Twitch application saved.")
        self.status.update(
            phase="starting",
            detail="Starting…",
            twitch_state="checking",
            twitch_detail="",
            twitch_can_link=False,
        )
        self._start_bot()
        if saved_twitch_user_id(self.root / ".tio.tokens.json") is None:
            _link_twitch(self.status)

    def save_spotify(self, client_id: str, secret: str) -> None:
        self.config = replace(
            self.config,
            spotify_client_id=client_id.strip(),
            spotify_client_secret=secret.strip(),
        )
        save_config(self.config_path, self.config)
        logger.info("Spotify application saved.")
        spotify = self._use_spotify()
        threading.Thread(
            target=lambda: _finish_spotify_setup(spotify, self.status),
            name="tca-spotify-setup",
            daemon=True,
        ).start()

    def link_spotify(self) -> None:
        if self.spotify is None:
            return
        _link_spotify(self.spotify, self.status)

    def link_twitch(self) -> None:
        _link_twitch(self.status)

    def stop(self) -> None:
        if self.service is not None:
            self.service.stop()

    def _use_spotify(self) -> SpotifyNow:
        spotify = SpotifyNow(
            client_id=self.config.spotify_client_id,
            client_secret=self.config.spotify_client_secret,
            redirect_uri=REDIRECT_URI,
            cache_path=self.root / ".spotify_cache",
        )
        self.spotify = spotify
        if self.service is not None:
            self.service.attach_spotify(spotify)
        return spotify

    def _start_bot(self) -> None:
        if self.service is not None and self.service.is_running():
            return
        self.service = BotService(
            client_id=self.config.twitch_client_id,
            client_secret=self.config.twitch_client_secret,
            commands=self.commands,
            status=self.status,
            user_id=saved_twitch_user_id(self.root / ".tio.tokens.json"),
        )
        if self.spotify is not None:
            self.service.attach_spotify(self.spotify)
        self.service.start()


class BotService:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        commands: CommandFile,
        status: AppStatus,
        user_id: str | None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.commands = commands
        self.status = status
        self.user_id = user_id
        self.spotify: SpotifyNow | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._bot: Autoreply | None = None
        self._guard = threading.Lock()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def attach_spotify(self, spotify: SpotifyNow) -> None:
        with self._guard:
            self.spotify = spotify
            if self._bot is not None:
                self._bot.spotify = spotify

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="tca-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        bot = self._bot
        loop = self._loop
        if bot is not None and loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
            try:
                future.result(timeout=8)
            except Exception:
                logger.exception("Error while closing the Twitch connection")
        if self._thread is not None:
            self._thread.join(timeout=8)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            logger.exception("The chat connection stopped")
            if self.status.snapshot().phase == "running":
                self.status.mark_program_error("The chat connection stopped. Restart TCA.")
            else:
                self.status.mark_twitch_needs_app()
                self.status.update(
                    phase="error",
                    detail="Twitch did not accept the application. Check the client ID and secret, then save again.",
                )
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        with self._guard:
            spotify = self.spotify
        bot = Autoreply(
            login=self.user_id or "",
            commands=self.commands,
            spotify=spotify,
            status=self.status,
            client_id=self.client_id,
            client_secret=self.client_secret,
            bot_id=self.user_id,
        )
        with self._guard:
            self._bot = bot
            bot.spotify = self.spotify
        async with bot:
            await bot.start()


def main() -> None:
    _console_utf8()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    root = project_root()
    os.chdir(root)
    status = AppStatus()

    try:
        commands = CommandFile(root / "commands.json")
    except CommandFileError as exc:
        status.mark_program_error(str(exc))
        StatusWindow(status, lambda: None, lambda: None).run()
        return

    config_path = root / "config.json"
    runtime = Runtime(
        root=root,
        config_path=config_path,
        config=load_config(config_path),
        commands=commands,
        status=status,
    )
    runtime.apply_startup()
    window = StatusWindow(
        status,
        on_link_twitch=runtime.link_twitch,
        on_link_spotify=runtime.link_spotify,
        on_save_twitch=runtime.save_twitch,
        on_save_spotify=runtime.save_spotify,
    )
    window.prefill(
        twitch_id=runtime.config.twitch_client_id,
        twitch_secret=runtime.config.twitch_client_secret,
        spotify_id=runtime.config.spotify_client_id,
        spotify_secret=runtime.config.spotify_client_secret,
    )
    try:
        window.run()
    finally:
        runtime.stop()


def _check_spotify(spotify: SpotifyNow | None, status: AppStatus) -> None:
    if spotify is None:
        status.mark_spotify_needs_app()
        return
    try:
        linked = spotify.has_saved_login()
    except SpotifyUnavailable as exc:
        logger.error("Spotify check failed: %s", exc)
        _spotify_failure(status, exc)
        return
    if linked:
        logger.info("Spotify is signed in.")
        status.mark_spotify_connected()
    else:
        status.mark_spotify_unlinked()


def _finish_spotify_setup(spotify: SpotifyNow, status: AppStatus) -> None:
    try:
        linked = spotify.has_saved_login()
    except SpotifyUnavailable as exc:
        logger.error("Spotify check failed: %s", exc)
        _spotify_failure(status, exc)
        return
    if linked:
        logger.info("Spotify is signed in.")
        status.mark_spotify_connected()
        return
    _link_spotify(spotify, status)


def _link_spotify(spotify: SpotifyNow, status: AppStatus) -> None:
    status.mark_spotify_linking()

    def worker() -> None:
        try:
            spotify.link_account()
        except SpotifyUnavailable as exc:
            logger.error("Spotify link failed: %s", exc)
            _spotify_failure(status, exc)
            return
        except Exception:
            logger.exception("Spotify link failed")
            status.mark_spotify_error("Spotify login failed. Try again.")
            return
        logger.info("Spotify is signed in.")
        status.mark_spotify_connected()

    threading.Thread(target=worker, name="tca-spotify-link", daemon=True).start()


def _spotify_failure(status: AppStatus, exc: SpotifyUnavailable) -> None:
    if "invalid_client" in str(exc).casefold():
        status.mark_spotify_needs_app()
        status.update(spotify_detail="Spotify rejected the client ID or secret. Check them and save again.")
        return
    status.mark_spotify_error(str(exc) or "Spotify login failed. Try again.")


def _link_twitch(status: AppStatus) -> None:
    force = status.snapshot().twitch_state == "error"
    url = f"{AUTH_URL}?force_verify=true" if force else AUTH_URL

    def worker() -> None:
        if not _oauth_port_is_open():
            status.mark_twitch_link_failed("The Twitch login page is not ready yet. Try again in a moment.")
            return
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            status.mark_twitch_link_failed(f"Open {url} in a browser.")
            return
        status.note_twitch_login_open()

    threading.Thread(target=worker, name="tca-twitch-link", daemon=True).start()


def _oauth_port_is_open() -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, AUTH_PORT), timeout=0.2):
                    return True
            except OSError:
                continue
        time.sleep(0.1)
    return False


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    main()
