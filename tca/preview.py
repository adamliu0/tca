"""Show the song widget page inside the status window."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import tkinter as tk

_STA = 2
_CHANGED_MODE = 0x80010106

_user32 = ctypes.windll.user32
_set_parent = _user32.SetParent
_set_parent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_set_parent.restype = ctypes.c_void_p
_get_parent = _user32.GetParent
_get_parent.argtypes = [ctypes.c_void_p]
_get_parent.restype = ctypes.c_void_p
_move_window = _user32.MoveWindow
_move_window.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_bool]
_move_window.restype = ctypes.c_bool


def embed_preview(holder: tk.Misc, url: str, cache_dir: Path, *, width: int, height: int) -> None:
    """Place the widget page in this frame. Raises when WebView2 cannot start."""
    _apartment()
    from tkwebview2.tkwebview2 import have_runtime

    if not have_runtime():
        raise RuntimeError("WebView2 runtime is not installed.")

    from webview.util import interop_dll_path
    import clr

    for platform in ("win-x64", "win-x86", "win-arm64"):
        native = interop_dll_path(platform)
        if os.path.isdir(native):
            os.environ["PATH"] = native + os.pathsep + os.environ.get("PATH", "")
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")

    from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
    from System import Uri
    from System.Drawing import Color
    from System.Windows.Forms import DockStyle, Panel

    cache_dir.mkdir(parents=True, exist_ok=True)
    panel = Panel()
    panel.Width = width
    panel.Height = height
    panel.BackColor = Color.FromArgb(255, 0x18, 0x18, 0x1B)
    web = WebView2()
    try:
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = str(cache_dir)
        web.CreationProperties = props
        web.Dock = DockStyle.Fill
        web.DefaultBackgroundColor = Color.FromArgb(255, 0x18, 0x18, 0x1B)
        panel.Controls.Add(web)
        panel.Visible = False

        def ready(sender, _args) -> None:
            try:
                core = sender.CoreWebView2
                settings = core.Settings
                settings.AreDevToolsEnabled = False
                settings.AreDefaultContextMenusEnabled = False
                settings.AreBrowserAcceleratorKeysEnabled = False
            except Exception:
                return

        web.CoreWebView2InitializationCompleted += ready
        hwnd = int(panel.Handle.ToInt64())
        if not _place(hwnd, holder, width, height):
            raise RuntimeError("Could not place the song preview in the window.")
        panel.Visible = True
        web.Source = Uri(url)
    except Exception:
        panel.Dispose()
        raise

    def resize(_event: object) -> None:
        width = max(holder.winfo_width(), 1)
        height = max(holder.winfo_height(), 1)
        if getattr(holder, "_widget_box", None) == (width, height):
            return
        holder._widget_box = (width, height)  # type: ignore[attr-defined]
        _move_window(hwnd, 0, 0, width, height, True)

    def close(_event: object) -> None:
        try:
            panel.Dispose()
        except Exception:
            return

    holder.bind("<Configure>", resize)
    holder.bind("<Destroy>", close, add="+")
    # Keep the control and the callback alive for as long as the frame is.
    holder._widget_panel = panel  # type: ignore[attr-defined]
    holder._widget_web = web  # type: ignore[attr-defined]
    holder._widget_ready = ready  # type: ignore[attr-defined]


def _place(hwnd: int, holder: tk.Misc, width: int, height: int) -> bool:
    """Parent the preview to this frame. A failed parent leaves a window floating over the app."""
    parent = _as_handle(holder.winfo_id())
    if _as_handle(_get_parent(hwnd)) != parent:
        _set_parent(hwnd, parent)
        if _as_handle(_get_parent(hwnd)) != parent:
            return False
    width = max(width, 1)
    height = max(height, 1)
    holder._widget_box = (width, height)  # type: ignore[attr-defined]
    return bool(_move_window(hwnd, 0, 0, width, height, True))


def _as_handle(value: object) -> int:
    if not value:
        return 0
    return int(value) & 0xFFFFFFFFFFFFFFFF


def _apartment() -> None:
    result = ctypes.windll.ole32.CoInitializeEx(None, _STA) & 0xFFFFFFFF
    if result not in (0, 1):
        if result == _CHANGED_MODE:
            raise RuntimeError("WebView2 could not use this window.")
        raise OSError(f"WebView2 could not start ({result}).")
