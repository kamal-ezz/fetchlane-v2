import contextlib
import io
import subprocess
import sys
import types
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fetchlane.cli import build_arguments, create_parser, main


URL = "https://example.com/watch?v=123&list=456"


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"FETCHLANE_DB": str(Path(temporary.name) / "jobs.sqlite3"),
                                              "FETCHLANE_CONFIG": str(Path(temporary.name) / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        terminal = patch("sys.stdin.isatty", return_value=False)
        terminal.start()
        self.addCleanup(terminal.stop)

    def test_no_arguments_shows_guide_without_error(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main([]), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn('fetchlane audio "URL" --format mp3', output.getvalue())
        self.assertIn("fetchlane video --help", output.getvalue())

    def arguments(self, *options):
        return build_arguments(create_parser().parse_args(list(options)), [])

    def test_video_height_applies_to_both_format_paths(self):
        arguments = self.arguments("video", URL, "--quality", "1080")
        self.assertIn("bv*[height<=1080]+ba/b[height<=1080]", arguments)
        self.assertIn("--no-playlist", arguments)
        self.assertEqual(arguments[-2:], ["--", URL])

    def test_audio_and_playlist(self):
        audio = self.arguments("audio", URL, "--format", "mp3")
        self.assertIn("--extract-audio", audio)
        self.assertEqual(audio[audio.index("--audio-format") + 1], "mp3")
        playlist = self.arguments("playlist", URL, "--items", "1:10")
        self.assertIn("--yes-playlist", playlist)
        self.assertIn("1:10", playlist)
        self.assertIn("%(playlist_index)03d", playlist[playlist.index("--output") + 1])

    def test_info_does_not_download(self):
        arguments = self.arguments("info", URL, "--json")
        self.assertIn("--simulate", arguments)
        self.assertIn("--dump-json", arguments)
        self.assertNotIn("--output", arguments)

    def test_browser_impersonation_is_opt_in_for_all_commands(self):
        for command in ("video", "audio", "playlist", "info"):
            with self.subTest(command=command):
                self.assertNotIn("--impersonate", self.arguments(command, URL))
                arguments = self.arguments(command, URL, "--impersonate", "chrome")
                self.assertEqual(arguments[arguments.index("--impersonate") + 1], "chrome")

    def test_invalid_inputs_fail_before_execution(self):
        for options in (("video", "--exec=bad"), ("video", "file:///tmp/a"),
                        ("video", URL, "--quality", "0"),
                        ("video", URL, "--quality", "1080p"),
                        ("video", URL, "--qu", "720")):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(list(options))
                self.assertEqual(error.exception.code, 2)

    def test_dry_run_never_starts_downloader(self):
        with patch("fetchlane.cli.run_downloader") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", URL, "--dry-run"]), 0)
        run.assert_not_called()
        self.assertIn("yt-dlp --ignore-config", output.getvalue())

    @patch("fetchlane.cli.importlib.util.find_spec", return_value=object())
    def test_passthrough_preserves_arguments_without_shell(self, _spec):
        downloader = types.ModuleType("yt_dlp")
        downloader.main = Mock(side_effect=SystemExit(7))
        with patch.dict(sys.modules, {"yt_dlp": downloader}):
            self.assertEqual(main(["video", URL, "--", "--output", "a path/$(touch nope).%(ext)s"]), 7)
        arguments = downloader.main.call_args.args[0]
        self.assertEqual(arguments[-4:], ["--output", "a path/$(touch nope).%(ext)s", "--", URL])

    @patch("fetchlane.cli.importlib.util.find_spec", return_value=None)
    def test_missing_dependency_is_actionable(self, _spec):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["video", URL]), 127)
        self.assertIn("pip install", output.getvalue())

    @patch("fetchlane.cli.importlib.util.find_spec", return_value=object())
    def test_interrupt(self, _spec):
        downloader = types.ModuleType("yt_dlp")
        downloader.main = Mock(side_effect=KeyboardInterrupt)
        with patch.dict(sys.modules, {"yt_dlp": downloader}):
            self.assertEqual(main(["video", URL]), 130)

    def test_frozen_binary_invokes_bundled_downloader(self):
        from unittest.mock import Mock

        downloader = types.ModuleType("yt_dlp")
        downloader.main = Mock(side_effect=SystemExit(7))
        with patch.object(sys, "frozen", True, create=True), \
                patch.dict(sys.modules, {"yt_dlp": downloader}):
            self.assertEqual(main(["video", URL]), 7)
        self.assertEqual(downloader.main.call_args.args[0][-2:], ["--", URL])

    def test_frozen_binary_interrupt(self):
        from unittest.mock import Mock

        downloader = types.ModuleType("yt_dlp")
        downloader.main = Mock(side_effect=KeyboardInterrupt)
        with patch.object(sys, "frozen", True, create=True), \
                patch.dict(sys.modules, {"yt_dlp": downloader}):
            self.assertEqual(main(["audio", URL]), 130)

    def test_module_entrypoint(self):
        result = subprocess.run([sys.executable, "-m", "fetchlane", "video", URL, "--dry-run"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("yt-dlp", result.stdout)


if __name__ == "__main__":
    unittest.main()
