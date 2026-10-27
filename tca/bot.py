"""Listen to Twitch chat and answer commands as the signed-in user."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import twitchio
from twitchio import eventsub
from twitchio.exceptions import HTTPException

from tca.commands import Command, CommandFile, HandlerCommand, TextCommand, on_cooldown
from tca.replies import ReplyMemory, clamp_message
from tca.spotify import SpotifyNow, SpotifyUnavailable
from tca.status import AppStatus

logger = logging.getLogger("tca")

AUTH_URL = "http://localhost:4343/oauth"
AUTH_PORT = 4343
REDIRECT_URL = "http://localhost:4343/oauth/callback"


class Autoreply(twitchio.Client):
    def __init__(
        self,
        *,
        login: str,
        commands: CommandFile,
        spotify: SpotifyNow | None,
        status: AppStatus,
        client_id: str,
        client_secret: str,
        bot_id: str | None,
    ) -> None:
        scopes = twitchio.Scopes(user_read_chat=True, user_write_chat=True)
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            scopes=scopes,
        )
        self.account = login
        self._account_id = bot_id
        self.commands = commands
        self.spotify = spotify
        self.status = status
        self._listen_error: str | None = None
        self._recent = ReplyMemory()
        self._cooldowns: dict[str, float] = {}
        self._listening = False
        self._spotify_lock = asyncio.Lock()
        self._handlers: dict[str, Callable[[], str]] = {
            "spotify_song": self._song_reply,
            "spotify_playlist": self._playlist_reply,
        }

    def _song_reply(self) -> str:
        spotify = self.spotify
        if spotify is None:
            raise SpotifyUnavailable("Spotify is not set up yet. Add it in the TCA window.")
        return spotify.song_reply()

    def _playlist_reply(self) -> str:
        spotify = self.spotify
        if spotify is None:
            raise SpotifyUnavailable("Spotify is not set up yet. Add it in the TCA window.")
        return spotify.playlist_reply()

    async def setup_hook(self) -> None:
        self._remember_login()
        await self._listen()

    async def event_ready(self) -> None:
        logger.info("TCA is running. Leave the status window in the background.")
        logger.info("Replies go out through Twitch's API and do not use your keyboard or mouse.")
        self._remember_login()
        if self._listening or self._listen_error:
            return
        if not self.bot_id or self.bot_id not in self.tokens:
            logger.info("Twitch is not linked. Use Link Twitch in the status window.")
            self.status.mark_twitch_unlinked()

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload) -> None:
        if not payload.user_id:
            self.status.mark_twitch_error("Twitch did not say which account signed in.", can_link=True)
            return
        if self._account_id and payload.user_id != self._account_id:
            who = payload.user_login or "that account"
            message = f"Twitch signed in {who}. This program is already linked to a different account."
            logger.error(message)
            self.status.mark_twitch_error(message, can_link=True)
            return
        await self.add_token(payload.access_token, payload.refresh_token)
        await self.save_tokens()
        self._bot_id = payload.user_id
        self._account_id = payload.user_id
        if payload.user_login:
            self.account = payload.user_login
        logger.info("Twitch login saved for %s.", self.account or payload.user_id)
        await self._listen()

    async def event_token_refreshed(self, payload: twitchio.TokenRefreshedPayload) -> None:
        del payload
        await self.save_tokens()

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        text = payload.text.strip()
        if not text or self._recent.seen(text):
            return
        if self.commands.reload_if_changed():
            logger.info("Command list updated.")

        matched = self.commands.book.match(text)
        if matched is None:
            return
        name, command = matched
        now = time.monotonic()
        if on_cooldown(self._cooldowns.get(name), now, self.commands.book.cooldown_seconds):
            logger.info("Ignored %s from %s because that command is on cooldown.", name, _chatter_name(payload))
            return

        try:
            async with self._spotify_lock:
                reply = await asyncio.to_thread(self._build_reply, command)
        except SpotifyUnavailable as exc:
            logger.exception("Spotify request failed")
            if exc.needs_login:
                self.status.mark_spotify_unlinked("Spotify needs to be linked again.")
            reply = "Spotify isn't available right now."

        reply = clamp_message(reply)
        if not reply:
            return

        # Record the reply before sending so the echo of this message cannot run another command.
        self._recent.remember(reply)
        self._cooldowns[name] = now
        if not self.bot_id:
            logger.error("No Twitch user id is set, so the reply was not sent.")
            return

        channel = self.create_partialuser(self.bot_id)
        try:
            # HTTPS request to Twitch. This does not type into chat or touch the keyboard.
            await channel.send_message(reply, self.bot_id, token_for=self.bot_id)
        except (HTTPException, twitchio.MessageRejectedError):
            logger.exception("Twitch rejected the reply to %s", name)
            return
        logger.info("Replied to %s from %s: %s", name, _chatter_name(payload), reply)

    def _build_reply(self, command: Command) -> str:
        if isinstance(command, TextCommand):
            return command.text
        if isinstance(command, HandlerCommand):
            handler = self._handlers.get(command.handler)
            if handler is None:
                return "That command is not set up."
            return handler()
        return "That command is not set up."

    async def _listen(self) -> None:
        if self._listening or not self.bot_id or self.bot_id not in self.tokens:
            return
        try:
            await self.subscribe_websocket(
                eventsub.ChatMessageSubscription(
                    broadcaster_user_id=self.bot_id,
                    user_id=self.bot_id,
                ),
                token_for=self.bot_id,
            )
        except HTTPException:
            self._listen_error = "Twitch did not allow chat access. Link the account again."
            logger.exception(self._listen_error)
            self.status.mark_twitch_error(self._listen_error, can_link=True)
            return
        self._listen_error = None
        self._listening = True
        account = self.account or "your account"
        self.status.mark_listening(account)
        logger.info("Listening in %s's chat.", account)

    def _remember_login(self) -> None:
        user = self.user
        name = getattr(user, "name", None) if user is not None else None
        if isinstance(name, str) and name:
            self.account = name


def _chatter_name(payload: twitchio.ChatMessage) -> str:
    return payload.chatter.name or payload.chatter.id
