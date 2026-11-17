import json
import socket
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from tca.now_playing import (
    NowPlaying,
    NowPlayingFeed,
    PlayingTrack,
    WidgetSite,
    artwork_request_allowed,
    snapshot_payload,
    track_from_playback,
    widget_page,
    widget_response,
)
from tca.spotify import SpotifyUnavailable
from tca.ui import _NowPlayingPanel
from tca.request_log import RequestLog, SongRequest, requester_for_current


def request(track_id: str, viewer: str, title: str = "Song") -> SongRequest:
    return SongRequest(track_id=track_id, title=title, artist="Artist", viewer=viewer)


ART = "https://i.scdn.co/image/abc123"


def playback() -> dict:
    return {
        "is_playing": True,
        "progress_ms": 12500,
        "currently_playing_type": "track",
        "item": {
            "id": "track1",
            "type": "track",
            "name": "Night Drive",
            "duration_ms": 180000,
            "artists": [{"name": "Ada"}, {"name": "Bea"}],
            "album": {
                "images": [
                    {"url": "https://i.scdn.co/image/large", "width": 640},
                    {"url": ART, "width": 300},
                    {"url": "https://i.scdn.co/image/small", "width": 64},
                ]
            },
        },
    }


class TrackReadTests(unittest.TestCase):
    def test_track_uses_the_album_picture_near_300_pixels(self) -> None:
        track = track_from_playback(playback())
        assert track is not None
        self.assertEqual(track.title, "Night Drive")
        self.assertEqual(track.artist, "Ada, Bea")
        self.assertEqual(track.image_url, ART)
        self.assertEqual(track.progress_ms, 12500)
        self.assertEqual(track.duration_ms, 180000)
        self.assertTrue(track.is_playing)

    def test_paused_track_is_still_the_current_song(self) -> None:
        paused = playback()
        paused["is_playing"] = False
        track = track_from_playback(paused)
        assert track is not None
        self.assertFalse(track.is_playing)
        self.assertEqual(track.title, "Night Drive")

    def test_ads_podcasts_and_silence_are_empty(self) -> None:
        self.assertIsNone(track_from_playback(None))
        self.assertIsNone(track_from_playback({"is_playing": True, "currently_playing_type": "ad", "item": None}))
        self.assertIsNone(
            track_from_playback(
                {"is_playing": True, "currently_playing_type": "episode", "item": {"type": "episode", "name": "Show"}}
            )
        )
        self.assertIsNone(track_from_playback({"is_playing": False, "item": None}))


