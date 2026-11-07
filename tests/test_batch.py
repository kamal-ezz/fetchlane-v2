import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fetchlane import batch, jobs
from fetchlane.cli import main, media_url


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "links.txt"
        self.database = self.root / "jobs.sqlite3"
        environment = patch.dict(os.environ, {"FETCHLANE_DB": str(self.database),
                                              "FETCHLANE_CONFIG": str(self.root / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        terminal = patch("sys.stdin.isatty", return_value=False)
        terminal.start()
        self.addCleanup(terminal.stop)

    def test_comments_bom_crlf_and_duplicates_preserve_order(self):
        self.file.write_bytes(b"\xef\xbb\xbf# Queue\r\n\r\n https://example.com/a \r\nhttps://example.com/b\r\nhttps://example.com/a\r\n")
        self.assertEqual(batch.read_urls(self.file, media_url),
                         ["https://example.com/a", "https://example.com/b", "https://example.com/a"])

    def test_invalid_input_is_rejected_before_any_job(self):
        for contents in ("https://example.com/a\nnot-a-url\n", "# Empty\n", "https://example.com/a b\n"):
            with self.subTest(contents=contents):
                self.file.write_text(contents, encoding="utf-8")
                with patch("fetchlane.cli.run_downloader") as download, \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["batch", str(self.file)])
                self.assertEqual(error.exception.code, 2)
                download.assert_not_called()
                self.assertFalse(self.database.exists())

    def test_line_number_and_queue_bounds(self):
        self.file.write_text("# comment\ninvalid\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, ":2:"):
            batch.read_urls(self.file, media_url)
        self.file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
        with patch.object(batch, "MAX_URLS", 1), self.assertRaisesRegex(ValueError, "exceeds 1 links"):
            batch.read_urls(self.file, media_url)

    def test_continues_after_failure_and_tracks_individual_jobs(self):
        self.file.write_text("https://example.com/a\nhttps://example.com/b\nhttps://example.com/c\n", encoding="utf-8")
        with patch("fetchlane.cli.run_downloader", side_effect=[1, 0, 0]) as download, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["batch", str(self.file), "--type", "audio", "--format", "mp3"]), 1)
        self.assertEqual(download.call_count, 3)
        records = list(reversed(jobs.recent(10)))
        self.assertEqual([row["status"] for row in records], ["failed", "succeeded", "succeeded"])
        self.assertEqual([row["options"]["batch_position"] for row in records], [1, 2, 3])
        self.assertTrue(all(row["command"] == "audio" for row in records))
        self.assertIn("2 succeeded, 1 failed", output.getvalue())

    def test_interrupt_stops_remaining_links(self):
        self.file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
        with patch("fetchlane.cli.run_downloader", return_value=130) as download, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["batch", str(self.file)]), 130)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(jobs.get(1)["status"], "interrupted")
        self.assertIn("1 remaining", output.getvalue())

    def test_dry_run_preserves_passthrough_and_creates_no_jobs(self):
        self.file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
        with patch("fetchlane.cli.run_downloader") as download, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["batch", str(self.file), "--dry-run", "--", "--socket-timeout", "5"]), 0)
        download.assert_not_called()
        self.assertEqual(output.getvalue().count("--socket-timeout 5"), 2)
        self.assertFalse(self.database.exists())

    def test_stopping_one_job_keeps_the_sequential_queue_running(self):
        self.file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
        calls = 0

        def download(arguments):
            nonlocal calls
            calls += 1
            if calls == 1:
                jobs.request_stop(jobs.recent(1)[0]["id"])
                return 130
            return 0

        with patch("fetchlane.cli.run_downloader", side_effect=download), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["batch", str(self.file)]), 1)
        self.assertEqual(calls, 2)
        self.assertEqual(jobs.get(1)["status"], "stopped")
        self.assertEqual(jobs.get(2)["status"], "succeeded")
