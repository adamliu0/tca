import unittest

from tca.song_request import song_request_track, track_id_from_text

TRACK = "4iV5W9uYEdYUVa79Axb7Rh"
LINK = f"https://open.spotify.com/track/{TRACK}?si=abc"


class SongRequestTests(unittest.TestCase):
    def test_song_request_reward_accepts_a_song_link(self) -> None:
        self.assertEqual(song_request_track("Spotify Song Request", f"please play {LINK}"), TRACK)

    def test_reward_name_ignores_case_and_extra_spaces(self) -> None:
        self.assertEqual(song_request_track("  spotify   song request ", LINK), TRACK)

    def test_other_rewards_are_ignored_even_with_a_song_link(self) -> None:
        self.assertIsNone(song_request_track("Highlight my message", LINK))
        self.assertIsNone(song_request_track("Song Request", LINK))

    def test_playlist_album_and_plain_chat_are_not_songs(self) -> None:
        self.assertIsNone(song_request_track("Spotify Song Request", "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"))
        self.assertIsNone(song_request_track("Spotify Song Request", "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"))
        self.assertIsNone(track_id_from_text("check this out https://open.spotify.com/track/not-a-real-id"))
        self.assertIsNone(song_request_track("Spotify Song Request", "play my favorite song"))

    def test_uri_intl_and_embed_links(self) -> None:
        self.assertEqual(track_id_from_text(f"spotify:track:{TRACK}"), TRACK)
        self.assertEqual(track_id_from_text(f"https://open.spotify.com/intl-en/track/{TRACK}"), TRACK)
        self.assertEqual(track_id_from_text(f"https://open.spotify.com/embed/track/{TRACK}"), TRACK)


if __name__ == "__main__":
    unittest.main()
