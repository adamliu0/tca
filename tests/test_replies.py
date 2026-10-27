import unittest

from tca.replies import ReplyMemory, clamp_message, format_playlist, format_song, playlist_id


TRACK = {
    "is_playing": True,
    "currently_playing_type": "track",
    "item": {
        "type": "track",
        "name": "Song Title",
        "artists": [{"name": "Artist"}, {"name": "Guest"}],
        "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
    },
    "context": {
        "type": "playlist",
        "uri": "spotify:playlist:pl123",
        "external_urls": {"spotify": "https://open.spotify.com/playlist/pl123"},
    },
}


class ReplyTests(unittest.TestCase):
    def test_song_includes_artists_and_link(self) -> None:
        self.assertEqual(
            format_song(TRACK),
            "Now playing: Artist, Guest - Song Title — https://open.spotify.com/track/abc",
        )

    def test_paused_song(self) -> None:
        paused = {**TRACK, "is_playing": False}
        self.assertIn("Spotify is paused on:", format_song(paused))

    def test_nothing_playing(self) -> None:
        self.assertEqual(format_song(None), "Nothing is playing on Spotify right now.")

    def test_hidden_track(self) -> None:
        hidden = {"is_playing": True, "currently_playing_type": "track", "item": None}
        self.assertIn("hidden", format_song(hidden))

    def test_ad_and_podcast(self) -> None:
        self.assertIn("ad", format_song({"is_playing": True, "currently_playing_type": "ad", "item": None}))
        episode = {
            "is_playing": True,
            "currently_playing_type": "episode",
            "item": {"type": "episode", "name": "Show Notes"},
        }
        self.assertIn("podcast", format_song(episode))

    def test_playlist_name_and_link(self) -> None:
        reply = format_playlist(TRACK, "Late Night")
        self.assertEqual(reply, "Playlist: Late Night — https://open.spotify.com/playlist/pl123")
        self.assertEqual(playlist_id(TRACK), "pl123")

    def test_playlist_without_a_name(self) -> None:
        self.assertEqual(format_playlist(TRACK, None), "Playlist: https://open.spotify.com/playlist/pl123")

    def test_album_is_not_called_a_playlist(self) -> None:
        album = {
            "is_playing": True,
            "item": {"name": "Track"},
            "context": {
                "type": "album",
                "external_urls": {"spotify": "https://open.spotify.com/album/al1"},
            },
        }
        reply = format_playlist(album, None)
        self.assertIn("album", reply)
        self.assertIn("https://open.spotify.com/album/al1", reply)
        self.assertIsNone(playlist_id(album))

    def test_liked_songs(self) -> None:
        liked = {"is_playing": True, "item": {"name": "Track"}, "context": {"type": "collection"}}
        self.assertIn("Liked Songs", format_playlist(liked, None))

    def test_clamp_keeps_twitch_limit(self) -> None:
        long = "word " * 200
        clamped = clamp_message(long)
        self.assertLessEqual(len(clamped), 500)
        self.assertTrue(clamped.endswith("…"))

    def test_reply_memory_ignores_an_exact_echo(self) -> None:
        memory = ReplyMemory(limit=2)
        memory.remember("Now playing: A")
        self.assertTrue(memory.seen("Now playing: A"))
        self.assertFalse(memory.seen("!song"))
        memory.remember("two")
        memory.remember("three")
        self.assertFalse(memory.seen("Now playing: A"))


if __name__ == "__main__":
    unittest.main()