class RequesterTests(unittest.TestCase):
    def test_request_that_just_became_current(self) -> None:
        found = requester_for_current(
            [request("now", "ada", "Now"), request("next", "bea", "Next")],
            "now",
            ["next"],
            set(),
        )
        assert found is not None
        self.assertEqual(found.viewer, "ada")

    def test_later_request_still_queued_does_not_take_credit(self) -> None:
        found = requester_for_current(
            [request("now", "ada"), request("next", "bea")],
            "next",
            ["next"],
            set(),
        )
        self.assertIsNone(found)

    def test_streamers_own_song_has_no_requester(self) -> None:
        self.assertIsNone(
            requester_for_current([request("next", "ada")], "mine", ["next"], {0})
        )

    def test_repeated_track_from_history_with_an_empty_queue(self) -> None:
        self.assertIsNone(requester_for_current([request("same", "ada")], "same", [], set()))

    def test_last_request_seen_waiting_keeps_its_credit(self) -> None:
        found = requester_for_current([request("same", "ada")], "same", [], {0})
        assert found is not None
        self.assertEqual(found.viewer, "ada")

    def test_removed_request_is_not_credited(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = RequestLog(Path(folder) / "requests.json")
            log.add(request("song", "ada"))
            log.remember_waiting(["song"])
            waiting = log.note_queue(["song"])
            log.remove_matched(["song"], waiting[0][0])
            self.assertIsNone(log.current_viewer("song", []))


class WidgetPageTests(unittest.TestCase):
    def test_snapshot_is_empty_without_a_track(self) -> None:
        self.assertEqual(snapshot_payload(None), {})

    def test_playing_track_includes_progress_and_artwork(self) -> None:
        payload = snapshot_payload(
            NowPlaying(
                track_id="track1",
                title="Night Drive",
                artist="Ada",
                requester="bea",
                image_url=ART,
                progress_ms=1000,
                duration_ms=2000,
                is_playing=True,
                polled_at_ms=10,
            )
        )
        self.assertEqual(payload["artwork_url"], ART)
        self.assertEqual(payload["progress_ms"], 1000)
        self.assertEqual(payload["duration_ms"], 2000)
        self.assertEqual(payload["requester"], "bea")

    def test_page_is_a_rounded_card_with_art_and_a_progress_bar(self) -> None:
        page = widget_page()
        self.assertLess(page.index('id="art"'), page.index('id="title"'))
        self.assertIn("/now-playing.json", page)
        self.assertIn("/artwork", page)
        self.assertIn("border-radius", page)
        self.assertIn("radial-gradient", page)
        self.assertIn("blur", page)
        self.assertIn('id="fill"', page)
        self.assertIn("by.hidden = true", page)
        self.assertIn("requestAnimationFrame", page)
        self.assertIn("polled_at_ms", page)
        self.assertIn("SNAP_MS", page)

    def test_artwork_proxy_rejects_any_other_url(self) -> None:
        self.assertTrue(artwork_request_allowed(ART, ART))
        self.assertFalse(artwork_request_allowed("https://i.scdn.co/image/other", ART))
        self.assertFalse(artwork_request_allowed("https://evil.example/a.jpg", ART))
        self.assertFalse(artwork_request_allowed("http://i.scdn.co/image/abc123", "http://i.scdn.co/image/abc123"))

    def test_foreign_artwork_is_not_fetched(self) -> None:
        def fetch(url: str) -> tuple[bytes, str]:
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as folder:
            feed = NowPlayingFeed(lambda: None, RequestLog(Path(folder) / "requests.json"), fetch=fetch)
            feed._snapshot = NowPlaying(
                track_id="track1",
                title="Night Drive",
                artist="Ada",
                requester=None,
                image_url="https://evil.example/a.jpg",
                progress_ms=0,
                duration_ms=1000,
                is_playing=True,
                polled_at_ms=1,
            )
            self.assertIsNone(feed.artwork())
            self.assertEqual(feed.payload()["artwork_url"], "https://evil.example/a.jpg")

    def test_obs_address_starts_hidden_and_the_eye_reveals_it(self) -> None:
        root = tk.Tk()
        try:
            with tempfile.TemporaryDirectory() as folder:
                feed = NowPlayingFeed(lambda: None, RequestLog(Path(folder) / "requests.json"))
                site = WidgetSite(feed)
                holder = tk.Frame(root)
                holder.pack()
                panel = _NowPlayingPanel(holder, root, site, Path(folder))
                panel.frame.pack()
                root.update()
                entry = panel._address_entry
                self.assertEqual(entry.cget("show"), "*")
                self.assertEqual(entry.get(), site.url)
                eye = next(child for child in entry.master.winfo_children() if isinstance(child, tk.Canvas))
                eye.event_generate("<Button-1>", x=10, y=10)
                root.update()
                self.assertEqual(entry.cget("show"), "")
                self.assertEqual(entry.get(), site.url)
                eye.event_generate("<Button-1>", x=10, y=10)
                root.update()
                self.assertEqual(entry.cget("show"), "*")
                panel._copy()
                self.assertEqual(root.clipboard_get(), site.url)
        finally:
            root.destroy()

    def test_progress_is_stamped_before_the_queue_read(self) -> None:
        clock = {"now": 1.5}

        class Spotify:
            def playing_track(self) -> PlayingTrack:
                return PlayingTrack("track1", "Night Drive", "Ada", None, 4000, 200000, True)

            def upcoming_track_ids(self) -> list[str]:
                clock["now"] = 4.0
                return []

        with tempfile.TemporaryDirectory() as folder:
            feed = NowPlayingFeed(
                lambda: Spotify(),
                RequestLog(Path(folder) / "requests.json"),
                clock=lambda: clock["now"],
            )
            feed.refresh()
            self.assertEqual(feed.payload()["polled_at_ms"], 1500)
            self.assertEqual(feed.payload()["progress_ms"], 4000)

    def test_a_queue_error_does_not_hide_the_current_song(self) -> None:
        class Spotify:
            def playing_track(self) -> PlayingTrack:
                return PlayingTrack("track1", "Night Drive", "Ada", None, 1000, 2000, True)

            def upcoming_track_ids(self) -> list[str]:
                raise SpotifyUnavailable("The queue could not be read.")

        with tempfile.TemporaryDirectory() as folder:
            feed = NowPlayingFeed(lambda: Spotify(), RequestLog(Path(folder) / "requests.json"))
            feed.refresh()
            self.assertEqual(feed.payload()["title"], "Night Drive")

    def test_desktop_artwork_is_served_for_the_current_song(self) -> None:
        picture = b"\xff\xd8\xff\xd9"

        class Spotify:
            def playing_track(self) -> PlayingTrack:
                return PlayingTrack(
                    "desktop:ada",
                    "Night Drive",
                    "Ada",
                    "local://desktop/desktop:ada",
                    1000,
                    2000,
                    True,
                    picture,
                )

            def upcoming_track_ids(self) -> list[str]:
                return []

        with tempfile.TemporaryDirectory() as folder:
            feed = NowPlayingFeed(lambda: Spotify(), RequestLog(Path(folder) / "requests.json"))
            feed.refresh()
            self.assertEqual(feed.artwork(), (picture, "image/jpeg"))

    def test_routes(self) -> None:
        class Feed:
            def payload(self) -> dict[str, object]:
                return {"title": "Night Drive"}

            def artwork(self) -> tuple[bytes, str]:
                return b"img", "image/png"

        status, kind, body = widget_response("/now-playing.json", Feed())  # type: ignore[arg-type]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"title": "Night Drive"})
        self.assertIn("json", kind)
        status, kind, body = widget_response("/artwork", Feed())  # type: ignore[arg-type]
        self.assertEqual((status, kind, body), (200, "image/png", b"img"))
        status, _kind, body = widget_response("/", Feed())  # type: ignore[arg-type]
        self.assertEqual(status, 200)
        self.assertIn(b"now-playing.json", body)
        self.assertEqual(widget_response("/secret", Feed())[0], 404)  # type: ignore[arg-type]

    def test_busy_port_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            feed = NowPlayingFeed(lambda: None, RequestLog(Path(folder) / "requests.json"))
            self._assert_port_busy(feed)

    def _assert_port_busy(self, feed: NowPlayingFeed) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 8756))
        sock.listen(1)
        site = WidgetSite(feed)
        try:
            site.start()
            self.assertEqual(site.error, "Port 8756 is already in use. The song widget needs that address.")
        finally:
            site.stop()
            sock.close()


if __name__ == "__main__":
    unittest.main()
