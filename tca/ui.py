"""Small status window. Updates never pull this window in front of a game."""

from __future__ import annotations

import ctypes
import struct
import sys
import tkinter as tk
import webbrowser
import zlib
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk

from tca.bot import REDIRECT_URL as TWITCH_REDIRECT
from tca.commands import CommandFile, CommandFileError
from tca.request_log import RequestBoard, RequestLogError
from tca.spotify import REDIRECT_URI as SPOTIFY_REDIRECT
from tca.status import AppStatus, StatusRow, status_rows

# Twitch's dark UI colors.
_BG = "#0e0e10"
_CARD = "#18181b"
_FIELD = "#0e0e10"
_TEXT = "#efeff1"
_MUTED = "#adadb8"
_LINE = "#3a3a3d"
_PURPLE = "#9146ff"
_PURPLE_HOVER = "#772ce8"
_PURPLE_PRESSED = "#5c16c5"
_PURPLE_DISABLED = "#3d2866"
_NOTE_HOLD_MS = 3000
_NOTE_FADE_MS = 1000
_NOTE_FADE_STEPS = 10
_TONES = {
    "ok": "#00f593",
    "wait": "#ffca5f",
    "bad": "#eb0400",
    "idle": "#adadb8",
}

_Save = Callable[[str, str], None]


def _assets() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


_LOGO = _assets() / "tca.png"
_LOGO_SIZE = 48


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_up_left = abs(estimate - up_left)
    if distance_left <= distance_up and distance_left <= distance_up_left:
        return left
    if distance_up <= distance_up_left:
        return up
    return up_left


