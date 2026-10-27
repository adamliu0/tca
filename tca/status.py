"""Connection status shown in the TCA window."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Snapshot:
    phase: str = "starting"
    detail: str = "Starting…"
    twitch_state: str = "checking"
    twitch_detail: str = ""
    twitch_can_link: bool = False
    spotify_state: str = "checking"
    spotify_detail: str = ""
    spotify_can_link: bool = False


@dataclass(frozen=True)
class StatusRow:
    name: str
    value: str
    detail: str
    tone: str
    button: str | None = None
    button_enabled: bool = False


class AppStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def update(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def mark_not_configured(self, message: str) -> None:
        self.update(
            phase="error",
            detail=message,
            twitch_state="not_configured",
            twitch_detail="",
            twitch_can_link=False,
            spotify_state="not_configured",
            spotify_detail="",
            spotify_can_link=False,
        )

    def mark_program_error(self, message: str) -> None:
        with self._lock:
            twitch_state = self._snapshot.twitch_state
            if twitch_state == "checking":
                twitch_state = "error"
            self._snapshot = replace(
                self._snapshot,
                phase="error",
                detail=message,
                twitch_state=twitch_state,
                twitch_can_link=False,
            )

    def mark_twitch_needs_app(self) -> None:
        self.update(
            phase="waiting",
            detail="Sign in to Twitch after the one-time app setup.",
            twitch_state="needs_setup",
            twitch_detail="Add the Twitch app below.",
            twitch_can_link=False,
        )

    def mark_spotify_needs_app(self) -> None:
        self.update(
            spotify_state="needs_setup",
            spotify_detail="Add your Spotify app below.",
            spotify_can_link=False,
        )

    def mark_twitch_unlinked(self) -> None:
        self.update(
            phase="waiting",
            detail="Link Twitch to start replying in chat.",
            twitch_state="not_connected",
            twitch_detail="",
            twitch_can_link=True,
        )

    def mark_twitch_error(self, message: str, *, can_link: bool) -> None:
        self.update(
            phase="error",
            detail=message,
            twitch_state="error",
            twitch_detail=message,
            twitch_can_link=can_link,
        )

    def mark_listening(self, login: str) -> None:
        self.update(
            phase="running",
            detail=f"Listening in {login}'s chat.",
            twitch_state="connected",
            twitch_detail="",
            twitch_can_link=False,
        )

    def mark_spotify_unlinked(self, detail: str = "") -> None:
        self.update(spotify_state="not_connected", spotify_detail=detail, spotify_can_link=True)

    def mark_spotify_linking(self) -> None:
        self.update(
            spotify_state="linking",
            spotify_detail="Approve Spotify in your browser. Close TCA if you cancel.",
            spotify_can_link=False,
        )

    def mark_spotify_connected(self) -> None:
        self.update(spotify_state="connected", spotify_detail="", spotify_can_link=False)

    def mark_spotify_error(self, message: str) -> None:
        self.update(spotify_state="error", spotify_detail=message, spotify_can_link=True)

    def note_twitch_login_open(self) -> None:
        with self._lock:
            if self._snapshot.twitch_state == "needs_setup":
                return
            self._snapshot = replace(self._snapshot, twitch_detail="Approve Twitch in your browser.")

    def mark_twitch_link_failed(self, message: str) -> None:
        with self._lock:
            if self._snapshot.phase == "running" and self._snapshot.twitch_state == "connected":
                return
            if self._snapshot.twitch_state == "needs_setup":
                self._snapshot = replace(self._snapshot, twitch_detail=message)
                return
            state = "error" if self._snapshot.twitch_state == "error" else "not_connected"
            self._snapshot = replace(
                self._snapshot,
                twitch_state=state,
                twitch_detail=message,
                twitch_can_link=True,
            )


_ACCOUNT_LABELS = {
    "checking": ("Checking…", "wait"),
    "connected": ("Connected", "ok"),
    "not_connected": ("Not connected", "wait"),
    "linking": ("Waiting for approval", "wait"),
    "error": ("Problem", "bad"),
    "not_configured": ("Not configured", "bad"),
    "needs_setup": ("Setup needed", "wait"),
}

_PROGRAM_LABELS = {
    "starting": ("Starting", "wait"),
    "waiting": ("Waiting", "wait"),
    "running": ("Running", "ok"),
    "error": ("Not running", "bad"),
    "stopped": ("Stopped", "idle"),
}


def status_rows(snapshot: Snapshot) -> list[StatusRow]:
    program_value, program_tone = _PROGRAM_LABELS.get(snapshot.phase, ("Starting", "wait"))
    twitch_value, twitch_tone = _ACCOUNT_LABELS.get(snapshot.twitch_state, ("Checking…", "wait"))
    spotify_value, spotify_tone = _ACCOUNT_LABELS.get(snapshot.spotify_state, ("Checking…", "wait"))
    twitch_button, twitch_enabled = _link_button(
        snapshot.twitch_state,
        snapshot.twitch_can_link,
        "Link Twitch",
    )
    spotify_button, spotify_enabled = _link_button(
        snapshot.spotify_state,
        snapshot.spotify_can_link,
        "Link Spotify",
    )
    return [
        StatusRow("Program", program_value, snapshot.detail, program_tone),
        StatusRow("Twitch", twitch_value, snapshot.twitch_detail, twitch_tone, twitch_button, twitch_enabled),
        StatusRow("Spotify", spotify_value, snapshot.spotify_detail, spotify_tone, spotify_button, spotify_enabled),
    ]


def _link_button(state: str, can_link: bool, label: str) -> tuple[str | None, bool]:
    if state == "linking":
        return "Waiting…", False
    if can_link and state in {"not_connected", "error"}:
        again = "Link again" if state == "error" else label
        return again, True
    return None, False
