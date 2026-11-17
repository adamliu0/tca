import tempfile
import tkinter as tk
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from tca.artwork import card_images, fetch_album_art
from tca.commands import CommandFile
from tca.request_log import RequestBoard, RequestLog, SongRequest
from tca.spotify import album_image_url, queue_listing
from tca.ui import _CommandPanel, _RequestPanel


class QueueArtTests(unittest.TestCase):
    def test_album_art_url_prefers_a_sharp_picture(self) -> None:
        url = album_image_url(
            {
                "images": [
                    {"url": "https://i.scdn.co/image/large", "width": 640},
                    {"url": "https://i.scdn.co/image/card", "width": 300},
                    {"url": "https://i.scdn.co/image/tiny", "width": 64},
                ]
            }
        )
        self.assertEqual(url, "https://i.scdn.co/image/card")

    def test_queue_listing_keeps_ids_and_art(self) -> None:
        ids, images = queue_listing(
            {
                "queue": [
                    {"type": "track", "id": "one", "album": {"images": [{"url": "https://i.scdn.co/image/one", "width": 300}]}},
                    {"type": "episode", "id": "skip"},
                    {"id": "two"},
                ]
            }
        )
        self.assertEqual(ids, ["one", "two"])
        self.assertEqual(images, {"one": "https://i.scdn.co/image/one"})

    def test_other_hosts_are_not_downloaded(self) -> None:
        self.assertIsNone(fetch_album_art("https://example.com/art.jpg"))

    def test_a_card_washes_the_album_color(self) -> None:
        image = Image.new("RGB", (24, 24), (210, 40, 50))
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        wash, thumb = card_images(buffer.getvalue(), width=120, height=64, art_size=32)
        self.assertTrue(wash.startswith(b"\x89PNG"))
        self.assertIsNotNone(thumb)
        opened = Image.open(BytesIO(wash))
        red, green, blue, _alpha = opened.getpixel((16, 32))
        self.assertGreater(red, green)
        self.assertGreater(red, blue)

    def test_the_board_remembers_album_art_urls(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            log = RequestLog(Path(folder) / "requests.json")
            log.add(SongRequest("track", "Midnight", "Nova", "ada"))
            board = RequestBoard(
                log,
                lambda: (["track"], {"track": "https://i.scdn.co/image/midnight"}),
                interval=60,
            )
            try:
                board.refresh()
                shown = board.snapshot()
            finally:
                board.stop()
        self.assertEqual(shown[0][2], "Midnight")
        self.assertEqual(shown[0][5], "https://i.scdn.co/image/midnight")


class RequestCardTests(unittest.TestCase):
    def test_a_request_is_a_card_with_remove_on_the_right(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack(fill="both", expand=True)
            panel = _RequestPanel(
                holder,
                _Board(),
                on_changed=lambda: None,
                root=root,
            )
            panel.frame.pack(fill="both", expand=True)
            root.geometry("640x480")
            root.update()
            panel.sync()
            root.update()
            card = panel._cards[0]
            text = [card.itemcget(item, "text") for item in card.find_all() if card.type(item) == "text"]
            self.assertIn("Midnight", text)
            self.assertIn("Nova", text)
            self.assertIn("Requested by ada", text)
            self.assertTrue(card.find_withtag("remove"))
            box = card.remove_box
            self.assertGreater(box[0], 300)
        finally:
            root.destroy()

    def test_a_downloaded_picture_paints_the_card(self) -> None:
        root = tk.Tk()
        try:
            url = "https://i.scdn.co/image/midnight"
            board = _Board()
            board.rows = ((1, "track", "Midnight", "Nova", "ada", url),)
            holder = tk.Frame(root)
            holder.pack(fill="both", expand=True)
            panel = _RequestPanel(holder, board, on_changed=lambda: None, root=root)
            panel._want_art = lambda _url: None  # type: ignore[method-assign]
            panel.frame.pack(fill="both", expand=True)
            root.geometry("640x480")
            root.update()
            panel.sync()
            self.assertFalse(hasattr(panel._cards[0], "thumb"))
            image = Image.new("RGB", (40, 40), (20, 140, 200))
            buffer = BytesIO()
            image.save(buffer, format="JPEG")
            panel._art_ready.put((url, buffer.getvalue()))
            panel._take_art()
            self.assertTrue(hasattr(panel._cards[0], "thumb"))
        finally:
            root.destroy()

    def test_a_short_queue_does_not_scroll_past_the_cards(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack(fill="both", expand=True)
            panel = _RequestPanel(holder, _Board(3), on_changed=lambda: None, root=root)
            panel.frame.pack(fill="both", expand=True)
            root.geometry("700x900")
            root.update()
            panel.sync()
            root.update()
            content = panel._list.winfo_reqheight()
            region = _region_height(panel._rows.canvas)
            self.assertAlmostEqual(region, content, delta=4)
            self.assertLessEqual(panel._rows.canvas.winfo_height(), content + 4)
            self.assertGreaterEqual(panel._rows.canvas.yview()[1], 0.999)
            panel._rows._wheel(_Wheel())
            self.assertEqual(panel._rows.canvas.yview()[0], 0.0)
        finally:
            root.destroy()

    def test_a_shorter_queue_drops_the_extra_scroll_space(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack(fill="both", expand=True)
            board = _Board(10)
            panel = _RequestPanel(holder, board, on_changed=lambda: None, root=root)
            panel.frame.pack(fill="both", expand=True)
            root.geometry("700x560")
            root.update()
            panel.sync()
            root.update()
            self.assertGreater(_region_height(panel._rows.canvas), panel._rows.canvas.winfo_height() + 40)
            board.rows = _rows(3)
            panel.sync()
            root.update()
            content = panel._list.winfo_reqheight()
            self.assertAlmostEqual(_region_height(panel._rows.canvas), content, delta=4)
            self.assertLessEqual(panel._rows.canvas.winfo_height(), content + 4)
            self.assertGreaterEqual(panel._rows.canvas.yview()[1], 0.999)
        finally:
            root.destroy()


class CommandScrollTests(unittest.TestCase):
    def test_many_commands_keep_add_on_screen_and_scroll_only_the_rows(self) -> None:
        root = tk.Tk()
        try:
            with tempfile.TemporaryDirectory() as folder:
                commands = CommandFile(Path(folder) / "commands.json")
                for number in range(12):
                    commands.add_text_command(f"cmd{number}", f"reply {number}")
                holder = tk.Frame(root)
                holder.pack(fill="both", expand=True)
                panel = _CommandPanel(holder, commands, on_changed=lambda: None)
                panel.frame.pack(fill="x")
                root.geometry("520x640")
                root.update()
                panel.sync()
                root.update()
                add = panel._add
                self.assertTrue(add.winfo_ismapped())
                self.assertLess(add.winfo_rooty() + add.winfo_height(), root.winfo_rooty() + root.winfo_height())
                region = _region_height(panel._rows.canvas)
                self.assertAlmostEqual(region, panel._list.winfo_reqheight(), delta=4)
                self.assertGreater(region, panel._rows.canvas.winfo_height() + 20)
        finally:
            root.destroy()


def _region_height(canvas: tk.Canvas) -> float:
    _x1, y1, _x2, y2 = (float(part) for part in canvas.cget("scrollregion").split())
    return y2 - y1


class _Wheel:
    delta = -120


def _rows(count: int) -> tuple[tuple[int, str, str, str, str, str], ...]:
    return tuple((index, "track", f"Song {index}", "Artist", "ada", "") for index in range(1, count + 1))


class _Board:
    def __init__(self, count: int = 1) -> None:
        if count == 1:
            self.rows = ((1, "track", "Midnight", "Nova", "ada", ""),)
        else:
            self.rows = _rows(count)

    def snapshot(self) -> tuple[tuple[int, str, str, str, str, str], ...]:
        return self.rows

    def remove(self, _index: int) -> SongRequest:
        return SongRequest("track", "Midnight", "Nova", "ada")