def _rgba_png(path: Path) -> tuple[int, int, list[bytes]] | None:
    """Decode an 8-bit RGBA PNG. Returns None for any other file."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = bit = color = 0
    chunks: list[bytes] = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        start = pos + 8
        chunk = data[start : start + length]
        pos = start + length + 4
        if kind == b"IHDR":
            width, height, bit, color, _compression, png_filter, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            if png_filter != 0 or interlace != 0:
                return None
        elif kind == b"IDAT":
            chunks.append(chunk)
        elif kind == b"IEND":
            break
    if bit != 8 or color != 6 or width < 1 or height < 1:
        return None
    raw = zlib.decompress(b"".join(chunks))
    stride = width * 4
    rows: list[bytes] = []
    previous = bytearray(stride)
    index = 0
    for _y in range(height):
        filter_type = raw[index]
        index += 1
        scanline = bytearray(raw[index : index + stride])
        index += stride
        row = bytearray(stride)
        for x in range(stride):
            left = row[x - 4] if x >= 4 else 0
            up = previous[x]
            up_left = previous[x - 4] if x >= 4 else 0
            value = scanline[x]
            if filter_type == 1:
                value = (value + left) & 255
            elif filter_type == 2:
                value = (value + up) & 255
            elif filter_type == 3:
                value = (value + ((left + up) // 2)) & 255
            elif filter_type == 4:
                value = (value + _paeth(left, up, up_left)) & 255
            elif filter_type != 0:
                return None
            row[x] = value
        rows.append(bytes(row))
        previous = row
    return width, height, rows


def _header_logo(path: Path, size: int) -> tk.PhotoImage | None:
    """Shrink the app logo so the header can show it beside the title."""
    decoded = _rgba_png(path)
    if decoded is None:
        return None
    width, height, rows = decoded
    image = tk.PhotoImage(width=size, height=size)
    lines: list[str] = []
    for y in range(size):
        y0 = y * height // size
        y1 = max(y0 + 1, (y + 1) * height // size)
        cells: list[str] = []
        for x in range(size):
            x0 = x * width // size
            x1 = max(x0 + 1, (x + 1) * width // size)
            red = green = blue = weight = 0
            for py in range(y0, y1):
                row = rows[py]
                for px in range(x0, x1):
                    offset = px * 4
                    alpha = row[offset + 3]
                    red += row[offset] * alpha
                    green += row[offset + 1] * alpha
                    blue += row[offset + 2] * alpha
                    weight += alpha
            if weight < 128:
                cells.append("{}")
            else:
                cells.append(f"#{red // weight:02x}{green // weight:02x}{blue // weight:02x}")
        lines.append(" ".join(cells))
    image.put(lines)
    return image


class StatusWindow:
    def __init__(
        self,
        status: AppStatus,
        on_link_twitch: Callable[[], None],
        on_link_spotify: Callable[[], None],
        on_save_twitch: _Save | None = None,
        on_save_spotify: _Save | None = None,
        commands: CommandFile | None = None,
        requests: RequestBoard | None = None,
    ) -> None:
        self._status = status
        self._actions = {"Twitch": on_link_twitch, "Spotify": on_link_spotify}
        if sys.platform == "win32":
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TCA.TwitchChatAssistant")
        self.root = tk.Tk()
        self.root.title("TCA")
        self.root.configure(bg=_BG)
        self._apply_icon()
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._styles()

        shell = tk.Frame(self.root, bg=_BG, padx=18, pady=16)
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=_BG)
        header.pack(fill="x")
        self._mark_image = _header_logo(_LOGO, _LOGO_SIZE)
        if self._mark_image is not None:
            tk.Label(
                header,
                image=self._mark_image,
                bg=_BG,
                bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(0, 10))
        else:
            mark = tk.Frame(header, bg=_PURPLE, width=14, height=14, highlightthickness=0)
            mark.pack(side="left", padx=(0, 8), pady=(6, 0))
            mark.pack_propagate(False)
        titles = tk.Frame(header, bg=_BG)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text="TCA", bg=_BG, fg=_TEXT, font=("Segoe UI", 18, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            titles,
            text="Twitch Chat Assistant",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(fill="x")
        tk.Frame(shell, bg=_PURPLE, height=2, highlightthickness=0).pack(fill="x", pady=(12, 12))

        self._rows: dict[str, dict[str, tk.Widget]] = {}
        for name in ("Program", "Twitch", "Spotify"):
            self._rows[name] = self._build_row(shell, name)

        self._footer = tk.Label(
            shell,
            text="Leave this window running in the background to maintain TCA Bot functions and commands.",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=480,
        )
        self._twitch_form = _CredentialForm(
            shell,
            self.root,
            title="Twitch application",
            body=(
                "Twitch requires an application before a sign-in can work. Create one named TCA, "
                "category Chat Bot, and add this redirect URL. This is once for this copy of TCA. "
                "Afterward, Link Twitch is the sign-in."
            ),
            redirect=TWITCH_REDIRECT,
            open_label="Open the Twitch console",
            open_url="https://dev.twitch.tv/console/apps",
            save_label="Save Twitch app",
            on_save=on_save_twitch or _noop_save,
            on_saved=self._sync,
        )
        self._spotify_form = _CredentialForm(
            shell,
            self.root,
            title="Spotify",
            body=(
                "Create an app on your Spotify account, add this redirect URL, and paste the client ID "
                "and secret. TCA will then ask you to approve Spotify."
            ),
            redirect=SPOTIFY_REDIRECT,
            open_label="Open the Spotify dashboard",
            open_url="https://developer.spotify.com/dashboard",
            save_label="Save Spotify app",
            on_save=on_save_spotify or _noop_save,
            on_saved=self._sync,
        )
        self._requests = _RequestPanel(shell, requests, on_changed=self._sync) if requests is not None else None
        self._commands = _CommandPanel(shell, commands, on_changed=self._sync) if commands is not None else None
        anchor = self._footer
        if self._requests is not None:
            anchor = self._requests.frame
        elif self._commands is not None:
            anchor = self._commands.frame
        self._twitch_form.anchor(anchor)
        self._spotify_form.anchor(anchor)
        if self._requests is not None:
            self._requests.frame.pack(fill="x", pady=(8, 0))
        if self._commands is not None:
            self._commands.frame.pack(fill="x", pady=(8, 0))
        self._footer.pack(fill="x", pady=(14, 0))

        self._placed = False
        self._size: tuple[int, int] | None = None

    def prefill(
        self,
        *,
        twitch_id: str = "",
        twitch_secret: str = "",
        spotify_id: str = "",
        spotify_secret: str = "",
    ) -> None:
        self._twitch_form.prefill(twitch_id, twitch_secret)
        self._spotify_form.prefill(spotify_id, spotify_secret)

    def run(self) -> None:
        self._sync()
        self._dark_title_bar()
        icon_ico = _assets() / "tca.ico"
        if icon_ico.exists():
            self._set_windows_icon(icon_ico)
        self.root.after(400, self._refresh)
        self.root.mainloop()

    def _apply_icon(self) -> None:
        icon_png = _assets() / "tca.png"
        if not icon_png.exists():
            return
        self._icon_image = tk.PhotoImage(file=str(icon_png))
        self.root.iconphoto(True, self._icon_image)
        icon_ico = icon_png.with_suffix(".ico")
        if icon_ico.exists():
            try:
                self.root.iconbitmap(default=str(icon_ico))
            except tk.TclError:
                pass
            self._set_windows_icon(icon_ico)

    def _set_windows_icon(self, icon_ico: Path) -> None:
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        big = user32.LoadImageW(None, str(icon_ico), 1, 32, 32, 0x10)
        small = user32.LoadImageW(None, str(icon_ico), 1, 16, 16, 0x10)
        if not big and not small:
            return
        self.root.update_idletasks()
        hwnds = [int(self.root.winfo_id())]
        parent = user32.GetParent(hwnds[0])
        if parent:
            hwnds.append(int(parent))
        for hwnd in hwnds:
            if big:
                user32.SendMessageW(hwnd, 0x0080, 1, big)
            if small:
                user32.SendMessageW(hwnd, 0x0080, 0, small)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self._purple_button(style, "Link.TButton", padding=(10, 2))
        self._purple_button(style, "Primary.TButton", padding=(12, 6))
        self._purple_button(style, "Ghost.TButton", padding=(10, 4))

    def _purple_button(self, style: ttk.Style, name: str, *, padding: tuple[int, int]) -> None:
        """Flat Twitch purple, the same color as the logo and the header line.

        The clam theme lightens a button unless the border shades match the face.
        """
        style.configure(
            name,
            padding=padding,
            background=_PURPLE,
            foreground="#ffffff",
            borderwidth=0,
            relief="flat",
            focusthickness=0,
            focuscolor=_PURPLE,
            bordercolor=_PURPLE,
            lightcolor=_PURPLE,
            darkcolor=_PURPLE,
            font=("Segoe UI", 9),
        )
        style.map(
            name,
            background=[("pressed", _PURPLE_PRESSED), ("active", _PURPLE_HOVER), ("disabled", _PURPLE_DISABLED)],
            foreground=[("disabled", _MUTED)],
            bordercolor=[("pressed", _PURPLE_PRESSED), ("active", _PURPLE_HOVER), ("disabled", _PURPLE_DISABLED)],
            lightcolor=[("pressed", _PURPLE_PRESSED), ("active", _PURPLE_HOVER), ("disabled", _PURPLE_DISABLED)],
            darkcolor=[("pressed", _PURPLE_PRESSED), ("active", _PURPLE_HOVER), ("disabled", _PURPLE_DISABLED)],
            focuscolor=[("pressed", _PURPLE_PRESSED), ("active", _PURPLE_HOVER), ("disabled", _PURPLE_DISABLED)],
        )

    def _build_row(self, parent: tk.Frame, name: str) -> dict[str, tk.Widget]:
        frame = tk.Frame(parent, bg=_BG)
        frame.pack(fill="x", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        dot = tk.Frame(frame, bg=_TONES["wait"], width=10, height=10, highlightthickness=0)
        dot.grid(row=0, column=0, padx=(0, 8), pady=6, sticky="w")
        dot.grid_propagate(False)

        tk.Label(frame, text=name, bg=_BG, fg=_TEXT, font=("Segoe UI", 11, "bold"), width=10, anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        value = tk.Label(frame, text="…", bg=_BG, fg=_TONES["wait"], font=("Segoe UI", 11), anchor="w")
        value.grid(row=0, column=2, sticky="w", padx=(8, 8))

        button = ttk.Button(frame, text="Link", style="Link.TButton", command=self._actions.get(name))
        button.grid(row=0, column=3, sticky="e")
        button.grid_remove()

        detail = tk.Label(
            frame,
            text="",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=460,
        )
        detail.grid(row=1, column=1, columnspan=3, sticky="ew")
        detail.grid_remove()
        return {"dot": dot, "value": value, "button": button, "detail": detail}

    def _refresh(self) -> None:
        try:
            if not self.root.winfo_exists():
                return
            self._sync()
            # Reschedule only. Do not lift or focus the window.
            self.root.after(400, self._refresh)
        except tk.TclError:
            return

    def _sync(self) -> None:
        snapshot = self._status.snapshot()
        self._apply(status_rows(snapshot))
        self._twitch_form.set_visible(snapshot.twitch_state == "needs_setup")
        self._spotify_form.set_visible(snapshot.spotify_state == "needs_setup")
        if self._requests is not None:
            self._requests.sync()
        if self._commands is not None:
            self._commands.sync()
        self._fit()

    def _fit(self) -> None:
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 520)
        height = self.root.winfo_reqheight()
        size = (width, height)
        if size == self._size:
            return
        self._size = size
        if self._placed:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
        else:
            x, y = 120, 120
            self._placed = True
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _apply(self, rows: list[StatusRow]) -> None:
        for row in rows:
            widgets = self._rows[row.name]
            color = _TONES[row.tone]
            value: tk.Label = widgets["value"]  # type: ignore[assignment]
            dot: tk.Frame = widgets["dot"]  # type: ignore[assignment]
            detail: tk.Label = widgets["detail"]  # type: ignore[assignment]
            button: ttk.Button = widgets["button"]  # type: ignore[assignment]

            if value.cget("text") != row.value or value.cget("fg") != color:
                value.configure(text=row.value, fg=color)
            if str(dot.cget("bg")) != color:
                dot.configure(bg=color)

            if row.detail:
                detail.configure(text=row.detail)
                if not detail.winfo_ismapped():
                    detail.grid()
            elif detail.winfo_ismapped():
                detail.grid_remove()

            if row.button:
                state = tk.NORMAL if row.button_enabled else tk.DISABLED
                button.configure(text=row.button, state=state)
                if not button.winfo_ismapped():
                    button.grid()
            elif button.winfo_ismapped():
                button.grid_remove()

    def _dark_title_bar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError):
            return


class _CredentialForm:
    def __init__(
        self,
        parent: tk.Frame,
        root: tk.Tk,
        *,
        title: str,
        body: str,
        redirect: str,
        open_label: str,
        open_url: str,
        save_label: str,
        on_save: _Save,
        on_saved: Callable[[], None],
    ) -> None:
        self._root = root
        self._redirect = redirect
        self._open_url = open_url
        self._on_save = on_save
        self._on_saved = on_saved
        self._before: tk.Widget | None = None
        self.frame = tk.Frame(
            parent,
            bg=_CARD,
            highlightbackground=_LINE,
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        tk.Label(self.frame, text=title, bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(
            self.frame,
            text=body,
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        ).pack(fill="x", pady=(4, 8))

        redirect_row = tk.Frame(self.frame, bg=_CARD)
        redirect_row.pack(fill="x")
        redirect_entry = tk.Entry(
            redirect_row,
            font=("Segoe UI", 9),
            readonlybackground=_FIELD,
            bg=_FIELD,
            fg=_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_LINE,
            highlightcolor=_PURPLE,
        )
        redirect_entry.insert(0, redirect)
        redirect_entry.configure(state="readonly")
        redirect_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._copy_button = ttk.Button(redirect_row, text="Copy", style="Ghost.TButton", command=self._copy)
        self._copy_button.pack(side="left", padx=(8, 0))

        ttk.Button(self.frame, text=open_label, style="Ghost.TButton", command=self._open).pack(anchor="w", pady=(8, 4))
        self._client_id = self._field("Client ID")
        self._secret = self._field("Client secret")
        ttk.Button(self.frame, text=save_label, style="Primary.TButton", command=self._save).pack(anchor="w", pady=(8, 0))
        self._note = tk.Label(
            self.frame,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        )

    def anchor(self, before: tk.Widget) -> None:
        self._before = before

    def prefill(self, client_id: str, secret: str) -> None:
        if client_id and not self._client_id.get():
            self._client_id.insert(0, client_id)
        if secret and not self._secret.get():
            self._secret.insert(0, secret)

    def set_visible(self, visible: bool) -> None:
        mapped = bool(self.frame.winfo_ismapped())
        if visible and not mapped and self._before is not None:
            self.frame.pack(fill="x", pady=(4, 8), before=self._before)
        elif not visible and mapped:
            self.frame.pack_forget()
            self._set_note("")
            self._copy_button.configure(text="Copy")

    def _field(self, label: str) -> tk.Entry:
        row = tk.Frame(self.frame, bg=_CARD)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=label, bg=_CARD, fg=_TEXT, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
        entry = tk.Entry(
            row,
            font=("Segoe UI", 10),
            bg=_FIELD,
            fg=_TEXT,
            insertbackground=_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_LINE,
            highlightcolor=_PURPLE,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        return entry

    def _copy(self) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(self._redirect)
        self._copy_button.configure(text="Copied")

    def _open(self) -> None:
        try:
            webbrowser.open(self._open_url)
        except webbrowser.Error:
            self._set_note(f"Open {self._open_url} in a browser.")

    def _save(self) -> None:
        client_id = self._client_id.get().strip()
        secret = self._secret.get().strip()
        if not client_id or not secret:
            self._set_note("Paste both the client ID and the client secret.")
            return
        self._set_note("")
        self._on_save(client_id, secret)
        self._on_saved()

    def _set_note(self, text: str) -> None:
        if text:
            self._note.configure(text=text)
            if not self._note.winfo_ismapped():
                self._note.pack(fill="x", pady=(6, 0))
        elif self._note.winfo_ismapped():
            self._note.pack_forget()


class _FadingNote:
    """A status line that stays readable, then fades into the card."""

    def __init__(self, note: tk.Label, on_changed: Callable[[], None]) -> None:
        self._note = note
        self._on_changed = on_changed
        self._note_color = _TONES["ok"]
        self._fade_after: str | None = None

    def set(self, text: str, *, ok: bool = False) -> None:
        self._cancel()
        if not text:
            if self._note.winfo_ismapped():
                self._note.pack_forget()
            return
        self._note_color = _TONES["ok"] if ok else _TONES["bad"]
        self._note.configure(text=text, fg=self._note_color)
        if not self._note.winfo_ismapped():
            self._note.pack(fill="x", pady=(6, 0))
        self._fade_after = self._note.after(_NOTE_HOLD_MS, self._fade_step)

    def _fade_step(self, step: int = 1) -> None:
        self._fade_after = None
        try:
            if not self._note.winfo_exists():
                return
        except tk.TclError:
            return
        if step > _NOTE_FADE_STEPS:
            if self._note.winfo_ismapped():
                self._note.pack_forget()
            self._on_changed()
            return
        self._note.configure(fg=_blend(self._note_color, _CARD, step / _NOTE_FADE_STEPS))
        self._fade_after = self._note.after(
            _NOTE_FADE_MS // _NOTE_FADE_STEPS,
            lambda next_step=step + 1: self._fade_step(next_step),
        )

    def _cancel(self) -> None:
        after_id = self._fade_after
        self._fade_after = None
        if after_id is None:
            return
        try:
            self._note.after_cancel(after_id)
        except tk.TclError:
            return


def _blend(start: str, end: str, amount: float) -> str:
    def channel(first: str, second: str) -> int:
        return round(int(first, 16) + (int(second, 16) - int(first, 16)) * amount)

    return f"#{channel(start[1:3], end[1:3]):02x}{channel(start[3:5], end[3:5]):02x}{channel(start[5:7], end[5:7]):02x}"


class _RequestPanel:
    """Song requests still waiting, each with a way to drop it from the list."""

    def __init__(self, parent: tk.Frame, requests: RequestBoard, on_changed: Callable[[], None]) -> None:
        self._requests = requests
        self._on_changed = on_changed
        self._shown: tuple[tuple[int, str, str, str, str], ...] | None = None
        self.frame = tk.Frame(
            parent,
            bg=_CARD,
            highlightbackground=_LINE,
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        tk.Label(self.frame, text="Song requests", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            fill="x"
        )
        tk.Label(
            self.frame,
            text="Viewers' songs still waiting. Remove drops one from this list and from !queue.",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        ).pack(fill="x", pady=(4, 8))
        self._list = tk.Frame(self.frame, bg=_CARD)
        self._list.pack(fill="x")
        self._note = tk.Label(
            self.frame,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        )
        self._fading = _FadingNote(self._note, on_changed)

    def sync(self) -> None:
        shown = self._requests.snapshot()
        if shown == self._shown:
            return
        self._shown = shown
        for child in self._list.winfo_children():
            child.destroy()
        if not shown:
            tk.Label(
                self._list,
                text="No song requests are waiting.",
                bg=_CARD,
                fg=_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(fill="x")
            return
        for index, _track_id, title, artist, viewer in shown:
            row = tk.Frame(self._list, bg=_CARD)
            row.pack(fill="x", pady=(0, 4))
            tk.Label(
                row,
                text=_short_reply(f"{title} - {artist}"),
                bg=_CARD,
                fg=_TEXT,
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=viewer,
                bg=_CARD,
                fg=_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(8, 8))
            ttk.Button(
                row,
                text="Remove",
                style="Link.TButton",
                command=lambda item=index: self._remove(item),
            ).pack(side="right")

    def _remove(self, index: int) -> None:
        try:
            removed = self._requests.remove(index)
        except RequestLogError as exc:
            self._set_note(str(exc))
            return
        self._set_note(f"Removed {removed.title}. Spotify may still play it.", ok=True)
        self._on_changed()

    def _set_note(self, text: str, *, ok: bool = False) -> None:
        self._fading.set(text, ok=ok)


class _CommandPanel:
    """Add and remove text commands. Built-in Spotify commands stay."""

    def __init__(self, parent: tk.Frame, commands: CommandFile, on_changed: Callable[[], None]) -> None:
        self._commands = commands
        self._on_changed = on_changed
        self._shown: tuple[tuple[str, str], ...] | None = None
        self._handlers: tuple[str, ...] | None = None
        self.frame = tk.Frame(
            parent,
            bg=_CARD,
            highlightbackground=_LINE,
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        tk.Label(self.frame, text="Commands", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            fill="x"
        )
        tk.Label(
            self.frame,
            text="Add a name such as !discord and the reply chat should see.",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        ).pack(fill="x", pady=(4, 8))
        self._built_in = tk.Label(
            self.frame,
            text="",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        )
        self._built_in.pack(fill="x", pady=(0, 6))
        self._list = tk.Frame(self.frame, bg=_CARD)
        self._list.pack(fill="x")
        self._name = self._field("Name")
        self._reply = self._field("Reply")
        self._name.bind("<Return>", self._add_event)
        self._reply.bind("<Return>", self._add_event)
        ttk.Button(self.frame, text="Add command", style="Primary.TButton", command=self._add).pack(
            anchor="w", pady=(8, 0)
        )
        self._note = tk.Label(
            self.frame,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=470,
        )
        self._fading = _FadingNote(self._note, on_changed)

    def sync(self) -> None:
        text = tuple(self._commands.text_commands())
        handlers = tuple(self._commands.handler_names())
        if text == self._shown and handlers == self._handlers:
            return
        self._shown = text
        self._handlers = handlers
        built_in = ", ".join(handlers) if handlers else "none"
        self._built_in.configure(text=f"Built in: {built_in}")
        for child in self._list.winfo_children():
            child.destroy()
        if not text:
            tk.Label(
                self._list,
                text="No extra commands yet.",
                bg=_CARD,
                fg=_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(fill="x", pady=(0, 6))
            return
        for name, reply in text:
            row = tk.Frame(self._list, bg=_CARD)
            row.pack(fill="x", pady=(0, 4))
            tk.Label(
                row,
                text=name,
                bg=_CARD,
                fg=_TEXT,
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=_short_reply(reply),
                bg=_CARD,
                fg=_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(8, 8))
            ttk.Button(
                row,
                text="Remove",
                style="Link.TButton",
                command=lambda command=name: self._remove(command),
            ).pack(side="right")

    def _field(self, label: str) -> tk.Entry:
        row = tk.Frame(self.frame, bg=_CARD)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=label, bg=_CARD, fg=_TEXT, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
        entry = tk.Entry(
            row,
            font=("Segoe UI", 10),
            bg=_FIELD,
            fg=_TEXT,
            insertbackground=_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_LINE,
            highlightcolor=_PURPLE,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        return entry

    def _add_event(self, _event: tk.Event[tk.Misc]) -> str:
        self._add()
        return "break"

    def _add(self) -> None:
        try:
            name = self._commands.add_text_command(self._name.get(), self._reply.get())
        except CommandFileError as exc:
            self._set_note(str(exc))
            return
        self._name.delete(0, "end")
        self._reply.delete(0, "end")
        self._set_note(f"Added {name}.", ok=True)
        self._on_changed()

    def _remove(self, name: str) -> None:
        try:
            self._commands.remove_text_command(name)
        except CommandFileError as exc:
            self._set_note(str(exc))
            return
        self._set_note(f"Removed {name}.", ok=True)
        self._on_changed()

    def _set_note(self, text: str, *, ok: bool = False) -> None:
        self._fading.set(text, ok=ok)


def _short_reply(text: str, limit: int = 42) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _noop_save(_client_id: str, _secret: str) -> None:
    return
