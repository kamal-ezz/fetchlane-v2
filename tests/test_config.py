from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fetchlane import config, jobs
from fetchlane.cli import main


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.configuration = self.root / "preferences" / "config.json"
        environment = patch.dict(os.environ, {"FETCHLANE_CONFIG": str(self.configuration),
                                              "FETCHLANE_DB": str(self.root / "jobs.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        home = patch("fetchlane.config.Path.home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        downloads = patch("fetchlane.config.downloads_directory", return_value=self.root / "Downloads")
        downloads.start()
        self.addCleanup(downloads.stop)

    def test_first_launch_accepts_downloads_and_only_prompts_once(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="y") as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        prompt.assert_called_once()
        self.assertIn("Is this folder right?", prompt.call_args.args[0])
        self.assertIn(str(self.root / "Downloads"), output.getvalue())
        self.assertEqual(config.load_directory(), self.root / "Downloads")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as prompt, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
        prompt.assert_not_called()

    def test_first_download_can_select_another_folder(self):
        destination = self.root / "Media folder 日本語"
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["n", str(destination)]), \
                patch("fetchlane.cli.run_downloader", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["audio", "https://example.com/a"]), 0)
        self.assertEqual(config.load_directory(), destination)
        self.assertEqual(jobs.get(1)["options"]["output"], str(destination))

    def test_config_command_changes_saved_default(self):
        destination = self.root / "Media"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["config", "--output", str(destination)]), 0)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["config"]), 0)
        self.assertIn(str(destination), output.getvalue())
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", "https://example.com/a", "--dry-run"]), 0)
        self.assertIn(str(destination), output.getvalue())
        prompt.assert_not_called()

    def test_output_override_preserves_saved_default(self):
        config.save_directory(self.root / "Default")
        override = self.root / "Override"
        with patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", "https://example.com/a", "--output", str(override), "--dry-run"]), 0)
        self.assertIn(str(override), output.getvalue())
        self.assertEqual(config.load_directory(), self.root / "Default")
        prompt.assert_not_called()

    def test_explicit_output_does_not_require_first_run_setup(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as prompt, \
                patch("fetchlane.cli.run_downloader", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["video", "https://example.com/a", "--output", str(self.root)]), 0)
        prompt.assert_not_called()
        self.assertFalse(self.configuration.exists())

    def test_noninteractive_and_preview_use_downloads_without_saving(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.input") as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", "https://example.com/a", "--dry-run"]), 0)
        self.assertIn(str(self.root / "Downloads"), output.getvalue())
        self.assertFalse(self.configuration.exists())
        prompt.assert_not_called()

    def test_help_and_version_do_not_trigger_setup(self):
        for option in ("--help", "--version"):
            with self.subTest(option=option), patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as result:
                main([option])
            self.assertEqual(result.exception.code, 0)
            prompt.assert_not_called()
        self.assertFalse(self.configuration.exists())

    def test_cancel_first_run_saves_nothing_and_starts_no_job(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError), \
                patch("fetchlane.cli.run_downloader") as download, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["video", "https://example.com/a"]), 130)
        self.assertFalse(self.configuration.exists())
        self.assertFalse((self.root / "jobs.sqlite3").exists())
        download.assert_not_called()

    def test_rejects_existing_file_as_destination(self):
        occupied = self.root / "file"
        occupied.write_text("keep me", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["config", "--output", str(occupied)]), 1)
        self.assertFalse(self.configuration.exists())
        self.assertEqual(occupied.read_text(encoding="utf-8"), "keep me")

    def test_invalid_configuration_is_reported_and_can_be_repaired(self):
        self.configuration.parent.mkdir()
        self.configuration.write_text("broken json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid configuration"):
            config.default_directory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["config", "--output", str(self.root / "Repaired")]), 0)
        self.assertEqual(config.load_directory(), self.root / "Repaired")

    def test_concurrent_writes_leave_one_complete_configuration(self):
        destinations = [self.root / f"folder-{index}" for index in range(8)]
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(config.save_directory, destinations))
        self.assertIn(config.load_directory(), destinations)
        self.assertIn("download_directory", json.loads(self.configuration.read_text(encoding="utf-8")))
        self.assertEqual(list(self.configuration.parent.glob(".fetchlane-*")), [])

    def test_settings_command_saves_all_defaults_and_downloads_use_them(self):
        destination = self.root / "Media"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["settings", "--downloads", str(destination), "--max-concurrent", "3",
                                   "--audio-format", "mp3", "--video-format", "mp4"]), 0)
        settings = config.load_settings()
        self.assertEqual(settings["max_concurrent_downloads"], 3)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["audio", "https://example.com/a", "--dry-run"]), 0)
        self.assertIn("--audio-format mp3", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", "https://example.com/a", "--dry-run"]), 0)
        self.assertIn("--merge-output-format mp4", output.getvalue())
        self.assertIn("--remux-video mp4", output.getvalue())

    def test_per_command_format_overrides_do_not_change_defaults(self):
        config.save_settings({"audio_format": "mp3", "video_format": "mp4"})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["video", "https://example.com/a", "--format", "mkv", "--dry-run"]), 0)
        self.assertIn("--remux-video mkv", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["audio", "https://example.com/a", "--format", "opus", "--dry-run"]), 0)
        self.assertIn("--audio-format opus", output.getvalue())
        self.assertEqual(config.load_settings()["audio_format"], "mp3")
        self.assertEqual(config.load_settings()["video_format"], "mp4")

    def test_legacy_folder_only_configuration_and_updates_preserve_fields(self):
        self.configuration.parent.mkdir()
        self.configuration.write_text(json.dumps({"download_directory": str(self.root)}), encoding="utf-8")
        self.assertEqual(config.load_settings()["max_concurrent_downloads"], 1)
        config.save_settings({"audio_format": "mp3", "max_concurrent_downloads": 2})
        config.save_directory(self.root / "Another")
        self.assertEqual(config.load_settings()["audio_format"], "mp3")
        self.assertEqual(config.load_settings()["max_concurrent_downloads"], 2)

    def test_concurrent_updates_to_different_settings_are_not_lost(self):
        changes = [{"audio_format": "mp3"}, {"video_format": "mkv"}, {"max_concurrent_downloads": 3}]
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(config.save_settings, changes))
        self.assertEqual(config.load_settings()["audio_format"], "mp3")
        self.assertEqual(config.load_settings()["video_format"], "mkv")
        self.assertEqual(config.load_settings()["max_concurrent_downloads"], 3)

    def test_settings_reject_invalid_limits_before_saving(self):
        for value in ("0", "17", "many"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["settings", "--max-concurrent", value])
        self.assertFalse(self.configuration.exists())
        with self.assertRaises(ValueError):
            config.save_settings({"max_concurrent_downloads": True})

    def test_batch_can_override_saved_concurrency(self):
        config.save_settings({"max_concurrent_downloads": 3})
        links = self.root / "links.txt"
        links.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
        with patch("fetchlane.cli.run_downloader", return_value=0) as download, \
                patch("fetchlane.workers.run_parallel") as parallel, \
                patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["batch", str(links), "--max-concurrent", "1"]), 0)
        self.assertEqual(download.call_count, 2)
        parallel.assert_not_called()
        self.assertEqual(config.load_settings()["max_concurrent_downloads"], 3)
