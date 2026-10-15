# -*- coding: utf-8 -*-
"""Small cross-platform helpers shared by the desktop shell and converter."""
import os
import subprocess
import sys


def subprocess_options(platform=None):
    """Return Windows-only flags without leaking them onto POSIX systems."""
    platform = platform or sys.platform
    return {"creationflags": 0x08000000} if platform == "win32" else {}


def open_in_file_manager(path, reveal=False, platform=None):
    """Open a path, or reveal a file, with the host file manager."""
    path = os.path.normpath(path)
    if not os.path.exists(path):
        return False
    platform = platform or sys.platform
    try:
        if platform == "win32":
            if os.path.isfile(path) and reveal:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)
        elif platform == "darwin":
            subprocess.Popen(["open", "-R", path] if os.path.isfile(path) and reveal
                             else ["open", path])
        else:
            target = os.path.dirname(path) if os.path.isfile(path) and reveal else path
            subprocess.Popen(["xdg-open", target])
        return True
    except (OSError, subprocess.SubprocessError):
        return False
