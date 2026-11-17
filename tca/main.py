"""Start TCA from the project folder."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import socket
import struct
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path

from tca.bot import AUTH_PORT, AUTH_URL, Autoreply
from tca.commands import CommandFile, CommandFileError
from tca.config_store import Config, load_config, save_config, saved_twitch_user_id
from tca.now_playing import NowPlayingFeed, WidgetSite
from tca.request_log import RequestBoard, RequestLog
from tca.spotify import REDIRECT_URI, SpotifyNow, SpotifyUnavailable
from tca.status import AppStatus
from tca.ui import StatusWindow

logger = logging.getLogger("tca")


def project_root() -> Path:
    """Folder for config and saved logins. Next to TCA.exe when the app is packaged."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class Runtime:
    root: Path
    config_path: Path
    config: Config
    commands: CommandFile
    requests: RequestLog
    status: AppStatus
    request_board: RequestBoard | None = None
    widget_feed: NowPlayingFeed | None = None
    widget: WidgetSite | None = None
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
        client_id = client_id.strip()
        secret = secret.strip()
        changed = (
            client_id != self.config.twitch_client_id or secret != self.config.twitch_client_secret
        )
        self.config = replace(
            self.config,
            twitch_client_id=client_id,
            twitch_client_secret=secret,
        )
        save_config(self.config_path, self.config)
        logger.info("Twitch application saved.")
        # Saving the same app used to force "Checking…" and then leave it there,
        # because the chat connection was already running and nothing cleared it.
        threading.Thread(
            target=self._apply_saved_twitch,
            args=(changed,),
            name="tca-twitch-save",
            daemon=True,
        ).start()

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
        threading.Thread(target=self._link_twitch_worker, name="tca-twitch-link", daemon=True).start()

    def unlink_twitch(self) -> None:
        self.status.update(
            phase="waiting",
            detail="Twitch is unlinked.",
            twitch_state="checking",
            twitch_detail="Unlinking…",
            twitch_can_link=False,
        )
        threading.Thread(target=self._unlink_twitch, name="tca-twitch-unlink", daemon=True).start()

    def unlink_spotify(self) -> None:
        self.status.update(spotify_state="checking", spotify_detail="Unlinking…", spotify_can_link=False)
        threading.Thread(target=self._unlink_spotify, name="tca-spotify-unlink", daemon=True).start()

    def _unlink_twitch(self) -> None:
        logger.info("Twitch unlinked.")
        with self._twitch_guard():
            self._stop_twitch(drop_login=True)
            if not self.config.twitch_ready:
                self.status.mark_twitch_needs_app()
                return
            # The replacement has to own the login port. Starting it while the
            # old page is still bound leaves approval pending forever.
            self._start_bot()
            service = self.service
            ready = service is not None and service.wait_until_oauth(15)
        if not ready and self.status.snapshot().twitch_state == "checking":
            self.status.mark_twitch_error("Twitch did not finish unlinking. Try Link Twitch again.", can_link=True)

    def _apply_saved_twitch(self, changed: bool) -> None:
        with self._twitch_guard():
            service = self.service
            running = service is not None and service.is_running()
            serving = bool(running and service is not None and service.accepts_oauth())
            has_user = saved_twitch_user_id(self.root / ".tio.tokens.json") is not None
            action = twitch_save_action(changed=changed, running=running, serving=serving, has_user=has_user)
            if action == "keep":
                return
            if action == "open":
                _open_twitch_login(self.status)
                return
            self.status.update(
                phase="starting",
                detail="Starting…",
                twitch_state="checking",
                twitch_detail="",
                twitch_can_link=False,
            )
            self._stop_twitch(drop_login=False)
            if not self.config.twitch_ready:
                self.status.mark_twitch_needs_app()
                return
            self._start_bot()
            service = self.service
            ready = service is not None and service.wait_until_oauth(15)
            has_user = saved_twitch_user_id(self.root / ".tio.tokens.json") is not None
        if not ready:
            if self.status.snapshot().twitch_state == "checking":
                self.status.mark_twitch_link_failed("The Twitch login page is not ready yet. Try again in a moment.")
            return
        if not has_user:
            _open_twitch_login(self.status)

    def _link_twitch_worker(self) -> None:
        with self._twitch_guard():
            service = self.service
            running = service is not None and service.is_running()
            serving = bool(running and service is not None and service.accepts_oauth())
            if twitch_link_action(running=running, serving=serving) == "restart":
                self._stop_twitch(drop_login=False)
                if not self.config.twitch_ready:
                    self.status.mark_twitch_needs_app()
                    return
                self._start_bot()
                service = self.service
                if service is None or not service.wait_until_oauth(15):
                    self.status.mark_twitch_link_failed(
                        "The Twitch login page is not ready yet. Try again in a moment."
                    )
                    return
        _open_twitch_login(self.status)

    def _twitch_guard(self) -> threading.RLock:
        guard = getattr(self, "_twitch_lock", None)
        if guard is None:
            guard = threading.RLock()
            self._twitch_lock = guard
        return guard

    def _stop_twitch(self, *, drop_login: bool) -> None:
        service = self.service
        self.service = None
        if service is not None:
            service.stop(save_tokens=not drop_login)
        if not drop_login:
            return
        token_path = self.root / ".tio.tokens.json"
        try:
            token_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove the Twitch login.")

    def _unlink_spotify(self) -> None:
        logger.info("Spotify unlinked.")
        spotify = self.spotify
        if spotify is not None:
            spotify.forget_login()
        self.status.mark_spotify_unlinked()

    def queue_ids(self) -> tuple[list[str], dict[str, str]] | None:
        spotify = self.spotify
        if spotify is None:
            return [], {}
        try:
            return spotify.upcoming_queue()
        except SpotifyUnavailable:
            return None

    def start_request_board(self) -> None:
        self.request_board = RequestBoard(self.requests, self.queue_ids)

    def start_widget(self) -> None:
        self.widget_feed = NowPlayingFeed(lambda: self.spotify, self.requests)
        self.widget_feed.start()
        self.widget = WidgetSite(self.widget_feed)
        self.widget.start()

    def stop(self) -> None:
        if self.widget is not None:
            self.widget.stop()
        if self.widget_feed is not None:
            self.widget_feed.stop()
        if self.request_board is not None:
            self.request_board.stop()
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
            requests=self.requests,
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
        requests: RequestLog,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.commands = commands
        self.status = status
        self.user_id = user_id
        self.requests = requests
        self.spotify: SpotifyNow | None = None
        self._expect_stop = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._bot: Autoreply | None = None
        self._guard = threading.Lock()
        self._oauth_gate = threading.Event()
        self._oauth_ok = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def accepts_oauth(self) -> bool:
        """The sign-in page is up, so a browser approval can reach this connection."""
        return self._oauth_ok and self.is_running()

    def wait_until_oauth(self, timeout: float) -> bool:
        if not self._oauth_gate.wait(timeout):
            return False
        return self._oauth_ok and self.is_running()

    def _note_oauth(self, serving: bool) -> None:
        self._oauth_ok = serving
        self._oauth_gate.set()

    def attach_spotify(self, spotify: SpotifyNow) -> None:
        with self._guard:
            self.spotify = spotify
            if self._bot is not None:
                self._bot.spotify = spotify

    def start(self) -> None:
        self._oauth_ok = False
        self._oauth_gate.clear()
        self._thread = threading.Thread(target=self._thread_main, name="tca-bot", daemon=True)
        self._thread.start()

    def stop(self, *, save_tokens: bool = True) -> None:
        self._expect_stop = True
        bot = self._bot
        loop = self._loop
        if bot is not None and loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close_bot(bot, save_tokens=save_tokens), loop)
            try:
                future.result(timeout=8)
            except Exception:
                logger.exception("Error while closing the Twitch connection")
        if self._thread is not None:
            self._thread.join(timeout=8)
        if self._thread is not None and self._thread.is_alive() and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
            self._thread.join(timeout=3)
        if not _wait_until_port_free(AUTH_PORT, 5):
            logger.error("The Twitch login port is still in use.")

    async def _close_bot(self, bot: Autoreply, *, save_tokens: bool) -> None:
        if not save_tokens:
            for user_id in list(bot.tokens):
                try:
                    await bot.remove_token(user_id)
                except Exception:
                    logger.exception("Could not drop a Twitch login")
        await bot.close(save_tokens=save_tokens)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            if self._expect_stop:
                return
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
            if not self._oauth_gate.is_set():
                self._note_oauth(False)
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
            requests=self.requests,
            report_oauth=self._note_oauth,
        )
        with self._guard:
            self._bot = bot
            bot.spotify = self.spotify
        async with bot:
            await bot.start()


