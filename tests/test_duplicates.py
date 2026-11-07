import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fetchlane import jobs
from fetchlane.cli import main
from fetchlane.identity import request_key


class DuplicatePromptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"FETCHLANE_DB": str(self.root / "jobs.sqlite3"),
                                              "FETCHLANE_CONFIG": str(self.root / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.url = "https://example.com/audio"
        self.arguments = ["audio", self.url, "--format", "mp3", "--output", str(self.root)]
        self.saved = self.root / "audio.mp3"
        self.saved.write_bytes(b"existing media")
        options = {"output": str(self.root), "format": "mp3"}
        job_id = jobs.start("audio", [self.url], options,
                            request_key=request_key("audio", [self.url], options, []))
        report = self.root / "report.jsonl"
        report.write_text(json.dumps({"title": "Audio", "path": str(self.saved)}) + "\n", encoding="utf-8")
        jobs.finish(job_id, 0, report)

    def test_overwrite_prompts_and_passes_force_flag(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="o"), \
                patch("fetchlane.cli.run_downloader", return_value=0) as download, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(self.arguments), 0)
        self.assertIn(str(self.saved), output.getvalue())
        self.assertIn("--force-overwrites", download.call_args.args[0])

    def test_change_location_creates_job_in_new_destination(self):
        destination = self.root / "new folder"
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["l", str(destination)]), \
                patch("fetchlane.cli.run_downloader", return_value=0) as download, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.arguments), 0)
        self.assertEqual(jobs.get(2)["options"]["output"], str(destination))
        arguments = download.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--paths") + 1], str(destination))
        self.assertNotIn("--force-overwrites", arguments)

    def test_invalid_choice_then_skip_creates_no_job(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["invalid", "s"]), \
                patch("fetchlane.cli.run_downloader") as download, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(self.arguments), 0)
        download.assert_not_called()
        self.assertIsNone(jobs.get(2))

    def test_noninteractive_duplicate_requires_explicit_choice(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.input") as prompt, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(self.arguments), 1)
        prompt.assert_not_called()
        self.assertIn("--on-duplicate", errors.getvalue())

    def test_explicit_overwrite_and_skip_do_not_prompt(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.input") as prompt, \
                patch("fetchlane.cli.run_downloader", return_value=0) as download, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*self.arguments, "--on-duplicate", "skip"]), 0)
            download.assert_not_called()
            self.assertEqual(main([*self.arguments, "--on-duplicate", "overwrite"]), 0)
        prompt.assert_not_called()
        self.assertIn("--force-overwrites", download.call_args.args[0])

    def test_cancel_at_prompt_returns_130(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self.arguments), 130)
        self.assertIsNone(jobs.get(2))
