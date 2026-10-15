#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) LABEL="AppleSilicon" ;;
  x86_64) LABEL="Intel" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 2 ;;
esac
export MACOSX_DEPLOYMENT_TARGET=14.0

uv python install 3.12
PY="$(uv python find --managed-python 3.12)"
uv pip install --python "$PY" -r requirements-macos.txt
uv pip check --python "$PY"
"$PY" -m unittest discover -s tests -v
"$PY" -c 'import edition; assert edition.EDITION == "points"'

HOMEBREW_NO_AUTO_UPDATE=1 brew install ffmpeg
export AMU_FFMPEG="$(command -v ffmpeg)"
export AMU_FFPROBE="$(command -v ffprobe)"
file "$AMU_FFMPEG" | grep -q "$ARCH"
file "$AMU_FFPROBE" | grep -q "$ARCH"

rm -rf build dist macos/AppIcon.iconset macos/AppIcon.icns
mkdir -p macos/AppIcon.iconset dist
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" assets/amu_logo.png \
    --out "macos/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  twice=$((size * 2))
  sips -z "$twice" "$twice" assets/amu_logo.png \
    --out "macos/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns macos/AppIcon.iconset -o macos/AppIcon.icns

"$PY" -m PyInstaller AmuMusicAudioConverter-macOS.spec --noconfirm --clean
APP="dist/AmuMusic Audio Converter.app"
test -x "$APP/Contents/MacOS/AmuMusicAudioConverter"
test -f "$APP/Contents/Resources/web/index.html"

find "$APP" -type f -print0 | while IFS= read -r -d '' item; do
  if file -b "$item" | grep -q 'Mach-O'; then
    codesign --force --sign - "$item"
  fi
done
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

"$PY" macos/smoke_bundle.py "$APP" "$ARCH" "dist/smoke-${ARCH}.json"
cp macos/使用说明.txt dist/使用说明.txt
uv pip freeze --python "$PY" > "dist/dependencies-${ARCH}.txt"
ZIP="dist/AmuMusic音频转换器-macOS-${LABEL}.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
(cd dist && shasum -a 256 "$(basename "$ZIP")" > "SHA256-${ARCH}.txt")
