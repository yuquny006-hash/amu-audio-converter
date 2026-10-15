import os
import tempfile
import unittest
from unittest import mock

import converter
import platform_utils


class ToolResolutionTests(unittest.TestCase):
    def test_frozen_bundle_finds_tool_in_meipass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = os.path.join(temp_dir, "ffprobe")
            with open(tool, "wb") as stream:
                stream.write(b"bin")
            with mock.patch.object(converter.shutil, "which", return_value=None), \
                    mock.patch.object(converter.sys, "platform", "darwin"), \
                    mock.patch.object(converter.sys, "_MEIPASS", temp_dir, create=True):
                self.assertEqual(converter._find_tool("ffprobe"), tool)

    def test_frozen_macos_finds_extensionless_tool_next_to_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tool = os.path.join(temp_dir, "ffmpeg")
            with open(tool, "wb") as stream:
                stream.write(b"bin")
            with mock.patch.object(converter.shutil, "which", return_value=None), \
                    mock.patch.object(converter.sys, "executable", os.path.join(temp_dir, "AmuMusic")), \
                    mock.patch.object(converter.sys, "platform", "darwin"):
                self.assertEqual(converter._find_tool("ffmpeg"), tool)

    def test_subprocess_options_hide_windows_console_only_on_windows(self):
        self.assertEqual(platform_utils.subprocess_options("darwin"), {})
        self.assertEqual(platform_utils.subprocess_options("win32"),
                         {"creationflags": 0x08000000})


class FileManagerTests(unittest.TestCase):
    def test_macos_reveal_uses_finder_reveal(self):
        with tempfile.NamedTemporaryFile() as source, \
                mock.patch.object(platform_utils.subprocess, "Popen") as popen:
            self.assertTrue(platform_utils.open_in_file_manager(
                source.name, reveal=True, platform="darwin"))
            popen.assert_called_once_with(["open", "-R", os.path.normpath(source.name)])

    def test_macos_open_folder_uses_open(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(platform_utils.subprocess, "Popen") as popen:
            self.assertTrue(platform_utils.open_in_file_manager(
                folder, platform="darwin"))
            popen.assert_called_once_with(["open", os.path.normpath(folder)])


if __name__ == "__main__":
    unittest.main()
