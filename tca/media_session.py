"""The Spotify app playing on this PC, when Spotify's API reports nothing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("tca")

_PLAYING = 4
_PAUSED = 5
_ARTWORK_LIMIT = 2_000_000


@dataclass(frozen=True)
class DesktopPlayback:
    playback: dict
    artwork: bytes | None = None


def describe_session(
    app_id: str,
    status: int,
    title: str,
    artist: str,
    *,
    progress_ms: int,
    duration_ms: int,
) -> DesktopPlayback | None:
    """A Spotify app session only. Other players, such as a browser stream, are ignored."""
    if "spotify" not in app_id.casefold():
        return None
    if status not in {_PLAYING, _PAUSED}:
        return None
    name = title.strip()
    if not name:
        return None
    who = artist.strip() or "Unknown artist"
    progress = progress_ms if progress_ms > 0 else 0
    duration = duration_ms if duration_ms > 0 else 0
    return DesktopPlayback(
        playback={
            "is_playing": status == _PLAYING,
            "currently_playing_type": "track",
            "progress_ms": progress,
            "item": {
                "type": "track",
                "id": f"desktop:{who}\u0000{name}",
                "name": name,
                "duration_ms": duration,
                "artists": [{"name": who}],
            },
        }
    )


def desktop_spotify() -> DesktopPlayback | None:
    """What the Spotify app on this computer is playing, or nothing."""
    try:
        return asyncio.run(_desktop_spotify())
    except Exception:
        logger.debug("Windows could not read the Spotify app.", exc_info=True)
        return None


async def _desktop_spotify() -> DesktopPlayback | None:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    found: list[tuple[DesktopPlayback, object]] = []
    for session in list(manager.get_sessions()):
        props = await session.try_get_media_properties_async()
        if props is None:
            continue
        info = session.get_playback_info()
        timeline = session.get_timeline_properties()
        start = _milliseconds(getattr(timeline, "start_time", None))
        end = _milliseconds(getattr(timeline, "end_time", None))
        position = _milliseconds(getattr(timeline, "position", None))
        described = describe_session(
            str(session.source_app_user_model_id or ""),
            int(info.playback_status),
            str(props.title or ""),
            str(props.artist or ""),
            progress_ms=max(position - start, 0),
            duration_ms=max(end - start, 0),
        )
        if described is not None:
            found.append((described, props))
    playing = [item for item in found if item[0].playback.get("is_playing")]
    chosen = playing[0] if playing else (found[0] if found else None)
    if chosen is None:
        return None
    described, props = chosen
    return DesktopPlayback(playback=described.playback, artwork=await _artwork(props))


def _milliseconds(span: object) -> int:
    total = getattr(span, "total_seconds", None)
    if total is None:
        return 0
    return int(total() * 1000)


async def _artwork(props: object) -> bytes | None:
    thumbnail = getattr(props, "thumbnail", None)
    if thumbnail is None:
        return None
    try:
        from winrt.windows.storage.streams import DataReader

        stream = await thumbnail.open_read_async()
        size = int(stream.size)
        if size <= 0 or size > _ARTWORK_LIMIT:
            return None
        reader = DataReader(stream)
        loaded = int(await reader.load_async(size))
        if loaded <= 0:
            return None
        raw = bytearray(loaded)
        result = reader.read_bytes(raw)
        data = bytes(result) if isinstance(result, (bytes, bytearray)) else bytes(raw)
    except Exception:
        logger.debug("Windows did not return the Spotify album picture.", exc_info=True)
        return None
    if data.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
        return data
    return None
