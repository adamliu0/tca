import tempfile
import unittest
from pathlib import Path

from tca.commands import CommandFile
from tca.now_playing import WidgetSite
from tca.spotify import SpotifyNow, SpotifyUnavailable
from tca.status import AppStatus
from tca.ui import _GOLDEN, StatusWindow, golden_window


class GoldenWindowTests(unittest.TestCase):
    def test_a_tall_window_grows_wider(self) -> None:
        width, height = golden_window(800, 700, 1920, 1080)
        self.assertGreaterEqual(width, 800)
        self.assertGreaterEqual(height, 700)
        self.assertAlmostEqual(width / height, _GOLDEN, delta=0.02)

    def test_a_wide_window_grows_taller(self) -> None:
        width, height = golden_window(1000, 400, 1920, 1080)
        self.assertGreaterEqual(width, 1000)
        self.assertGreaterEqual(height, 400)
        self.assertAlmostEqual(width / height, _GOLDEN, delta=0.02)

    def test_the_rectangle_stays_on_screen(self) -> None:
        width, height = golden_window(900, 1100, 1280, 720)
        self.assertLessEqual(width, 1280)
        self.assertLessEqual(height, 720)
        self.assertAlmostEqual(width / height, _GOLDEN, delta=0.03)


class UnlinkTests(unittest.TestCase):
    def test_unlink_still_runs_when_the_row_is_refreshed_mid_click(self) -> None:
        hits = {"twitch": 0}
        status = AppStatus()
        status.mark_listening("ada")
        status.mark_spotify_connected()
        window = StatusWindow(
            status,
            lambda: None,
            lambda: None,
            on_unlink_twitch=lambda: hits.__setitem__("twitch", hits["twitch"] + 1),
            on_unlink_spotify=lambda: None,
        )
        try:
            window._sync()
            window.root.update()
            button = window._rows["Twitch"]["unlink"]
            button.event_generate("<ButtonPress-1>")
            window._sync()
            button.event_generate("<ButtonRelease-1>")
            window.root.update()
            self.assertEqual(hits["twitch"], 1)
        finally:
            window.root.destroy()

    def test_unlinking_does_not_move_or_shrink_the_window(self) -> None:
        status = AppStatus()
        status.mark_listening("ada")
        status.mark_spotify_connected()
        window = StatusWindow(status, lambda: None, lambda: None, on_unlink_twitch=lambda: None, on_unlink_spotify=lambda: None)
        try:
            window._sync()
            window.root.update()
            place = (window.root.winfo_x(), window.root.winfo_y(), window.root.winfo_width(), window.root.winfo_height())
            status.update(
                phase="waiting",
                detail="Twitch is unlinked.",
                twitch_state="checking",
                twitch_detail="Unlinking…",
                twitch_can_link=False,
            )
            window._sync()
            window.root.update()
            self.assertEqual((window.root.winfo_x(), window.root.winfo_y()), place[:2])
            self.assertGreaterEqual(window.root.winfo_width(), place[2])
            self.assertGreaterEqual(window.root.winfo_height(), place[3])
            again = (window.root.winfo_x(), window.root.winfo_y(), window.root.winfo_width(), window.root.winfo_height())
            window._sync()
            window.root.update()
            self.assertEqual(
                (window.root.winfo_x(), window.root.winfo_y(), window.root.winfo_width(), window.root.winfo_height()),
                again,
            )
        finally:
            window.root.destroy()

    def test_the_window_appears_only_after_the_cards_have_settled(self) -> None:
        status = AppStatus()
        status.mark_listening("ada")
        status.mark_spotify_connected()
        window = StatusWindow(status, lambda: None, lambda: None)
        try:
            self.assertEqual(float(window.root.attributes("-alpha")), 0.0)
            window._sync()
            window._connections.open(True)
            window.root.update()
            settled = (window.root.winfo_width(), window.root.winfo_height())
            card_height = int(window._twitch_form.frame._canvas.cget("height"))
            self.assertGreater(card_height, 48)
            self.assertFalse(window.root.geometry().endswith("+120+120"))
            window._reveal()
            self.assertEqual(float(window.root.attributes("-alpha")), 1.0)
            self.assertEqual((window.root.winfo_width(), window.root.winfo_height()), settled)
            self.assertEqual(int(window._twitch_form.frame._canvas.cget("height")), card_height)
            self.assertTrue(window.root.geometry().endswith("+120+120"))
        finally:
            window.root.destroy()

    def test_a_resized_window_is_not_pulled_back(self) -> None:
        status = AppStatus()
        status.mark_listening("ada")
        status.mark_spotify_connected()
        window = StatusWindow(status, lambda: None, lambda: None)
        try:
            window._sync()
            window.root.update()
            wide, tall = window.root.resizable()
            self.assertTrue(wide)
            self.assertTrue(tall)
            golden_w = window.root.winfo_width()
            golden_h = window.root.winfo_height()
            window.root.geometry(f"{golden_w + 120}x{golden_h + 60}+30+30")
            window.root.update()
            resized = (
                window.root.winfo_x(),
                window.root.winfo_y(),
                window.root.winfo_width(),
                window.root.winfo_height(),
            )
            self.assertGreater(resized[2], golden_w)
            self.assertGreater(resized[3], golden_h)
            window._sync()
            window.root.update()
            self.assertEqual(
                (window.root.winfo_x(), window.root.winfo_y(), window.root.winfo_width(), window.root.winfo_height()),
                resized,
            )
        finally:
            window.root.destroy()

    def test_commands_stay_readable_when_the_window_is_short(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            commands = CommandFile(Path(folder) / "commands.json")
            for number in range(4):
                commands.add_text_command(f"!cmd{number}", f"reply {number}")
            status = AppStatus()
            status.mark_listening("ada")
            status.mark_spotify_connected()
            window = StatusWindow(status, lambda: None, lambda: None, commands=commands)
            try:
                for number in range(8):
                    commands.add_text_command(f"!more{number}", f"reply {number}")
                window._sync()
                window.root.geometry("980x700+40+40")
                window.root.update()
                panel = window._commands
                assert panel is not None
                self.assertFalse(hasattr(window, "_setup_scroll"))
                add = panel._add
                card = panel.frame
                root_top = window.root.winfo_rooty()
                root_bottom = root_top + window.root.winfo_height()
                self.assertTrue(add.winfo_ismapped())
                self.assertGreaterEqual(card.winfo_rooty(), root_top - 2)
                self.assertLessEqual(card.winfo_rooty() + card.winfo_height(), root_bottom + 4)
                self.assertGreaterEqual(add.winfo_rooty(), card.winfo_rooty())
                self.assertLessEqual(add.winfo_rooty() + add.winfo_height(), card.winfo_rooty() + card.winfo_height() + 4)
                _x1, y1, _x2, y2 = (float(part) for part in panel._rows.canvas.cget("scrollregion").split())
                region = y2 - y1
                self.assertGreater(region, panel._rows.canvas.winfo_height() + 20)
            finally:
                window.root.destroy()

    def test_preview_and_requests_share_one_width(self) -> None:
        site = WidgetSite(None)
        site.error = "No preview in this test."
        window = StatusWindow(
            AppStatus(),
            lambda: None,
            lambda: None,
            requests=_EmptyBoard(),
            widget=site,
            widget_cache=Path("."),
        )
        try:
            window._sync()
            window.root.geometry("1200x760+40+40")
            window.root.update()
            preview = window._widget.frame
            requests = window._requests.frame
            assert preview is not None and requests is not None
            self.assertGreater(preview.winfo_width(), 200)
            self.assertAlmostEqual(preview.winfo_width(), requests.winfo_width(), delta=2)
            self.assertAlmostEqual(preview.winfo_rootx(), requests.winfo_rootx(), delta=2)
            self.assertLess(preview.winfo_rooty(), requests.winfo_rooty())
        finally:
            window.root.destroy()

    def test_connections_open_over_the_commands_section(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            commands = CommandFile(Path(folder) / "commands.json")
            window = StatusWindow(AppStatus(), lambda: None, lambda: None, commands=commands)
            try:
                window._sync()
                window.root.geometry("1100x760+40+40")
                window.root.update()
                panel = window._commands
                assert panel is not None
                card = panel.frame
                place = (card.winfo_x(), card.winfo_y(), card.winfo_width(), card.winfo_height())
                self.assertFalse(window._connections.panel.winfo_ismapped())
                window._connections.open(True)
                window.root.update()
                self.assertEqual((card.winfo_x(), card.winfo_y(), card.winfo_width(), card.winfo_height()), place)
                self.assertTrue(window._connections.panel.winfo_ismapped())
                self.assertTrue(window._twitch_form.frame.winfo_ismapped())
                self.assertTrue(window._spotify_form.frame.winfo_ismapped())
                self.assertLess(window._twitch_form.frame.winfo_rooty(), window._spotify_form.frame.winfo_rooty())
                self.assertGreaterEqual(window._twitch_form.frame.winfo_rooty(), card.winfo_rooty() - 2)
                self.assertLess(window._twitch_form.frame.winfo_rooty(), card.winfo_rooty() + card.winfo_height())
                window._connections.open(False)
                window.root.update()
                self.assertFalse(window._connections.panel.winfo_ismapped())
                self.assertEqual((card.winfo_x(), card.winfo_y(), card.winfo_width(), card.winfo_height()), place)
            finally:
                window.root.destroy()

    def test_credential_bars_stay_when_both_accounts_are_linked(self) -> None:
        status = AppStatus()
        status.mark_listening("ada")
        status.mark_spotify_connected()
        window = StatusWindow(status, lambda: None, lambda: None)
        try:
            window.prefill(twitch_id="twitch-id", twitch_secret="twitch-secret", spotify_id="spot-id", spotify_secret="spot-secret")
            window._sync()
            window._connections.open(True)
            window.root.update()
            for form, client_id, secret in (
                (window._twitch_form, "twitch-id", "twitch-secret"),
                (window._spotify_form, "spot-id", "spot-secret"),
            ):
                self.assertTrue(form.frame.winfo_ismapped())
                self.assertTrue(form._client_id.winfo_ismapped())
                self.assertTrue(form._secret.winfo_ismapped())
                self.assertEqual(form._client_id.get(), client_id)
                self.assertEqual(form._secret.get(), secret)
                self.assertEqual(form._client_id.cget("show"), "*")
                self.assertEqual(form._secret.cget("show"), "*")
                self.assertFalse(form._body.winfo_ismapped())
        finally:
            window.root.destroy()

class _EmptyBoard:
    def snapshot(self) -> tuple[()]:
        return ()


class SpotifySessionTests(unittest.TestCase):
    def test_spotify_forget_login_drops_the_saved_session(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / ".spotify_cache"
            cache.write_text('{"access_token":"token","refresh_token":"refresh","expires_at":9999999999}', encoding="utf-8")
            spotify = SpotifyNow("client", "secret", "http://127.0.0.1:8888/callback", cache)
            spotify.forget_login()
            self.assertFalse(cache.exists())
            self.assertFalse(spotify.has_saved_login())
            with self.assertRaises(SpotifyUnavailable) as caught:
                spotify.playing_track()
            self.assertTrue(caught.exception.needs_login)
