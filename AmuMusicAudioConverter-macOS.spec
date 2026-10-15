# -*- mode: python ; coding: utf-8 -*-
import os

root = os.path.abspath(SPECPATH)
ffmpeg = os.environ["AMU_FFMPEG"]
ffprobe = os.environ["AMU_FFPROBE"]

a = Analysis(
    [os.path.join(root, "main.py")],
    pathex=[root],
    binaries=[(ffmpeg, "."), (ffprobe, ".")],
    datas=[(os.path.join(root, "web"), "web"),
           (os.path.join(root, "assets"), "assets")],
    hiddenimports=[
        "keyring", "keyring.backends", "keyring.backends.macOS",
        "webview.platforms.cocoa",
    ],
    excludes=["clr", "pythonnet", "webview.platforms.winforms",
              "webview.platforms.qt", "webview.platforms.gtk"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="AmuMusicAudioConverter", console=False,
    debug=False, strip=False, upx=False,
    argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="AmuMusicAudioConverter")
app = BUNDLE(
    coll,
    name="AmuMusic Audio Converter.app",
    icon=os.path.join(root, "macos", "AppIcon.icns"),
    bundle_identifier="com.amumusic.audio-converter",
    info_plist={
        "CFBundleName": "AmuMusic Audio Converter",
        "CFBundleDisplayName": "Amu Music 音频转换器",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "14.0",
    },
)
