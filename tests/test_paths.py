import ctypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from fetchlane import config
from fetchlane.paths import downloads_directory, windows_downloads_directory


class PlatformPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = patch.dict(os.environ, {"FETCHLANE_CONFIG": "", "APPDATA": str(self.root / "Roaming"),
                                                   "XDG_CONFIG_HOME": str(self.root / "xdg")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        home = patch("pathlib.Path.home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)

    def test_configuration_directory_for_each_platform(self):
        expected = {"win32": self.root / "Roaming" / "fetchlane" / "config.json",
                    "darwin": self.root / "Library" / "Application Support" / "fetchlane" / "config.json",
                    "linux": self.root / "xdg" / "fetchlane" / "config.json"}
        for platform, destination in expected.items():
            with self.subTest(platform=platform), patch("sys.platform", platform):
                self.assertEqual(config.config_path(), destination)

    def test_windows_redirected_downloads_folder(self):
        redirected = self.root / "OneDrive" / "Downloads 日本語"
        with patch("sys.platform", "win32"), \
                patch("fetchlane.paths.windows_downloads_directory", return_value=redirected):
            self.assertEqual(downloads_directory(), redirected)

    def test_windows_api_uses_guid_and_frees_native_memory(self):
        native_path = str(self.root / "Redirected Downloads 日本語")
        buffer = ctypes.create_unicode_buffer(native_path)

        def get_path(identifier, flags, token, output):
            expected = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
            self.assertEqual(ctypes.string_at(identifier, 16), expected)
            self.assertEqual(flags, 0x4000)
            self.assertIsNone(token)
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(buffer, ctypes.c_void_p).value
            return 0

        shell = Mock(SHGetKnownFolderPath=Mock(side_effect=get_path))
        ole = Mock(CoTaskMemFree=Mock())
        with patch("ctypes.WinDLL", side_effect=[shell, ole], create=True):
            self.assertEqual(windows_downloads_directory(), Path(native_path))
        ole.CoTaskMemFree.assert_called_once()

    def test_windows_api_failure_falls_back_to_home_downloads(self):
        with patch("sys.platform", "win32"), \
                patch("fetchlane.paths.windows_downloads_directory", side_effect=OSError("unavailable")):
            self.assertEqual(downloads_directory(), self.root / "Downloads")

    def test_linux_localized_downloads_are_read_without_executing_shell(self):
        root = self.root / "xdg"
        root.mkdir()
        (root / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Téléchargements 日本語"\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            self.assertEqual(downloads_directory(), self.root / "Téléchargements 日本語")

    def test_malformed_xdg_configuration_falls_back(self):
        root = self.root / "xdg"
        root.mkdir()
        (root / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$(touch unexpected)"\n', encoding="utf-8")
        with patch("sys.platform", "linux"):
            self.assertEqual(downloads_directory(), self.root / "Downloads")
        self.assertFalse((self.root / "unexpected").exists())

    def test_macos_uses_home_downloads(self):
        with patch("sys.platform", "darwin"):
            self.assertEqual(downloads_directory(), self.root / "Downloads")

    @unittest.skipUnless(sys.platform == "win32", "requires the real Windows Known Folder API")
    def test_native_windows_download_folder_api(self):
        destination = windows_downloads_directory()
        self.assertIsNotNone(destination)
        self.assertTrue(destination.is_absolute())
