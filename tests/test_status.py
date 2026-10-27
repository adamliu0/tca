import unittest

from tca.status import AppStatus, status_rows


class StatusRowTests(unittest.TestCase):
    def test_unlinked_accounts_get_link_buttons(self) -> None:
        status = AppStatus()
        status.mark_twitch_unlinked()
        status.mark_spotify_unlinked()
        rows = {row.name: row for row in status_rows(status.snapshot())}

        self.assertEqual(rows["Program"].value, "Waiting")
        self.assertEqual(rows["Twitch"].button, "Link Twitch")
        self.assertTrue(rows["Twitch"].button_enabled)
        self.assertEqual(rows["Spotify"].button, "Link Spotify")
        self.assertTrue(rows["Spotify"].button_enabled)
        self.assertIsNone(rows["Program"].button)

    def test_running_and_connected_hides_buttons(self) -> None:
        status = AppStatus()
        status.mark_listening("example")
        status.mark_spotify_connected()
        rows = {row.name: row for row in status_rows(status.snapshot())}

        self.assertEqual(rows["Program"].value, "Running")
        self.assertEqual(rows["Program"].tone, "ok")
        self.assertIn("example", rows["Program"].detail)
        self.assertEqual(rows["Twitch"].value, "Connected")
        self.assertEqual(rows["Spotify"].value, "Connected")
        self.assertIsNone(rows["Twitch"].button)
        self.assertIsNone(rows["Spotify"].button)

    def test_spotify_link_in_progress_disables_the_button(self) -> None:
        status = AppStatus()
        status.mark_spotify_linking()
        spotify = {row.name: row for row in status_rows(status.snapshot())}["Spotify"]
        self.assertEqual(spotify.value, "Waiting for approval")
        self.assertEqual(spotify.button, "Waiting…")
        self.assertFalse(spotify.button_enabled)

    def test_problem_offers_link_again(self) -> None:
        status = AppStatus()
        status.mark_twitch_error("Twitch did not allow chat access.", can_link=True)
        status.mark_spotify_error("Spotify login failed.")
        rows = {row.name: row for row in status_rows(status.snapshot())}
        self.assertEqual(rows["Program"].value, "Not running")
        self.assertEqual(rows["Twitch"].button, "Link again")
        self.assertEqual(rows["Spotify"].button, "Link again")

    def test_missing_configuration_has_no_link_buttons(self) -> None:
        status = AppStatus()
        status.mark_not_configured("Missing settings in .env: TWITCH_CLIENT_ID.")
        rows = {row.name: row for row in status_rows(status.snapshot())}
        self.assertEqual(rows["Program"].value, "Not running")
        self.assertIn("TWITCH_CLIENT_ID", rows["Program"].detail)
        self.assertEqual(rows["Twitch"].value, "Not configured")
        self.assertIsNone(rows["Twitch"].button)
        self.assertIsNone(rows["Spotify"].button)

    def test_setup_needed_has_no_link_buttons(self) -> None:
        status = AppStatus()
        status.mark_twitch_needs_app()
        status.mark_spotify_needs_app()
        rows = {row.name: row for row in status_rows(status.snapshot())}
        self.assertEqual(rows["Program"].value, "Waiting")
        self.assertEqual(rows["Twitch"].value, "Setup needed")
        self.assertEqual(rows["Spotify"].value, "Setup needed")
        self.assertIsNone(rows["Twitch"].button)
        self.assertIsNone(rows["Spotify"].button)

    def test_link_failure_leaves_the_setup_form_available(self) -> None:
        status = AppStatus()
        status.mark_twitch_needs_app()
        status.mark_twitch_link_failed("The Twitch login page is not ready yet.")
        rows = {row.name: row for row in status_rows(status.snapshot())}
        self.assertEqual(rows["Twitch"].value, "Setup needed")
        self.assertIn("not ready", rows["Twitch"].detail)
        self.assertIsNone(rows["Twitch"].button)


if __name__ == "__main__":
    unittest.main()
