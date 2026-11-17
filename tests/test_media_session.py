import tempfile
import unittest
from pathlib import Path

from tca.media_session import DesktopPlayback, describe_session
from tca.spotify import SpotifyNow


class SessionSelectionTests(unittest.TestCase):
    def test_a_browser_session_is_ignored(self) -> None:
        self.assertIsNone(
            describe_session("Chrome", 4, "Night Drive", "Ada", progress_ms=1000, duration_ms=2000)
        )

    def test_spotify_playing_and_paused_are_kept(self) -> None:
        playing = describe_session("Spotify.exe", 4, "Night Drive", "Ada", progress_ms=1500, duration_ms=3000)
        paused = describe_session("SpotifyAB.SpotifyMusic", 5, "Night Drive", "Ada", progress_ms=1500, duration_ms=3000)
        assert playing is not None and paused is not None
        self.assertTrue(playing.playback["is_playing"])
        self.assertFalse(paused.playback["is_playing"])
        self.assertEqual(playing.playback["item"]["name"], "Night Drive")
        self.assertEqual(playing.playback["progress_ms"], 1500)

    def test_a_stopped_spotify_session_is_ignored(self) -> None:
        self.assertIsNone(describe_session("Spotify.exe", 3, "Night Drive", "Ada", progress_ms=0, duration_ms=0))


class DesktopFallbackTests(unittest.TestCase):
    def test_an_empty_api_uses_the_spotify_app(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spotify = SpotifyNow("client", "secret", "http://127.0.0.1:8888/callback", Path(folder) / "cache")
            spotify.client.current_playback = lambda additional_types=None: None  # type: ignore[method-assign]
            spotify.client.currently_playing = lambda additional_types=None: None  # type: ignore[method-assign]
            spotify._desktop = lambda: DesktopPlayback(
                playback={
                    "is_playing": True,
                    "currently_playing_type": "track",
                    "progress_ms": 10,
                    "item": {
                        "type": "track",
                        "id": "desktop:ada",
                        "name": "Night Drive",
                        "duration_ms": 20,
                        "artists": [{"name": "Ada"}],
                    },
                },
                artwork=b"\xff\xd8\xff\xd9",
            )
            track = spotify.playing_track()
            assert track is not None
            self.assertEqual(track.title, "Night Drive")
            self.assertEqual(track.artwork, b"\xff\xd8\xff\xd9")
            self.assertEqual(track.image_url, "local://desktop/desktop:ada")

    def test_an_api_track_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            spotify = SpotifyNow("client", "secret", "http://127.0.0.1:8888/callback", Path(folder) / "cache")
            spotify.client.current_playback = lambda additional_types=None: {  # type: ignore[method-assign]
                "is_playing": True,
                "currently_playing_type": "track",
                "progress_ms": 5,
                "item": {
                    "type": "track",
                    "id": "abc",
                    "name": "From Spotify",
                    "duration_ms": 9,
                    "artists": [{"name": "Bea"}],
                },
            }
            spotify._desktop = lambda: (_ for _ in ()).throw(AssertionError("desktop should not be read"))  # type: ignore[method-assign]
            track = spotify.playing_track()
            assert track is not None
            self.assertEqual(track.title, "From Spotify")
            self.assertEqual(track.track_id, "abc")
