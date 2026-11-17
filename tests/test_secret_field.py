import tkinter as tk
import unittest

from tca.ui import _CredentialForm, _EntryBar, _RoundButton, _paint_eye


def _entry_bar(entry: tk.Entry) -> _EntryBar:
    widget: tk.Misc = entry
    while not isinstance(widget, _EntryBar):
        widget = widget.nametowidget(widget.winfo_parent())
    return widget


def _copy_buttons(widget: tk.Misc) -> list[_RoundButton]:
    found: list[_RoundButton] = []
    if isinstance(widget, _RoundButton) and widget.cget("text") == "Copy":
        found.append(widget)
    for child in widget.winfo_children():
        found.extend(_copy_buttons(child))
    return found


class SecretFieldTests(unittest.TestCase):
    def test_hidden_eye_is_a_different_grey_icon(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            canvas = tk.Canvas(root, bg="#0e0e10", width=44, height=34)
            _paint_eye(canvas, revealed=True)
            open_item = canvas.find_all()[0]
            self.assertEqual(len(canvas.find_all()), 1)
            self.assertEqual(canvas.itemcget(open_item, "fill"), "#adadb8")
            open_text = canvas.itemcget(open_item, "text")
            _paint_eye(canvas, revealed=False)
            hidden_item = canvas.find_all()[0]
            self.assertEqual(len(canvas.find_all()), 1)
            self.assertEqual(canvas.itemcget(hidden_item, "fill"), "#adadb8")
            self.assertNotEqual(canvas.itemcget(hidden_item, "text"), open_text)
        finally:
            root.destroy()

    def test_credentials_start_hidden_and_the_eye_reveals_them(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack()
            form = _CredentialForm(
                holder,
                root,
                title="Twitch",
                body="Paste the Twitch app.",
                redirect="http://localhost:4343",
                open_label="Open Twitch",
                open_url="https://dev.twitch.tv",
                save_label="Save",
                on_save=lambda _client_id, _secret: None,
                on_saved=lambda: None,
            )
            form.frame.pack()
            root.update()
            for entry in (form._client_id, form._secret):
                self.assertEqual(entry.cget("show"), "*")
                eye = next(child for child in entry.master.winfo_children() if isinstance(child, tk.Canvas))
                eye.event_generate("<Button-1>", x=10, y=10)
                self.assertEqual(entry.cget("show"), "")
                eye.event_generate("<Button-1>", x=10, y=10)
                self.assertEqual(entry.cget("show"), "*")
        finally:
            root.destroy()

    def test_clicking_outside_a_field_leaves_it(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack(fill="both", expand=True)
            form = _CredentialForm(
                holder,
                root,
                title="Twitch connection",
                body="Paste the Twitch app.",
                redirect="http://localhost:4343",
                open_label="Open Twitch",
                open_url="https://dev.twitch.tv",
                save_label="Save",
                on_save=lambda _client_id, _secret: None,
                on_saved=lambda: None,
            )
            form.frame.pack()
            root.geometry("480x360")
            root.update()
            entry = form._client_id
            bar = _entry_bar(entry)
            entry.event_generate("<FocusIn>")
            self.assertEqual(bar._outline, "#9146ff")
            moved: list[str] = []
            root.focus_set = lambda: moved.append("away")  # type: ignore[method-assign]
            title = form.frame.body.winfo_children()[0]
            title.event_generate("<Button-1>", x=6, y=6)
            self.assertEqual(moved, ["away"])
            form._secret.event_generate("<Button-1>", x=8, y=8)
            self.assertEqual(moved, ["away"])
        finally:
            root.destroy()

    def test_copy_uses_the_saved_value_while_it_is_hidden(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root)
            holder.pack()
            form = _CredentialForm(
                holder,
                root,
                title="Twitch",
                body="Paste the Twitch app.",
                redirect="http://localhost:4343",
                open_label="Open Twitch",
                open_url="https://dev.twitch.tv",
                save_label="Save",
                on_save=lambda _client_id, _secret: None,
                on_saved=lambda: None,
            )
            form.prefill("client-id", "client-secret")
            form.frame.pack()
            root.update()
            self.assertEqual(form._secret.cget("show"), "*")
            owner = form._secret
            while not any(isinstance(child, _RoundButton) for child in owner.winfo_children()):
                owner = owner.master
            form._copy_text(form._secret.get(), owner)
            self.assertEqual(root.clipboard_get(), "client-secret")
            self.assertEqual(form._client_id.cget("show"), "*")
        finally:
            root.destroy()

    def test_copy_buttons_stay_visible_in_a_narrow_form(self) -> None:
        root = tk.Tk()
        try:
            holder = tk.Frame(root, width=320, height=560)
            holder.pack_propagate(False)
            holder.pack()
            form = _CredentialForm(
                holder,
                root,
                title="Twitch",
                body="Paste the Twitch app.",
                redirect="http://localhost:4343/callback",
                open_label="Open Twitch",
                open_url="https://dev.twitch.tv",
                save_label="Save",
                on_save=lambda _client_id, _secret: None,
                on_saved=lambda: None,
            )
            form.frame.pack(fill="both", expand=True)
            root.update()
            copies = _copy_buttons(form.frame)
            self.assertGreaterEqual(len(copies), 3)
            right = form.frame.winfo_rootx() + form.frame.winfo_width()
            for button in copies:
                self.assertGreater(button.winfo_width(), 40)
                self.assertLessEqual(button.winfo_rootx() + button.winfo_width(), right + 1)
        finally:
            root.destroy()

    def test_every_button_is_purple(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            copy = _RoundButton(root, text="Copy", command=None)
            save = _RoundButton(root, text="Save Twitch app", command=None, size="regular")
            self.assertEqual(copy._colors()[0], "#9146ff")
            self.assertEqual(save._colors()[0], "#9146ff")
            kinds = {copy.type(item) for item in copy.find_all()}
            self.assertIn("image", kinds)
            self.assertEqual(copy._radius, 10)
            self.assertLess(copy._radius, copy._height // 2)
            self.assertEqual(copy._height, 40)
            self.assertGreaterEqual(copy._pad_x, 22)
            self.assertGreaterEqual(copy._font_size, 13)
        finally:
            root.destroy()