def main() -> None:
    multiprocessing.freeze_support()
    root = project_root()
    os.chdir(root)
    _console_utf8()
    _configure_logging(root)
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
        requests=RequestLog(root / ".song_requests.json"),
        status=status,
    )
    runtime.apply_startup()
    runtime.start_request_board()
    runtime.start_widget()
    window = StatusWindow(
        status,
        on_link_twitch=runtime.link_twitch,
        on_link_spotify=runtime.link_spotify,
        on_unlink_twitch=runtime.unlink_twitch,
        on_unlink_spotify=runtime.unlink_spotify,
        on_save_twitch=runtime.save_twitch,
        on_save_spotify=runtime.save_spotify,
        commands=runtime.commands,
        requests=runtime.request_board,
        widget=runtime.widget,
        widget_cache=runtime.root / ".webview",
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
        _note_spotify_permissions(spotify, status)
    else:
        _spotify_signed_out(spotify, status)


def _finish_spotify_setup(spotify: SpotifyNow, status: AppStatus) -> None:
    try:
        linked = spotify.has_saved_login()
    except SpotifyUnavailable as exc:
        logger.error("Spotify check failed: %s", exc)
        _spotify_failure(status, exc)
        return
    if linked:
        _note_spotify_permissions(spotify, status)
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
        _note_spotify_permissions(spotify, status)

    threading.Thread(target=worker, name="tca-spotify-link", daemon=True).start()


def _spotify_failure(status: AppStatus, exc: SpotifyUnavailable) -> None:
    if "invalid_client" in str(exc).casefold():
        status.mark_spotify_needs_app()
        status.update(spotify_detail="Spotify rejected the client ID or secret. Check them and save again.")
        return
    status.mark_spotify_error(str(exc) or "Spotify login failed. Try again.")


def _spotify_signed_out(spotify: SpotifyNow, status: AppStatus) -> None:
    if spotify.has_cached_login() and not spotify.has_queue_permission():
        status.mark_spotify_unlinked("Link Spotify again so the song queue can be read.")
        return
    status.mark_spotify_unlinked()


def _note_spotify_permissions(spotify: SpotifyNow, status: AppStatus) -> None:
    if spotify.has_queue_permission():
        logger.info("Spotify is signed in.")
        status.mark_spotify_connected()
        return
    logger.info("Spotify is signed in without permission to read the queue.")
    status.mark_spotify_connected()
    status.note_spotify_reauth("Link Spotify again so the song queue can be read.")


def twitch_save_action(*, changed: bool, running: bool, serving: bool, has_user: bool) -> str:
    """What saving the Twitch app should do to the connection already running.

    ``keep`` leaves a live login as it is. ``open`` asks for approval on the
    login page that is already up. ``restart`` replaces the connection first.
    """
    if changed or not running or not serving:
        return "restart"
    if not has_user:
        return "open"
    return "keep"


def twitch_link_action(*, running: bool, serving: bool) -> str:
    """Link uses the login page that is already up, or starts a new one."""
    if running and serving:
        return "open"
    return "restart"


def _open_twitch_login(status: AppStatus) -> None:
    # Always ask Twitch to show the approval page. A silent redirect after an
    # unlink can be answered by a login page that is already shutting down.
    url = f"{AUTH_URL}?force_verify=true"

    def worker() -> None:
        if not _wait_until_port_open(AUTH_PORT, 15):
            status.mark_twitch_link_failed("The Twitch login page is not ready yet. Try again in a moment.")
            return
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            status.mark_twitch_link_failed(f"Open {url} in a browser.")
            return
        status.note_twitch_login_open()

    threading.Thread(target=worker, name="tca-twitch-browser", daemon=True).start()


def _port_is_open(port: int) -> bool:
    # A connection probe sits in the listen backlog until the server accepts it.
    # After an unlink that backlog belongs to the page that is shutting down,
    # and a full queue looks the same as a closed port.
    if sys.platform == "win32":
        listening = _windows_port_is_listening(port)
        if listening is not None:
            return listening
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            continue
    return False


def _windows_port_is_listening(port: int) -> bool | None:
    """True when a TCP listener owns ``port``. None when the table cannot be read."""
    v4 = _tcp_table(socket.AF_INET, 0)
    v6 = _tcp_table(socket.AF_INET6, 3)
    if v4 is None or v6 is None:
        return None
    return _table_has_port(v4, port, row_size=20, port_offset=8) or _table_has_port(
        v6, port, row_size=56, port_offset=20
    )


def _tcp_table(family: int, table_class: int) -> bytes | None:
    import ctypes
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong
    size = wintypes.DWORD(0)
    for _attempt in range(3):
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family, table_class, 0)
        if size.value <= 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family, table_class, 0)
        if result == 0:
            return buf.raw[: size.value]
        if result != 122:
            return None
    return None


def _table_has_port(table: bytes, port: int, *, row_size: int, port_offset: int) -> bool:
    if len(table) < 4:
        return False
    count = struct.unpack_from("<I", table, 0)[0]
    for index in range(count):
        base = 4 + index * row_size
        if base + port_offset + 4 > len(table):
            return False
        local_port = struct.unpack_from("<I", table, base + port_offset)[0]
        if socket.ntohs(local_port & 0xFFFF) == port:
            return True
    return False


def _wait_until_port_open(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_open(port):
            return True
        time.sleep(0.1)
    return _port_is_open(port)


def _wait_until_port_free(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_is_open(port):
            return True
        time.sleep(0.1)
    return not _port_is_open(port)


def _configure_logging(root: Path) -> None:
    handlers: list[logging.Handler] = []
    if getattr(sys, "frozen", False):
        handlers.append(logging.FileHandler(root / "tca.log", encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


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
