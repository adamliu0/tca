# -*- mode: python ; coding: utf-8 -*-
"""Build dist/TCA.exe. Account files are not bundled; they are saved beside the exe."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
twitch_datas, twitch_binaries, twitch_hidden = collect_all("twitchio")
spotify_datas, spotify_binaries, spotify_hidden = collect_all("spotipy")
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
net_datas, net_binaries, net_hidden = collect_all("pythonnet")
clr_datas, clr_binaries, clr_hidden = collect_all("clr_loader")
widget_datas, widget_binaries, widget_hidden = collect_all("tkwebview2")

a = Analysis(
    [str(root / "tca" / "__main__.py")],
    pathex=[str(root)],
    binaries=twitch_binaries + spotify_binaries + webview_binaries + net_binaries + clr_binaries + widget_binaries,
    datas=[
        (str(root / "assets"), "assets"),
        *twitch_datas,
        *spotify_datas,
        *webview_datas,
        *net_datas,
        *clr_datas,
        *widget_datas,
    ],
    hiddenimports=[
        "tca",
        "tca.now_playing",
        "tca.preview",
        "tca.media_session",
        "tca.artwork",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageEnhance",
        "PIL.ImageFilter",
        "winrt.windows.media.control",
        "winrt.windows.storage.streams",
        "tkwebview2",
        "tkwebview2.tkwebview2",
        *twitch_hidden,
        *spotify_hidden,
        *webview_hidden,
        *net_hidden,
        *clr_hidden,
        *widget_hidden,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TCA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "assets" / "tca.ico"),
)
