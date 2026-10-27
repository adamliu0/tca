import tempfile
import unittest
from pathlib import Path

from tca.request_log import RequestLog, RequestLogError, SongRequest, format_request_queue, match_upcoming
from tca.spotify import grants_queue_access


def request(track_id: str, viewer: str, title: str = "Song") -> SongRequest:
    return SongRequest(track_id=track_id, title=title, artist="Artist", viewer=viewer)


class RequestQueueTests(unittest.TestCase):
    def test_upcoming_follows_playback_order_and_skips_played_songs(self) -> None:
        waiting = [
            request("played", "ada"),
            request("second", "bea", "Second"),
            request("first", "cam", "First"),
        ]
        shown = match_upcoming(waiting, ["first", "other", "second"], limit=5)
        self.assertEqual([item.viewer for item in shown], ["cam", "bea"])

    def test_duplicate_songs_stay_with_the_viewer_who_asked(self) -> None:
        waiting = [request("same", "ada", "Same"), request("same", "bea", "Same")]
        shown = match_upcoming(waiting, ["same", "same"], limit=5)
        self.assertEqual([item.viewer for item in shown], ["ada", "bea"])

    def test_only_five_are_shown(self) -> None:
        waiting = [request(str(index), f"viewer{index}", f"Song {index}") for index in range(8)]
        shown = match_upcoming(waiting, [str(index) for index in range(8)], limit=5)
        self.assertEqual(len(shown), 5)
        self.assertEqual(shown[0].title, "Song 0")
        self.assertEqual(shown[-1].title, "Song 4")

    def test_format_includes_song_artist_and_viewer(self) -> None:
        text = format_request_queue(
            [SongRequest("a", "Midnight", "Nova", "ada"), SongRequest("b", "Daylight", "Nova", "bea")]
        )
        self.assertEqual(
            text,
            "Queue: 1. Midnight - Nova (requested by ada) | 2. Daylight - Nova (requested by bea)",
        )

    def test_empty_queue(self) -> None:
        self.assertEqual(format_request_queue([]), "No song requests are waiting.")

    def test_requested_by_touches_the_closing_parenthesis(self) -> None:
        text = format_request_queue([SongRequest("a", "Midnight", "Nova", "ada ")])
        self.assertIn("(requested by ada)", text)
        self.assertNotIn("ada )", text)

    def test_format_stays_within_the_chat_limit(self) -> None:
        waiting = [
            SongRequest(str(index), "Very long song title " * 8, "Very long artist " * 4, f"viewer{index}")
            for index in range(5)
        ]
        text = format_request_queue(waiting)
        self.assertLessEqual(len(text), 500)
        self.assertTrue(text.startswith("Queue: 1. "))

    def test_queue_access_needs_both_playback_scopes(self) -> None:
        granted = "user-read-currently-playing playlist-read-private user-modify-playback-state"
        self.assertFalse(grants_queue_access(granted))
        self.assertTrue(grants_queue_access(granted + " user-read-playback-state"))

    def test_remove_hides_one_copy_while_the_song_stays_queued(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".song_requests.json"
            log = RequestLog(path)
            log.add(request("same", "ada", "Same"))
            log.add(request("same", "bea", "Same"))
            log.add(request("same", "cam", "Same"))
            shown = log.note_queue(["same", "same"])
            self.assertEqual([item.viewer for _, item in shown], ["ada", "bea"])

            removed = log.remove_matched(["same", "same"], shown[0][0])
            self.assertEqual(removed.viewer, "ada")
            again = log.note_queue(["same", "same"])
            self.assertEqual([item.viewer for _, item in again], ["bea"])
            self.assertEqual([item.viewer for item in log.upcoming(["same", "same"])], ["bea"])

            reloaded = RequestLog(path)
            self.assertEqual([item.viewer for _, item in reloaded.note_queue(["same", "same"])], ["bea"])
            self.assertEqual(reloaded.upcoming([]), [])
            self.assertEqual(reloaded.note_queue(["same"]), [item for _, item in reloaded.matched(["same"])])
            with self.assertRaises(RequestLogError):
                reloaded.remove_matched(["same"], 99)

    def test_old_request_file_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".song_requests.json"
            path.write_text(
                '[{"track_id": "abc", "title": "Midnight", "artist": "Nova", "viewer": "ada"}]\n',
                encoding="utf-8",
            )
            log = RequestLog(path)
            self.assertEqual(log.upcoming(["abc"]), [SongRequest("abc", "Midnight", "Nova", "ada")])

    def test_log_remembers_requests(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".song_requests.json"
            log = RequestLog(path)
            log.add(SongRequest("abc", "Midnight", "Nova", "ada"))
            reloaded = RequestLog(path)
            shown = reloaded.upcoming(["abc"])
            self.assertEqual(shown, [SongRequest("abc", "Midnight", "Nova", "ada")])


if __name__ == "__main__":
    unittest.main()
