# -*- mode: python ; coding: utf-8 -*-
"""Build dist/TCA.exe. Account files are not bundled; they are saved beside the exe."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
twitch_datas, twitch_binaries, twitch_hidden = collect_all("twitchio")
spotify_datas, spotify_binaries, spotify_hidden = collect_all("spotipy")

a = Analysis(
    [str(root / "tca" / "__main__.py")],
    pathex=[str(root)],
    binaries=twitch_binaries + spotify_binaries,
    datas=[(str(root / "assets"), "assets"), *twitch_datas, *spotify_datas],
    hiddenimports=["tca", *twitch_hidden, *spotify_hidden],
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
