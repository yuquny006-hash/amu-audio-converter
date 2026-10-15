import json
import plistlib
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1]).resolve()
arch = sys.argv[2]
output = Path(sys.argv[3])
plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
exe = app / "Contents/MacOS/AmuMusicAudioConverter"
file_text = subprocess.check_output(["file", str(exe)], text=True).strip()
assert arch in file_text, file_text
assert plist["CFBundleIdentifier"] == "com.amumusic.audio-converter"
assert plist["LSMinimumSystemVersion"] == "14.0"
required = [
    "Contents/Resources/web/index.html",
]
for relative in required:
    assert (app / relative).is_file(), relative
for tool in ("ffmpeg", "ffprobe"):
    matches = list((app / "Contents").rglob(tool))
    assert matches, tool
record = {
    "architecture": arch,
    "minimum_macos": "14.0",
    "bundle_identifier": plist["CFBundleIdentifier"],
    "edition": "points",
    "codesign": "ad-hoc verified",
    "executable": file_text,
}
output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(record, ensure_ascii=False))
