"""Small status window. Updates never pull this window in front of a game."""

from __future__ import annotations

import base64
import ctypes
import logging
import queue
import sys
import threading
import tkinter as tk
from io import BytesIO
import tkinter.font as tkfont
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tca.artwork import card_images, fetch_album_art
from tca.bot import REDIRECT_URL as TWITCH_REDIRECT
from tca.commands import CommandFile, CommandFileError
from tca.request_log import RequestBoard, RequestLogError
from tca.spotify import REDIRECT_URI as SPOTIFY_REDIRECT
from tca.now_playing import WidgetSite
from tca.status import AppStatus, StatusRow, status_rows

logger = logging.getLogger("tca")

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
_GOLDEN = (1 + 5 ** 0.5) / 2
_LIVE_MIN = 560
_SETUP_MIN = 348
_LIVE_WRAP = 520
_SETUP_WRAP = 300
_COMMAND_LIST_MAX = 200


def _assets() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


_LOGO = _assets() / "tca.png"
_LOGO_SIZE = 48


def _header_logo(path: Path, size: int) -> tk.PhotoImage | None:
    """Shrink the app logo so the header can show it beside the title."""
    try:
        image = Image.open(path).convert("RGBA")
    except OSError:
        return None
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buffer.getvalue()).decode("ascii"))


def _smooth_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs: object) -> int:
    """A rounded rectangle. Duplicate corners make Tk draw a smooth curve."""
    curve = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + curve, y1, x1 + curve, y1,
        x2 - curve, y1, x2 - curve, y1,
        x2, y1, x2, y1 + curve, x2, y1 + curve,
        x2, y2 - curve, x2, y2 - curve,
        x2, y2, x2 - curve, y2, x2 - curve, y2,
        x1 + curve, y2, x1 + curve, y2,
        x1, y2, x1, y2 - curve, x1, y2 - curve,
        x1, y1 + curve, x1, y1 + curve, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


_PILL_PNG: dict[tuple[int, int, str, str, int, int], bytes] = {}
_LABEL_FONTS: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_BUTTON_RADIUS = 10
_BUTTON_FONT = 13


def _lerp(start: tuple[int, int, int], end: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(int(start[channel] + (end[channel] - start[channel]) * amount) for channel in range(3))


def _label_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    cached = _LABEL_FONTS.get(size)
    if cached is not None:
        return cached
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.load_default()
    for name in ("segoeuisb.ttf", "segoeui.ttf"):
        path = Path(r"C:\Windows\Fonts") / name
        if path.exists():
            font = ImageFont.truetype(str(path), size)
            break
    _LABEL_FONTS[size] = font
    return font


def _label_width(text: str, size: int) -> int:
    return int(_label_font(size).getlength(text))


def _pill_png(width: int, height: int, state: str, text: str, font_size: int, radius: int) -> bytes:
    """A rounded rectangle with a left-to-right purple gradient and a bright outline."""
    key = (width, height, state, text, font_size, radius)
    cached = _PILL_PNG.get(key)
    if cached is not None:
        return cached
    if state == "disabled":
        left, right = (78, 62, 112), (62, 48, 96)
        rim_top, rim_bottom = (132, 116, 168), (96, 80, 132)
    elif state == "pressed":
        left, right = (78, 28, 196), (58, 16, 168)
        rim_top, rim_bottom = (150, 110, 230), (96, 60, 190)
    elif state == "hover":
        left, right = (132, 72, 255), (104, 44, 242)
        rim_top, rim_bottom = (214, 190, 255), (156, 110, 250)
    else:
        left, right = (112, 48, 250), (86, 28, 226)
        rim_top, rim_bottom = (196, 168, 255), (140, 96, 245)
    scale = 3
    wide, tall = width * scale, height * scale
    pad = 2 * scale
    stroke = max(4, int(1.7 * scale))
    image = Image.new("RGBA", (wide, tall), (0, 0, 0, 0))
    outer = (pad, pad, wide - pad - 1, tall - pad - 1)
    radius = min(radius * scale, (outer[3] - outer[1]) / 2)
    inner = (outer[0] + stroke, outer[1] + stroke, outer[2] - stroke, outer[3] - stroke)
    outer_mask = Image.new("L", (wide, tall), 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle(outer, radius=radius, fill=255)
    inner_mask = Image.new("L", (wide, tall), 0)
    ImageDraw.Draw(inner_mask).rounded_rectangle(inner, radius=max(radius - stroke, 1), fill=255)
    ring = outer_mask.copy()
    ring.paste(0, mask=inner_mask)
    shadow = Image.new("RGBA", (wide, tall), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (outer[0], outer[1] + scale, outer[2], outer[3] + scale),
        radius=radius,
        fill=(0, 0, 0, 120),
    )
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(2 * scale)))
    rim_color = Image.new("RGBA", (wide, tall))
    rim_px = rim_color.load()
    for y in range(tall):
        color = _lerp(rim_top, rim_bottom, y / max(tall - 1, 1))
        for x in range(wide):
            rim_px[x, y] = (*color, 255)
    blank = Image.new("RGBA", (wide, tall), (0, 0, 0, 0))
    image.alpha_composite(Image.composite(rim_color, blank, ring))
    fill = Image.new("RGBA", (wide, tall))
    fill_px = fill.load()
    for x in range(wide):
        color = _lerp(left, right, x / max(wide - 1, 1))
        for y in range(tall):
            shade = 1.06 - 0.12 * (y / max(tall - 1, 1))
            fill_px[x, y] = tuple(max(0, min(255, int(channel * shade))) for channel in color) + (255,)
    image.alpha_composite(Image.composite(fill, blank, inner_mask))
    small = image.resize((width, height), Image.Resampling.LANCZOS)
    # Draw the label after the resize so the letters are not blurred with the gradient.
    label = ImageDraw.Draw(small)
    font = _label_font(font_size)
    origin = (width / 2, height / 2 + (1 if state == "pressed" else 0))
    if state == "disabled":
        label.text(origin, text, font=font, fill=(232, 226, 240, 255), anchor="mm")
    else:
        label.text((origin[0], origin[1] + 1), text, font=font, fill=(28, 0, 56, 230), anchor="mm")
        label.text(origin, text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    buffer = BytesIO()
    small.save(buffer, format="PNG")
    data = buffer.getvalue()
    _PILL_PNG[key] = data
    return data


def _pill_button(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    text: str,
    *,
    pressed: bool = False,
    disabled: bool = False,
    hover: bool = False,
    font_size: int = _BUTTON_FONT,
    radius: int = _BUTTON_RADIUS,
    tags: str = "",
) -> tuple[float, float]:
    """Draw the gradient button, with the label painted at a sharp size."""
    width = max(int(round(x2 - x1)), 8)
    height = max(int(round(y2 - y1)), 8)
    if disabled:
        state = "disabled"
    elif pressed:
        state = "pressed"
    elif hover:
        state = "hover"
    else:
        state = "normal"
    photo = tk.PhotoImage(
        data=base64.b64encode(_pill_png(width, height, state, text, font_size, radius)).decode("ascii")
    )
    faces = getattr(canvas, "_pill_faces", None)
    if faces is None:
        faces = {}
        canvas._pill_faces = faces
    faces[tags or "face"] = photo
    options: dict[str, object] = {"tags": tags} if tags else {}
    canvas.create_image(x1, y1, image=photo, anchor="nw", **options)
    return (x1 + x2) / 2, (y1 + y2) / 2 + (1.0 if pressed else 0.0)


def _page_color(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return _BG


class _Card(tk.Frame):
    """A rounded panel. Put the contents in ``body``."""

    def __init__(self, parent: tk.Misc, *, radius: int = 18, expand: bool = False) -> None:
        page = _page_color(parent)
        super().__init__(parent, bg=page, highlightthickness=0, bd=0)
        self._radius = radius
        self._expand = expand
        # Keep the contents inside the curve so the square body does not cover the corners.
        self._padx = radius
        self._pady = radius
        self._drawn: tuple[int, int] | None = None
        self._busy = False
        self._canvas = tk.Canvas(self, bg=page, highlightthickness=0, bd=0, height=48)
        self._canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=_CARD, highlightthickness=0, bd=0)
        self._window = self._canvas.create_window(self._padx, self._pady, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._fit)

    def _fit(self, _event: object = None) -> None:
        if self._busy:
            return
        width = self.winfo_width()
        if width < 40:
            return
        self._busy = True
        try:
            inner_width = max(width - self._padx * 2, 1)
            if self._expand:
                height = self.winfo_height()
                if height < 40:
                    return
                inner_height = max(height - self._pady * 2, 1)
                self._canvas.itemconfigure(self._window, width=inner_width, height=inner_height)
            else:
                self._canvas.itemconfigure(self._window, width=inner_width)
                self.body.update_idletasks()
                height = max(self.body.winfo_reqheight() + self._pady * 2, self._radius * 2)
                if int(self._canvas.cget("height")) != height:
                    self._canvas.configure(height=height)
            if self._drawn == (width, height):
                return
            self._drawn = (width, height)
            shape = _smooth_rect(
                self._canvas,
                1,
                1,
                width - 2,
                height - 2,
                self._radius,
                fill=_CARD,
                outline=_LINE,
                width=1,
                tags="shape",
            )
            self._canvas.tag_lower(shape)
            for older in self._canvas.find_withtag("shape"):
                if older != shape:
                    self._canvas.delete(older)
        finally:
            self._busy = False


class _EntryBar(tk.Frame):
    """A rounded text box. The entry itself is ``entry``."""

    def __init__(self, parent: tk.Misc, *, show: str = "", readonly: bool = False) -> None:
        page = _page_color(parent)
        super().__init__(parent, bg=page, highlightthickness=0, bd=0)
        self._outline = _LINE
        self._radius = 10
        self._pad_y = 4
        self._busy = False
        self._drawn: tuple[int, int, str] | None = None
        self._canvas = tk.Canvas(self, bg=page, highlightthickness=0, bd=0, height=32)
        self._canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=_FIELD, highlightthickness=0, bd=0)
        self._window = self._canvas.create_window(self._radius, self._pad_y, window=self.body, anchor="nw")
        self.entry = tk.Entry(
            self.body,
            font=("Segoe UI", 10),
            bg=_FIELD,
            fg=_TEXT,
            insertbackground=_TEXT,
            readonlybackground=_FIELD,
            relief="flat",
            highlightthickness=0,
            show=show,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=1)
        if readonly:
            self.entry.configure(state="readonly")
        self.entry.bind("<FocusIn>", lambda _event: self.set_outline(_PURPLE))
        self.entry.bind("<FocusOut>", lambda _event: self.set_outline(_LINE))
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._fit)
        _install_click_away(self.winfo_toplevel())

    def set_outline(self, color: str) -> None:
        self._outline = color
        self._drawn = None
        self._fit()

    def _fit(self, _event: object = None) -> None:
        if self._busy:
            return
        width = self.winfo_width()
        if width < 40:
            return
        self._busy = True
        try:
            self.body.update_idletasks()
            height = max(self.body.winfo_reqheight() + self._pad_y * 2, 32)
            inner_width = max(width - self._radius * 2, 1)
            self._canvas.itemconfigure(self._window, width=inner_width)
            if int(self._canvas.cget("height")) != height:
                self._canvas.configure(height=height)
            if self._drawn == (width, height, self._outline):
                return
            self._drawn = (width, height, self._outline)
            self._canvas.delete("shape")
            _smooth_rect(
            self._canvas,
            1,
            1,
            width - 2,
            height - 2,
            self._radius,
            fill=_FIELD,
            outline=self._outline,
            width=1,
            tags="shape",
            )
            self._canvas.tag_lower("shape")
        finally:
            self._busy = False


