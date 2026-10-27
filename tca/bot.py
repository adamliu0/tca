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
from tca.request_log import RequestLog, SongRequest, format_request_queue
from tca.song_request import is_song_request_reward, track_id_from_text
from tca.spotify import QueuedTrack, SpotifyNow, SpotifyUnavailable
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
        requests: RequestLog,
    ) -> None:
        scopes = twitchio.Scopes(
            user_read_chat=True,
            user_write_chat=True,
            channel_read_redemptions=True,
        )
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
        self._song_requests = False
        self._redemptions = ReplyMemory(limit=200)
        self._spotify_lock = asyncio.Lock()
        self._requests = requests
        self._handlers: dict[str, Callable[[], str]] = {
            "spotify_song": self._song_reply,
            "spotify_playlist": self._playlist_reply,
            "spotify_queue": self._queue_reply,
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

    def _queue_reply(self) -> str:
        spotify = self.spotify
        if spotify is None:
            raise SpotifyUnavailable("Spotify is not set up yet. Add it in the TCA window.")
        waiting = self._requests.upcoming(spotify.upcoming_track_ids())
        return format_request_queue(waiting)

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
                self.status.note_spotify_reauth(str(exc) or "Link Spotify again.")
                reply = str(exc) or "Spotify needs to be linked again."
            else:
                reply = "Spotify isn't available right now."

        self._cooldowns[name] = now
        if await self._say(reply):
            logger.info("Replied to %s from %s: %s", name, _chatter_name(payload), clamp_message(reply))

    async def event_custom_redemption_add(self, payload: twitchio.ChannelPointsRedemptionAdd) -> None:
        # Chat messages are handled above and never reach this. A Spotify link in chat is not queued.
        if payload.status == "canceled":
            return
        reward = payload.reward.title
        user_input = payload.user_input or ""
        if not is_song_request_reward(reward):
            if track_id_from_text(user_input):
                logger.info("Ignored a Spotify link from the %r reward. Only Spotify Song Request is queued.", reward)
            return
        if self._redemptions.seen(payload.id):
            return
        self._redemptions.remember(payload.id)
        viewer = payload.user.name or payload.user.id
        track_id = track_id_from_text(user_input)
        if track_id is None:
            logger.info("Song request from %s had no Spotify song link.", viewer)
            await self._say("That song request needs a Spotify song link.")
            return
        try:
            async with self._spotify_lock:
                queued = await asyncio.to_thread(self._queue_track, track_id)
                self._requests.add(
                    SongRequest(
                        track_id=queued.track_id,
                        title=queued.title,
                        artist=queued.artist,
                        viewer=viewer,
                    )
                )
        except SpotifyUnavailable as exc:
            logger.exception("Could not queue a song request from %s", viewer)
            if exc.needs_login:
                self.status.note_spotify_reauth("Link Spotify again so song requests can be queued.")
            await self._say(str(exc) or "Spotify isn't available right now.")
            return

        if await self._say(f"Queued: {queued.label}"):
            logger.info("Queued %s for %s.", queued.label, viewer)

    def _queue_track(self, track_id: str) -> QueuedTrack:
        spotify = self.spotify
        if spotify is None:
            raise SpotifyUnavailable("Spotify is not set up yet. Add it in the TCA window.")
        return spotify.queue_track(track_id)

    async def _say(self, text: str) -> bool:
        reply = clamp_message(text)
        if not reply:
            return False
        # Record the reply before sending so the echo of this message cannot run another command.
        self._recent.remember(reply)
        if not self.bot_id:
            logger.error("No Twitch user id is set, so the reply was not sent.")
            return False
        channel = self.create_partialuser(self.bot_id)
        try:
            # HTTPS request to Twitch. This does not type into chat or touch the keyboard.
            await channel.send_message(reply, self.bot_id, token_for=self.bot_id)
        except (HTTPException, twitchio.MessageRejectedError):
            logger.exception("Twitch rejected a reply")
            return False
        return True

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
        if not self.bot_id or self.bot_id not in self.tokens:
            return
        if self._listening:
            await self._listen_for_song_requests()
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
        await self._listen_for_song_requests()

    async def _listen_for_song_requests(self) -> None:
        if self._song_requests or not self.bot_id:
            return
        try:
            await self.subscribe_websocket(
                eventsub.ChannelPointsRedeemAddSubscription(broadcaster_user_id=self.bot_id),
                token_for=self.bot_id,
            )
        except HTTPException as exc:
            if exc.status in {401, 403}:
                message = "Link Twitch again so song requests can be read."
                logger.warning(message)
                self.status.note_twitch_reauth(message)
                return
            logger.exception("Twitch did not allow song request redemptions.")
            return
        self._song_requests = True
        self.status.clear_twitch_reauth()
        logger.info("Listening for Spotify Song Request redemptions.")

    def _remember_login(self) -> None:
        user = self.user
        name = getattr(user, "name", None) if user is not None else None
        if isinstance(name, str) and name:
            self.account = name


def _chatter_name(payload: twitchio.ChatMessage) -> str:
    return payload.chatter.name or payload.chatter.id