_click_away_ready = False


def _entry_bar_containing(widget: tk.Misc) -> _EntryBar | None:
    current: tk.Misc | None = widget
    while current is not None:
        if isinstance(current, _EntryBar):
            return current
        parent = current.winfo_parent()
        if not parent:
            return None
        try:
            current = current.nametowidget(parent)
        except tk.TclError:
            return None
    return None


def _install_click_away(root: tk.Misc) -> None:
    """Leave a text field when the click lands outside its bar."""
    global _click_away_ready
    if _click_away_ready:
        return
    _click_away_ready = True

    def on_click(event: tk.Event) -> None:
        widget = event.widget
        try:
            bar = _entry_bar_containing(widget)
        except tk.TclError:
            return
        if bar is not None:
            if widget is not bar.entry:
                try:
                    bar.entry.focus_set()
                except tk.TclError:
                    return
            return
        try:
            widget.winfo_toplevel().focus_set()
        except tk.TclError:
            return

    root.bind_all("<Button-1>", on_click, add="+")


class _RoundButton(tk.Canvas):
    """A purple rounded rectangle: a left-to-right gradient with a bright outline."""

    def __init__(self, parent: tk.Misc, *, text: str, command: Callable[[], None] | None, size: str = "small") -> None:
        self._page = _page_color(parent)
        self._text = text
        self._command = command
        self._state = "normal"
        self._pressed = False
        self._inside = False
        self._font_size = _BUTTON_FONT
        self._pad_x = 28 if size == "regular" else 22
        self._height = 40
        self._radius = _BUTTON_RADIUS
        super().__init__(parent, bg=self._page, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self._resize()
        self._draw()

    def cget(self, key: str) -> str:
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return str(super().cget(key))

    def configure(self, cnf: dict[str, object] | None = None, **kwargs: object) -> None:  # type: ignore[override]
        if cnf:
            kwargs.update(cnf)
        text = kwargs.pop("text", None)
        state = kwargs.pop("state", None)
        if kwargs:
            super().configure(**kwargs)  # type: ignore[arg-type]
        if text is not None:
            self._text = str(text)
        if state is not None:
            self._state = "disabled" if str(state) == tk.DISABLED else "normal"
        self._resize()
        self._draw()

    config = configure

    def _resize(self) -> None:
        width = _label_width(self._text, self._font_size) + self._pad_x * 2
        super().configure(
            width=max(width, self._height),
            height=self._height,
            cursor="hand2" if self._state == "normal" else "arrow",
        )

    def _colors(self) -> tuple[str, str, str]:
        if self._state != "normal":
            return _PURPLE_DISABLED, _PURPLE_DISABLED, _MUTED
        if self._pressed:
            return _PURPLE_PRESSED, _PURPLE_PRESSED, "#ffffff"
        if self._inside:
            return _PURPLE_HOVER, _PURPLE_HOVER, "#ffffff"
        return _PURPLE, _PURPLE, "#ffffff"

    def _draw(self) -> None:
        self.delete("all")
        width = max(int(self.cget("width")), 2)
        height = max(int(self.cget("height")), 2)
        _fill, _outline, _text_color = self._colors()
        _pill_button(
            self,
            1,
            1,
            width - 2,
            height - 2,
            self._text,
            pressed=self._pressed,
            disabled=self._state != "normal",
            hover=self._inside and self._state == "normal",
            font_size=self._font_size,
            radius=self._radius,
        )

    def _enter(self, _event: object) -> None:
        self._inside = True
        self._draw()

    def _leave(self, _event: object) -> None:
        self._inside = False
        if not self._pressed:
            self._draw()

    def _press(self, _event: object) -> None:
        if self._state != "normal":
            return
        self._pressed = True
        self._draw()

    def _release(self, event: tk.Event) -> None:
        pressed = self._pressed
        self._pressed = False
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._inside = inside
        self._draw()
        if pressed and inside and self._state == "normal" and self._command is not None:
            self._command()


def _offscreen_origin() -> str:
    """A spot past every monitor, so the window can be measured before it is shown."""
    if sys.platform == "win32":
        metrics = ctypes.windll.user32.GetSystemMetrics
        x = int(metrics(76) + metrics(78) + 80)
        y = int(metrics(77))
        return f"+{x}+{y}"
    return "+10000+0"


def golden_window(needed_w: int, needed_h: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Smallest golden rectangle that holds the content and still fits on the screen."""
    width = max(needed_w, round(needed_h * _GOLDEN))
    height = max(needed_h, round(width / _GOLDEN))
    screen_w = max(screen_w, 1)
    screen_h = max(screen_h, 1)
    if width <= screen_w and height <= screen_h:
        return width, height
    scale = min(screen_w / width, screen_h / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


class StatusWindow:
    def __init__(
        self,
        status: AppStatus,
        on_link_twitch: Callable[[], None],
        on_link_spotify: Callable[[], None],
        on_save_twitch: _Save | None = None,
        on_save_spotify: _Save | None = None,
        on_unlink_twitch: Callable[[], None] | None = None,
        on_unlink_spotify: Callable[[], None] | None = None,
        commands: CommandFile | None = None,
        requests: RequestBoard | None = None,
        widget: WidgetSite | None = None,
        widget_cache: Path | None = None,
    ) -> None:
        self._status = status
        self._actions = {"Twitch": on_link_twitch, "Spotify": on_link_spotify}
        self._unlink_actions = {"Twitch": on_unlink_twitch, "Spotify": on_unlink_spotify}
        if sys.platform == "win32":
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TCA.TwitchChatAssistant")
        self.root = tk.Tk()
        # Stay invisible until the cards have their final size. Showing earlier
        # paints every resize as the rounded panels measure themselves.
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.root.withdraw()
        self.root.title("TCA")
        self.root.configure(bg=_BG)
        self._apply_icon()
        self.root.resizable(True, True)
        self.root.minsize(760, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self._styles()

        shell = tk.Frame(self.root, bg=_BG, padx=18, pady=16)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)

        header = tk.Frame(shell, bg=_BG)
        header.grid(row=0, column=0, sticky="ew")
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
        tk.Frame(shell, bg=_PURPLE, height=2, highlightthickness=0).grid(row=1, column=0, sticky="ew", pady=(12, 12))

        body = tk.Frame(shell, bg=_BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=618, minsize=_LIVE_MIN)
        body.columnconfigure(1, weight=382, minsize=_SETUP_MIN)
        body.rowconfigure(0, weight=1)
        live = tk.Frame(body, bg=_BG)
        live.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        setup = tk.Frame(body, bg=_BG)
        setup.grid(row=0, column=1, sticky="nsew")

        self._rows: dict[str, dict[str, tk.Widget]] = {}
        accounts_card = _Card(setup)
        accounts_card.pack(fill="x", pady=(0, 8))
        accounts = accounts_card.body
        tk.Label(accounts, text="Status", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", pady=(0, 8))
        for name in ("Program", "Twitch", "Spotify"):
            self._rows[name] = self._build_row(accounts, name, bg=_CARD)

        self._footer = tk.Label(
            shell,
            text="Leave this window running in the background to maintain TCA Bot functions and commands.",
            bg=_BG,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=1100,
        )
        self._connections = _Disclosure(setup, self.root, "Connections")
        self._connections.frame.pack(fill="x", pady=(0, 8))
        connection_tail = tk.Frame(self._connections.body, bg=_CARD)
        self._twitch_form = _CredentialForm(
            self._connections.body,
            self.root,
            title="Twitch connection",
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
            wraplength=_SETUP_WRAP,
        )
        self._spotify_form = _CredentialForm(
            self._connections.body,
            self.root,
            title="Spotify connection",
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
            wraplength=_SETUP_WRAP,
        )
        self._requests = (
            _RequestPanel(live, requests, on_changed=self._sync, root=self.root, wraplength=_LIVE_WRAP)
            if requests is not None
            else None
        )
        self._widget = (
            _NowPlayingPanel(live, self.root, widget, widget_cache or Path("."), wraplength=_LIVE_WRAP)
            if widget is not None
            else None
        )
        self._twitch_form.anchor(connection_tail)
        self._spotify_form.anchor(connection_tail)
        connection_tail.pack(fill="x")
        self._commands = (
            _CommandPanel(setup, commands, on_changed=self._sync, wraplength=_SETUP_WRAP, expand=True)
            if commands is not None
            else None
        )
        live.columnconfigure(0, weight=1)
        live.rowconfigure(1, weight=1)
        if self._widget is not None:
            self._widget.frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        if self._requests is not None:
            self._requests.frame.grid(row=1, column=0, sticky="nsew")
        # Commands is placed first so a shorter window keeps that section intact.
        accounts_card.pack_forget()
        self._connections.frame.pack_forget()
        if self._commands is not None:
            self._commands.frame.pack(side="bottom", fill="both", expand=True)
            self._connections.cover(self._commands.frame)
        accounts_card.pack(side="top", fill="x", pady=(0, 8))
        self._connections.frame.pack(side="top", fill="x", pady=(0, 8))
        self._footer.grid(row=3, column=0, sticky="ew", pady=(14, 0))

        self._placed = False
        self._size: tuple[int, int] | None = None
        self._offscreen = _offscreen_origin()
        self.root.geometry(f"1100x680{self._offscreen}")
        self._map_hidden()
        if self._widget is not None:
            self._widget.attach()

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
        self._reveal()
        self._bring_forward()
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

    def _build_row(self, parent: tk.Frame, name: str, *, bg: str = _BG) -> dict[str, tk.Widget]:
        frame = tk.Frame(parent, bg=bg)
        frame.pack(fill="x", pady=(0, 6))
        frame.columnconfigure(1, weight=1)

        dot = tk.Canvas(frame, width=12, height=12, bg=bg, highlightthickness=0, bd=0)
        dot.create_oval(1, 1, 11, 11, fill=_TONES["wait"], outline="", tags="mark")
        dot.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky="nw", pady=(4, 0))

        tk.Label(frame, text=name, bg=bg, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        actions = tk.Frame(frame, bg=bg)
        actions.grid(row=0, column=2, sticky="e")
        button = _RoundButton(actions, text="Link", command=self._actions.get(name))
        button.pack(side="left")
        button.pack_forget()
        unlink = _RoundButton(actions, text="Unlink", command=self._unlink_actions.get(name))
        unlink.pack(side="left", padx=(6, 0))
        unlink.pack_forget()

        value = tk.Label(frame, text="…", bg=bg, fg=_TONES["wait"], font=("Segoe UI", 10), anchor="w")
        value.grid(row=1, column=1, columnspan=2, sticky="w")

        detail = tk.Label(
            frame,
            text="",
            bg=bg,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=_SETUP_WRAP,
        )
        detail.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(2, 0))
        detail.grid_remove()
        return {"dot": dot, "value": value, "button": button, "unlink": unlink, "detail": detail}

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
        self._twitch_form.present(setup=snapshot.twitch_state == "needs_setup")
        self._spotify_form.present(setup=snapshot.spotify_state == "needs_setup")
        if self._requests is not None:
            self._requests.sync()
        if self._commands is not None:
            self._commands.sync()
        self._fit()

    def _fit(self) -> None:
        # The first show uses a golden rectangle. Later status updates must not
        # pull the window back, or a resize the user just made would not stick.
        if self._placed:
            return
        self._settle_layout()
        needed_w = max(self.root.winfo_reqwidth(), 1)
        needed_h = max(self.root.winfo_reqheight(), 1)
        screen_w = max(self.root.winfo_screenwidth() - 48, 1)
        screen_h = max(self.root.winfo_screenheight() - 96, 1)
        width, height = golden_window(needed_w, needed_h, screen_w, screen_h)
        self._size = (width, height)
        self._placed = True
        # Keep the measured window off-screen. _reveal moves it into place.
        self.root.geometry(f"{width}x{height}{self._offscreen}")
        self._settle_layout()
        grown_w = max(self.root.winfo_reqwidth(), 1)
        grown_h = max(self.root.winfo_reqheight(), 1)
        if grown_w > width or grown_h > height:
            width, height = golden_window(grown_w, grown_h, screen_w, screen_h)
            self._size = (width, height)
            self.root.geometry(f"{width}x{height}{self._offscreen}")
            self._settle_layout()

    def _settle_layout(self) -> None:
        """Let the rounded cards finish measuring before anything is painted."""
        previous: tuple[object, ...] | None = None
        for _ in range(8):
            self.root.update()
            signature = self._shape_signature()
            if signature == previous:
                return
            previous = signature

    def _shape_signature(self) -> tuple[object, ...]:
        found: list[object] = []

        def walk(widget: tk.Misc) -> None:
            canvas = getattr(widget, "_canvas", None)
            if isinstance(canvas, tk.Canvas):
                found.append((int(canvas.cget("height")), widget.winfo_width(), widget.winfo_reqheight()))
            for child in widget.winfo_children():
                walk(child)

        walk(self.root)
        found.append(
            (
                self.root.winfo_width(),
                self.root.winfo_height(),
                self.root.winfo_reqwidth(),
                self.root.winfo_reqheight(),
            )
        )
        return tuple(found)

    def _map_hidden(self) -> None:
        """Map the window so Tk can measure it without bringing it forward."""
        self.root.update_idletasks()
        if sys.platform != "win32":
            self.root.deiconify()
            return
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        child = int(self.root.winfo_id())
        parent = user32.GetParent(child) or child
        user32.ShowWindow(parent, 4)

    def _reveal(self) -> None:
        """Move the finished window into place and show it in one step."""
        if not self._placed:
            self._fit()
        width, height = self._size or (max(self.root.winfo_width(), 1), max(self.root.winfo_height(), 1))
        self.root.geometry(f"{width}x{height}+120+120")
        self._settle_layout()
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        self.root.deiconify()

    def _bring_forward(self) -> None:
        self.root.lift()
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        child = int(self.root.winfo_id())
        user32.SetForegroundWindow(user32.GetParent(child) or child)

    def _apply(self, rows: list[StatusRow]) -> None:
        for row in rows:
            widgets = self._rows[row.name]
            color = _TONES[row.tone]
            value: tk.Label = widgets["value"]  # type: ignore[assignment]
            dot: tk.Canvas = widgets["dot"]  # type: ignore[assignment]
            detail: tk.Label = widgets["detail"]  # type: ignore[assignment]
            button: ttk.Button = widgets["button"]  # type: ignore[assignment]
            unlink: ttk.Button = widgets["unlink"]  # type: ignore[assignment]

            if value.cget("text") != row.value or value.cget("fg") != color:
                value.configure(text=row.value, fg=color)
            if str(dot.itemcget("mark", "fill")) != color:
                dot.itemconfigure("mark", fill=color)

            if row.detail:
                detail.configure(text=row.detail)
                if not detail.winfo_ismapped():
                    detail.grid()
            elif detail.winfo_ismapped():
                detail.grid_remove()

            _show_row_button(button, row.button, row.button_enabled, before=unlink)
            _show_row_button(unlink, row.unlink, row.unlink_enabled)

    def _dark_title_bar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            # Windows 11 rounds the window. Older Windows ignores this attribute.
            corners = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corners), ctypes.sizeof(corners))
        except (AttributeError, OSError):
            return


class _Disclosure:
    """A heading that opens its body over the section underneath."""

    def __init__(self, parent: tk.Misc, root: tk.Misc, title: str) -> None:
        self.frame = _Card(parent)
        self._open = False
        self._cover: tk.Widget | None = None
        header = tk.Frame(self.frame.body, bg=_CARD, cursor="hand2")
        header.pack(fill="x")
        self._mark = tk.Label(
            header,
            text="\uE76C",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe MDL2 Assets", 11),
            cursor="hand2",
        )
        self._mark.pack(side="right")
        label = tk.Label(
            header,
            text=title,
            bg=_CARD,
            fg=_TEXT,
            font=("Segoe UI", 11, "bold"),
            anchor="w",
            cursor="hand2",
        )
        label.pack(side="left", fill="x", expand=True)
        self.panel = tk.Frame(parent, bg=_CARD, highlightthickness=0, bd=0)
        sheet = _Card(self.panel, expand=True)
        sheet.pack(fill="both", expand=True)
        scroll = _BoundedScroll(sheet.body, root, bg=_CARD)
        scroll.frame.pack(fill="both", expand=True)
        self.body = scroll.body
        for widget in (header, self._mark, label):
            widget.bind("<Button-1>", self._toggle)

    def cover(self, target: tk.Widget) -> None:
        """Open across this widget instead of pushing it down."""
        self._cover = target
        target.bind("<Configure>", lambda _event: self._follow(), add="+")

    def _toggle(self, _event: object = None) -> str:
        self.open(not self._open)
        return "break"

    def open(self, shown: bool) -> None:
        self._open = shown
        self._mark.configure(text="\uE70D" if shown else "\uE76C")
        if shown:
            self._place_over()
            return
        self.panel.place_forget()

    def _follow(self) -> None:
        if self._open:
            self._place_over()

    def _place_over(self) -> None:
        parent = self.frame.master
        self.frame.update_idletasks()
        cover = self._cover
        if cover is not None and cover.winfo_manager():
            x, y = cover.winfo_x(), cover.winfo_y()
            width, height = cover.winfo_width(), cover.winfo_height()
        else:
            x = self.frame.winfo_x()
            y = self.frame.winfo_y() + self.frame.winfo_height()
            width = parent.winfo_width() - x
            height = parent.winfo_height() - y
        if width < 40 or height < 40:
            return
        self.panel.place(in_=parent, x=x, y=y, width=width, height=height)
        self.panel.lift()


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
        wraplength: int = _SETUP_WRAP,
    ) -> None:
        self._root = root
        self._redirect = redirect
        self._open_url = open_url
        self._on_save = on_save
        self._on_saved = on_saved
        self._before: tk.Widget | None = None
        self._setup_shown = True
        self._copy_after: dict[_RoundButton, str] = {}
        self.frame = _Card(parent)
        host = self.frame.body
        tk.Label(host, text=title, bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        self._body = tk.Label(
            host,
            text=body,
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )
        self._body.pack(fill="x", pady=(4, 8))

        self._redirect_row = tk.Frame(host, bg=_CARD)
        self._redirect_row.pack(fill="x")
        self._copy_button = _RoundButton(self._redirect_row, text="Copy", command=self._copy_redirect)
        self._copy_button.pack(side="right", padx=(8, 0))
        redirect_bar = _EntryBar(self._redirect_row, readonly=True)
        redirect_bar.pack(side="left", fill="x", expand=True)
        redirect_bar.entry.configure(font=("Segoe UI", 9), width=1)
        redirect_bar.entry.configure(state="normal")
        redirect_bar.entry.insert(0, redirect)
        redirect_bar.entry.configure(state="readonly")

        self._open = _RoundButton(host, text=open_label, command=self._open)
        self._open.pack(anchor="w", pady=(8, 4))
        self._client_id = self._secret_field("Client ID")
        self._secret = self._secret_field("Client secret")
        _RoundButton(host, text=save_label, command=self._save, size="regular").pack(anchor="w", pady=(8, 0))
        self._note = tk.Label(
            host,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )

    def anchor(self, before: tk.Widget) -> None:
        self._before = before

    def prefill(self, client_id: str, secret: str) -> None:
        if client_id and not self._client_id.get():
            self._client_id.insert(0, client_id)
        if secret and not self._secret.get():
            self._secret.insert(0, secret)

    def present(self, *, setup: bool) -> None:
        """Keep the client ID and secret bars on screen. Setup instructions show until the app is saved."""
        if not self.frame.winfo_manager() and self._before is not None:
            self.frame.pack(fill="x", pady=(4, 8), before=self._before)
        if setup == self._setup_shown:
            return
        self._setup_shown = setup
        if setup:
            if not self._open.winfo_manager():
                self._open.pack(anchor="w", pady=(8, 4), before=self._id_row)
            if not self._redirect_row.winfo_manager():
                self._redirect_row.pack(fill="x", before=self._open)
            if not self._body.winfo_manager():
                self._body.pack(fill="x", pady=(4, 8), before=self._redirect_row)
            return
        for widget in (self._body, self._redirect_row, self._open):
            if widget.winfo_manager():
                widget.pack_forget()

    def _secret_field(self, label: str) -> tk.Entry:
        row = tk.Frame(self.frame.body, bg=_CARD)
        row.pack(fill="x", pady=(4, 0))
        if not hasattr(self, "_id_row"):
            self._id_row = row
        tk.Label(row, text=label, bg=_CARD, fg=_TEXT, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
        copy = _RoundButton(row, text="Copy", command=lambda: self._copy_text(entry.get(), row))
        copy.pack(side="right", padx=(8, 0))
        bar = _EntryBar(row, show="*")
        bar.pack(side="left", fill="x", expand=True)
        entry = bar.entry
        entry.configure(width=1)
        eye = tk.Canvas(bar.body, width=28, height=22, bg=_FIELD, highlightthickness=0, cursor="hand2")
        eye.pack(side="right", padx=(0, 2))
        revealed = False

        def paint() -> None:
            _paint_eye(eye, revealed=revealed)

        def toggle(_event: object) -> None:
            nonlocal revealed
            revealed = not revealed
            entry.configure(show="" if revealed else "*")
            paint()

        eye.bind("<Button-1>", toggle)
        entry.bind("<FocusIn>", lambda _event: bar.set_outline(_PURPLE), add="+")
        entry.bind("<FocusOut>", lambda _event: bar.set_outline(_LINE), add="+")
        paint()
        return entry

    def _copy_redirect(self) -> None:
        self._copy_text(self._redirect, self._redirect_row)

    def _copy_text(self, text: str, owner: tk.Widget) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        button = next(
            (child for child in owner.winfo_children() if isinstance(child, _RoundButton) and child.cget("text") in {"Copy", "Copied"}),
            None,
        )
        if button is None:
            return
        button.configure(text="Copied")
        previous = self._copy_after.pop(button, None)
        if previous is not None:
            self._root.after_cancel(previous)

        def reset(target: _RoundButton = button) -> None:
            self._copy_after.pop(target, None)
            try:
                target.configure(text="Copy")
            except tk.TclError:
                return

        self._copy_after[button] = self._root.after(2000, reset)

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


def _show_row_button(button: ttk.Button, label: str | None, enabled: bool, *, before: ttk.Button | None = None) -> None:
    if label:
        state = tk.NORMAL if enabled else tk.DISABLED
        # Configuring a button on every refresh cancels a click that is already in progress.
        if button.cget("text") != label or str(button.cget("state")) != state:
            button.configure(text=label, state=state)
        if not button.winfo_ismapped():
            options: dict[str, object] = {"side": "left", "padx": (6, 0)}
            if before is not None and before.winfo_ismapped():
                options["before"] = before
            button.pack(**options)
    elif button.winfo_ismapped():
        button.pack_forget()


def _paint_eye(canvas: tk.Canvas, *, revealed: bool) -> None:
    """Open eye while the secret is visible, and a crossed eye while it is hidden.

    These are the Windows icon-font glyphs, drawn in the theme grey. A canvas
    stroke at this size turns into a stair-step.
    """
    canvas.delete("all")
    canvas.create_text(
        int(canvas.cget("width")) / 2,
        int(canvas.cget("height")) / 2,
        text="\uE7B3" if revealed else "\uED1A",
        fill=_MUTED,
        font=("Segoe MDL2 Assets", 14),
        anchor="center",
    )


def _blend(start: str, end: str, amount: float) -> str:
    def channel(first: str, second: str) -> int:
        return round(int(first, 16) + (int(second, 16) - int(first, 16)) * amount)

    return f"#{channel(start[1:3], end[1:3]):02x}{channel(start[3:5], end[3:5]):02x}{channel(start[5:7], end[5:7]):02x}"


class _NowPlayingPanel:
    """The song card, plus the address OBS uses for a browser source."""

    def __init__(self, parent: tk.Frame, root: tk.Tk, site: WidgetSite, cache: Path, *, wraplength: int = _LIVE_WRAP) -> None:
        self._root = root
        self._site = site
        self._cache = cache
        self._copy_after: str | None = None
        self.frame = _Card(parent)
        host = self.frame.body
        tk.Label(host, text="Now playing", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            fill="x"
        )
        self._note = tk.Label(
            host,
            text="",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )
        # A container frame has its own window, so the preview cannot cover the rest of TCA.
        if site.error:
            self._holder = None
            self._note.configure(text=site.error)
            self._note.pack(fill="x", pady=(8, 8))
        else:
            self._holder = tk.Frame(host, height=200, bg=_CARD, highlightthickness=0, container=True)
            self._holder.pack(fill="x", pady=(8, 8))
            self._holder.pack_propagate(False)

        address = tk.Frame(host, bg=_CARD)
        self._address = address
        address.pack(fill="x")
        self._copy_button = _RoundButton(address, text="Copy", command=self._copy)
        self._copy_button.pack(side="right", padx=(8, 0))
        tk.Label(address, text="Browser Source Link", bg=_CARD, fg=_MUTED, font=("Segoe UI", 9), anchor="w").pack(side="left")
        address_bar = _EntryBar(address, show="*")
        address_bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
        entry = address_bar.entry
        entry.configure(width=1)
        entry.insert(0, site.url)
        entry.configure(state="readonly", font=("Segoe UI", 9))
        self._address_entry = entry
        eye = tk.Canvas(address_bar.body, width=28, height=22, bg=_FIELD, highlightthickness=0, cursor="hand2")
        eye.pack(side="right", padx=(0, 2))
        revealed = False

        def paint() -> None:
            _paint_eye(eye, revealed=revealed)

        def toggle(_event: object) -> None:
            nonlocal revealed
            revealed = not revealed
            entry.configure(show="" if revealed else "*")
            paint()

        eye.bind("<Button-1>", toggle)
        paint()

    def attach(self) -> None:
        holder = self._holder
        if holder is None:
            return
        self.frame.update_idletasks()
        width = holder.winfo_width()
        if width < 40:
            width = max(self.frame.winfo_width() - self.frame._padx * 2, 1)
        try:
            from tca.preview import embed_preview

            embed_preview(holder, self._site.preview_url, self._cache, width=width, height=200)
        except Exception:
            logger.exception("Could not open the song widget preview")
            holder.pack_forget()
            self._note.configure(
                text="The preview needs the WebView2 runtime. Copy the address below into OBS."
            )
            self._note.pack(fill="x", pady=(8, 8), before=self._address)

    def _copy(self) -> None:
        self._root.clipboard_clear()
        self._root.clipboard_append(self._site.url)
        self._copy_button.configure(text="Copied")
        if self._copy_after is not None:
            self._root.after_cancel(self._copy_after)
        self._copy_after = self._root.after(2000, self._reset_copy)

    def _reset_copy(self) -> None:
        self._copy_after = None
        try:
            self._copy_button.configure(text="Copy")
        except tk.TclError:
            return


class _BoundedScroll:
    """A vertical list whose scroll range is the rows, and no empty space past them."""

    def __init__(
        self,
        parent: tk.Misc,
        root: tk.Misc,
        *,
        bg: str = _CARD,
        outer: _BoundedScroll | None = None,
    ) -> None:
        self._root = root
        self._outer = outer
        self._fitting = False
        self._fitted: tuple[int, int] | None = None
        self.frame = tk.Frame(parent, bg=bg, highlightthickness=0, bd=0)
        self.canvas = tk.Canvas(self.frame, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._bar = ttk.Scrollbar(
            self.frame,
            orient="vertical",
            style="Queue.Vertical.TScrollbar",
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.body = tk.Frame(self.canvas, bg=bg, highlightthickness=0, bd=0)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _event: self.fit())
        self.canvas.bind("<Configure>", lambda _event: self.fit())
        self.frame.bind("<Enter>", self._bind_wheel)
        self.frame.bind("<Leave>", self._unbind_wheel)
        _style_queue_scrollbar(root.winfo_toplevel())

    def set_view(self, height: int) -> None:
        """Show only this many pixels. A short list stays short, so it cannot scroll."""
        height = max(int(height), 1)
        self.frame.pack_propagate(False)
        if int(self.frame.cget("height")) != height:
            self.frame.configure(height=height)
        self.fit()

    def fit(self) -> None:
        if self._fitting:
            return
        width = int(self.canvas.winfo_width())
        if width < 2:
            return
        self.body.update_idletasks()
        content = max(self.body.winfo_reqheight(), 1)
        if self._fitted == (width, content) and content <= self.canvas.winfo_height() + 1:
            return
        self._fitting = True
        try:
            self.canvas.itemconfigure(self._window, width=width, height=content)
            self.canvas.configure(scrollregion=(0, 0, width, content))
            if content <= self.canvas.winfo_height() + 1:
                self.canvas.yview_moveto(0)
            self._fitted = (width, content)
        finally:
            self._fitting = False

    def _on_scroll(self, first: str, last: str) -> None:
        self._bar.set(first, last)
        needed = float(first) > 0.001 or float(last) < 0.999
        if needed and not self._bar.winfo_ismapped():
            self._bar.pack(side="right", fill="y")
        elif not needed and self._bar.winfo_ismapped():
            self._bar.pack_forget()

    def _bind_wheel(self, _event: object) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event: object) -> None:
        x, y = self._root.winfo_pointerxy()
        widget = self._root.winfo_containing(x, y)
        if widget is not None and str(widget).startswith(str(self.frame)):
            return
        self.canvas.unbind_all("<MouseWheel>")
        outer = self._outer
        if outer is not None and widget is not None and str(widget).startswith(str(outer.frame)):
            outer.canvas.bind_all("<MouseWheel>", outer._wheel)

    def _wheel(self, event: tk.Event) -> None:
        if self.body.winfo_reqheight() <= self.canvas.winfo_height() + 1:
            if self._outer is not None:
                self._outer._wheel(event)
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class _RequestPanel:
    """Waiting song requests, each on a card washed with its album colors."""

    _CARD_HEIGHT = 96
    _ART_SIZE = 72

    def __init__(
        self,
        parent: tk.Frame,
        requests: RequestBoard,
        on_changed: Callable[[], None],
        root: tk.Tk,
        *,
        wraplength: int = _LIVE_WRAP,
    ) -> None:
        self._requests = requests
        self._on_changed = on_changed
        self._root = root
        self._shown: tuple[tuple[int, str, str, str, str, str], ...] | None = None
        self._art_bytes: dict[str, bytes] = {}
        self._art_ready: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._loading: set[str] = set()
        self._photos: dict[tuple[str, int, str], tk.PhotoImage] = {}
        self._cards: list[tk.Canvas] = []
        self._width = 0
        self._title_font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        self._artist_font = tkfont.Font(family="Segoe UI", size=10)
        self._by_font = tkfont.Font(family="Segoe UI", size=9)
        self._button_font_size = _BUTTON_FONT
        self.frame = _Card(parent, expand=True)
        self._sizing = False
        host = self.frame.body
        self._intro = tk.Frame(host, bg=_CARD)
        self._intro.pack(fill="x")
        tk.Label(self._intro, text="Song requests", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            fill="x"
        )
        tk.Label(
            self._intro,
            text="Viewers' songs still waiting. Remove drops one from this list and from !queue.",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        ).pack(fill="x", pady=(4, 8))
        self._rows = _BoundedScroll(host, root)
        self._rows.frame.pack(fill="x", anchor="nw")
        self._scroll = self._rows.canvas
        self._list = self._rows.body
        self._scroll.bind("<Configure>", self._fit_width, add="+")
        self.frame.bind("<Configure>", self._fit_rows, add="+")
        host.bind("<Configure>", self._fit_rows)
        self._note = tk.Label(
            host,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )
        self._note.bind("<Map>", self._fit_rows)
        self._note.bind("<Unmap>", self._fit_rows)
        self._fading = _FadingNote(self._note, on_changed)

    def sync(self) -> None:
        self._take_art()
        shown = self._requests.snapshot()
        if shown == self._shown:
            return
        self._shown = shown
        self._rebuild()

    def _fit_rows(self, _event: object = None) -> None:
        """Keep the list as tall as its cards, and no taller than the room under the heading."""
        if self._sizing:
            return
        self._sizing = True
        try:
            self.frame.update_idletasks()
            body_h = self.frame.body.winfo_height()
            content = max(self._list.winfo_reqheight(), 1)
            if body_h < 40:
                self._rows.set_view(content)
                return
            note_h = self._note.winfo_height() + 6 if self._note.winfo_ismapped() else 0
            available = body_h - self._intro.winfo_height() - note_h
            self._rows.set_view(min(content, max(available, 1)))
        finally:
            self._sizing = False

    def _fit_width(self, event: tk.Event) -> None:
        width = int(event.width)
        if abs(width - self._width) < 4:
            return
        self._width = width
        self._rebuild()

    def _rebuild(self) -> None:
        for child in self._list.winfo_children():
            child.destroy()
        self._cards = []
        shown = self._shown or ()
        if not shown:
            tk.Label(
                self._list,
                text="No song requests are waiting.",
                bg=_CARD,
                fg=_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(fill="x")
            self._fit_rows()
            return
        width = max(self._width, self._scroll.winfo_width(), 280)
        for index, _track_id, title, artist, viewer, image_url in shown:
            card = tk.Canvas(
                self._list,
                width=width,
                height=self._CARD_HEIGHT,
                bg=_CARD,
                highlightthickness=0,
                bd=0,
            )
            card.pack(fill="x", pady=(0, 8))
            card.index = index  # type: ignore[attr-defined]
            card.art_url = image_url  # type: ignore[attr-defined]
            card.details = (title, artist, viewer)  # type: ignore[attr-defined]
            card.hover = False  # type: ignore[attr-defined]
            card.bind("<Motion>", lambda event, target=card: self._hover(target, event))
            card.bind("<Leave>", lambda _event, target=card: self._hover(target, None))
            card.bind("<Button-1>", lambda event, target=card: self._click(target, event))
            self._cards.append(card)
            self._paint(card)
            if image_url:
                self._want_art(image_url)
        self._fit_rows()

    def _paint(self, card: tk.Canvas) -> None:
        width = max(int(card.cget("width")), 280)
        title, artist, viewer = card.details  # type: ignore[attr-defined]
        url = str(card.art_url)  # type: ignore[attr-defined]
        wash, thumb = self._photos_for(url, width)
        card.delete("all")
        card.create_image(0, 0, image=wash, anchor="nw")
        card.wash = wash  # type: ignore[attr-defined]
        if thumb is not None:
            card.create_image(12, 12, image=thumb, anchor="nw")
            card.thumb = thumb  # type: ignore[attr-defined]
        button = self._remove_box(width)
        text_width = max(button[0] - 108, 40)
        card.create_text(
            96,
            24,
            text=_fit_text(title, self._title_font, text_width),
            anchor="w",
            fill="#f4f4f5",
            font=self._title_font,
        )
        card.create_text(
            96,
            48,
            text=_fit_text(artist, self._artist_font, text_width),
            anchor="w",
            fill="#d4d4dc",
            font=self._artist_font,
        )
        card.create_text(
            96,
            70,
            text=_fit_text(f"Requested by {viewer}", self._by_font, text_width),
            anchor="w",
            fill="#c8c8d0",
            font=self._by_font,
        )
        self._draw_remove(card, button, bool(card.hover))  # type: ignore[attr-defined]
        card.remove_box = button  # type: ignore[attr-defined]

    def _photos_for(self, url: str, width: int) -> tuple[tk.PhotoImage, tk.PhotoImage | None]:
        data = self._art_bytes.get(url) if url else None
        key = (url if data else "", width)
        wash = self._photos.get((*key, "wash"))
        thumb = self._photos.get((*key, "thumb"))
        if wash is not None and (data is None or thumb is not None):
            return wash, thumb
        wash_png, thumb_png = card_images(data, width=width, height=self._CARD_HEIGHT, art_size=self._ART_SIZE)
        wash = tk.PhotoImage(data=base64.b64encode(wash_png).decode("ascii"))
        self._photos[(*key, "wash")] = wash
        if thumb_png is None:
            return wash, None
        thumb = tk.PhotoImage(data=base64.b64encode(thumb_png).decode("ascii"))
        self._photos[(*key, "thumb")] = thumb
        return wash, thumb

    def _want_art(self, url: str) -> None:
        if url in self._art_bytes or url in self._loading:
            return
        self._loading.add(url)

        def work() -> None:
            try:
                data = fetch_album_art(url)
            except Exception:
                logger.exception("Could not load album art.")
                data = None
            self._art_ready.put((url, data))

        threading.Thread(target=work, name="tca-request-art", daemon=True).start()

    def _take_art(self) -> None:
        """Apply pictures fetched off the UI thread. Tk cannot be touched from that thread."""
        try:
            while True:
                url, data = self._art_ready.get_nowait()
                self._loading.discard(url)
                if not data:
                    continue
                self._art_bytes[url] = data
                for card in list(self._cards):
                    try:
                        exists = bool(card.winfo_exists())
                    except tk.TclError:
                        continue
                    if exists and getattr(card, "art_url", "") == url:
                        self._paint(card)
        except queue.Empty:
            return

    def _remove_box(self, width: int) -> tuple[int, int, int, int]:
        button_w = _label_width("Remove", self._button_font_size) + 44
        button_h = 36
        x2 = width - 14
        x1 = x2 - button_w
        y1 = (self._CARD_HEIGHT - button_h) / 2
        return int(x1), int(y1), int(x2), int(y1 + button_h)

    def _draw_remove(self, card: tk.Canvas, box: tuple[int, int, int, int], hovered: bool) -> None:
        x1, y1, x2, y2 = box
        _pill_button(
            card,
            x1,
            y1,
            x2,
            y2,
            "Remove",
            hover=hovered,
            font_size=self._button_font_size,
            radius=_BUTTON_RADIUS,
            tags="remove",
        )

    def _hover(self, card: tk.Canvas, event: tk.Event | None) -> None:
        box = getattr(card, "remove_box", None)
        inside = event is not None and box is not None and _inside(box, event.x, event.y)
        if inside == bool(getattr(card, "hover", False)):
            return
        card.hover = inside  # type: ignore[attr-defined]
        card.delete("remove")
        if box is not None:
            self._draw_remove(card, box, inside)
        card.configure(cursor="hand2" if inside else "")

    def _click(self, card: tk.Canvas, event: tk.Event) -> None:
        box = getattr(card, "remove_box", None)
        if box is None or not _inside(box, event.x, event.y):
            return
        self._remove(int(card.index))  # type: ignore[attr-defined]

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


def _style_queue_scrollbar(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.configure(
        "Queue.Vertical.TScrollbar",
        background="#2a2a2e",
        troughcolor=_CARD,
        bordercolor=_CARD,
        arrowcolor=_MUTED,
        lightcolor="#2a2a2e",
        darkcolor="#2a2a2e",
        gripcount=0,
    )
    style.map("Queue.Vertical.TScrollbar", background=[("active", "#3a3a3d")])


def _inside(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    x1, y1, x2, y2 = box
    return x1 <= x <= x2 and y1 <= y <= y2


def _fit_text(text: str, font: tkfont.Font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    trimmed = text
    while trimmed and font.measure(trimmed + "…") > width:
        trimmed = trimmed[:-1]
    return (trimmed.rstrip() or text[:1]) + "…"


class _CommandPanel:
    """Add and remove text commands. Built-in Spotify commands stay."""

    def __init__(
        self,
        parent: tk.Frame,
        commands: CommandFile,
        on_changed: Callable[[], None],
        *,
        wraplength: int = _SETUP_WRAP,
        outer: _BoundedScroll | None = None,
        expand: bool = False,
    ) -> None:
        self._commands = commands
        self._on_changed = on_changed
        self._shown: tuple[tuple[str, str], ...] | None = None
        self._handlers: tuple[str, ...] | None = None
        self._sizing = False
        self.frame = _Card(parent, expand=expand)
        self._host = self.frame.body
        self._intro = tk.Frame(self._host, bg=_CARD)
        self._intro.pack(fill="x")
        tk.Label(self._intro, text="Commands", bg=_CARD, fg=_TEXT, font=("Segoe UI", 11, "bold"), anchor="w").pack(
            fill="x"
        )
        tk.Label(
            self._intro,
            text="Add a name such as !discord and the reply chat should see.",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        ).pack(fill="x", pady=(4, 8))
        self._built_in = tk.Label(
            self._intro,
            text="",
            bg=_CARD,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )
        self._built_in.pack(fill="x", pady=(0, 6))
        self._form = tk.Frame(self._host, bg=_CARD)
        self._form.pack(side="bottom", fill="x", pady=(8, 0))
        self._rows = _BoundedScroll(self._host, parent.winfo_toplevel(), outer=outer)
        self._rows.frame.pack(fill="both", expand=True)
        self._list = self._rows.body
        self._name = self._field("Name")
        self._reply = self._field("Reply")
        self._name.bind("<Return>", self._add_event)
        self._reply.bind("<Return>", self._add_event)
        self._add = _RoundButton(self._form, text="Add command", command=self._add, size="regular")
        self._add.pack(anchor="w", pady=(8, 0))
        self._note = tk.Label(
            self._form,
            text="",
            bg=_CARD,
            fg=_TONES["bad"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=wraplength,
        )
        self._fading = _FadingNote(self._note, on_changed)
        if expand:
            self.frame.update_idletasks()
            needed = self._intro.winfo_reqheight() + self._form.winfo_reqheight() + self.frame._pady * 2 + 16
            self.frame._canvas.configure(height=max(needed, 48))
        self.frame.bind("<Configure>", self._fit_rows, add="+")
        self._host.bind("<Configure>", self._fit_rows)
        self._form.bind("<Configure>", self._fit_rows)

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
        else:
            self._fill_commands(text)
        self._fit_rows()

    def _fill_commands(self, text: tuple[tuple[str, str], ...]) -> None:
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
            _RoundButton(
                row,
                text="Remove",
                command=lambda command=name: self._remove(command),
            ).pack(side="right")

    def _fit_rows(self, _event: object = None) -> None:
        """Scroll the command rows inside this card. The add form stays at the bottom."""
        if self._sizing:
            return
        self._sizing = True
        try:
            self._list.update_idletasks()
            content = max(self._list.winfo_reqheight(), 1)
            if not self.frame._expand:
                self._rows.set_view(min(content, _COMMAND_LIST_MAX))
                return
            body_h = self._host.winfo_height()
            if body_h < 40:
                self._rows.set_view(min(content, _COMMAND_LIST_MAX))
                return
            room = body_h - self._intro.winfo_height() - self._form.winfo_reqheight() - 8
            self._rows.set_view(min(content, max(room, 1)))
        finally:
            self._sizing = False

    def _field(self, label: str) -> tk.Entry:
        row = tk.Frame(self._form, bg=_CARD)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=label, bg=_CARD, fg=_TEXT, font=("Segoe UI", 9), width=14, anchor="w").pack(side="left")
        bar = _EntryBar(row)
        bar.pack(side="left", fill="x", expand=True)
        return bar.entry

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
