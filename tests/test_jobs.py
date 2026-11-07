from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
import signal
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fetchlane import jobs
from fetchlane.cli import main
from fetchlane.identity import request_key


class JobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "state" / "jobs.sqlite3"
        environment = patch.dict(os.environ, {"FETCHLANE_DB": str(self.database),
                                              "FETCHLANE_CONFIG": str(self.root / "config.json")})
        environment.start()
        self.addCleanup(environment.stop)
        terminal = patch("sys.stdin.isatty", return_value=False)
        terminal.start()
        self.addCleanup(terminal.stop)

    def test_lifecycle_and_files_survive_reopening_database(self):
        job_id = jobs.start("playlist", ["https://example.com/list"], {"quality": "1080"})
        self.assertEqual(jobs.get(job_id)["status"], "running")
        report = self.root / "files.jsonl"
        record = {"title": 'A "title" 日本語', "path": str(self.root / "song.mp3"), "url": "https://example.com/1"}
        report.write_text(json.dumps(record) + "\n", encoding="utf-8")
        jobs.finish(job_id, 0, report)
        job = jobs.get(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(job["files"], [record])
        self.assertEqual(job["options"], {"quality": "1080"})

    def test_failure_and_interrupt_keep_partial_files(self):
        report = self.root / "files.jsonl"
        report.write_text(json.dumps({"title": "First", "path": "/tmp/first.mp3"}) + "\n", encoding="utf-8")
        for code, expected in ((1, "failed"), (130, "interrupted")):
            with self.subTest(code=code):
                job_id = jobs.start("playlist", ["https://example.com/list"], {})
                jobs.finish(job_id, code, report)
                job = jobs.get(job_id)
                self.assertEqual(job["status"], expected)
                self.assertEqual(job["exit_code"], code)
                self.assertEqual(len(job["files"]), 1)

    def test_concurrent_connections_have_distinct_ids(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            ids = list(workers.map(lambda _: jobs.start("video", ["https://example.com/a"], {}), range(24)))
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(len(jobs.recent(3)), 3)
        self.assertEqual(len(jobs.recent(200, "succeeded")), 0)

    def test_cli_list_details_and_missing_job(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["jobs"]), 0)
        self.assertIn("No jobs found", output.getvalue())
        job_id = jobs.start("audio", ["https://example.com/a"], {})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["jobs", str(job_id), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "running")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["jobs", "999"]), 1)

    def test_dry_run_and_info_do_not_create_jobs(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("fetchlane.cli.run_downloader", return_value=0):
            self.assertEqual(main(["video", "https://example.com/a", "--dry-run"]), 0)
            self.assertEqual(main(["info", "https://example.com/a"]), 0)
        self.assertFalse(self.database.exists())

    def test_unwritable_database_stops_before_downloader(self):
        with patch.dict(os.environ, {"FETCHLANE_DB": str(self.root)}), \
                patch("fetchlane.cli.run_downloader") as download, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["video", "https://example.com/a"]), 1)
        download.assert_not_called()
        self.assertIn("could not save job history", errors.getvalue())

    def test_cli_records_interruption_and_nonzero_exit(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("fetchlane.cli.run_downloader", return_value=7):
            self.assertEqual(main(["audio", "https://example.com/a"]), 7)
        self.assertEqual(jobs.get(1)["status"], "failed")
        with contextlib.redirect_stdout(io.StringIO()), patch("fetchlane.cli.run_downloader", side_effect=KeyboardInterrupt):
            self.assertEqual(main(["video", "https://example.com/a"]), 130)
        self.assertEqual(jobs.get(2)["status"], "interrupted")

    def test_interruption_is_recorded_when_downloader_catches_signal(self):
        def catch_interrupt(arguments):
            try:
                signal.raise_signal(signal.SIGINT)
            except KeyboardInterrupt:
                return 1

        original = signal.getsignal(signal.SIGINT)
        with contextlib.redirect_stdout(io.StringIO()), patch("fetchlane.cli.run_downloader", side_effect=catch_interrupt):
            self.assertEqual(main(["audio", "https://example.com/a"]), 130)
        self.assertEqual(jobs.get(1)["status"], "interrupted")
        self.assertEqual(signal.getsignal(signal.SIGINT), original)

    def completed(self, key="same-request"):
        path = self.root / "saved.mp3"
        path.write_bytes(b"media")
        report = self.root / "files.jsonl"
        report.write_text(json.dumps({"title": "Saved", "path": str(path)}) + "\n", encoding="utf-8")
        job_id = jobs.start("audio", ["https://example.com/a"], {}, request_key=key)
        jobs.finish(job_id, 0, report)
        return job_id, path

    def test_duplicate_detection_checks_existing_files_and_override(self):
        job_id, path = self.completed()
        with self.assertRaises(jobs.DuplicateRequest) as duplicate:
            jobs.start("audio", ["https://example.com/a"], {}, request_key="same-request")
        self.assertEqual(duplicate.exception.job_id, job_id)
        self.assertFalse(duplicate.exception.active)
        override = jobs.start("audio", ["https://example.com/a"], {}, request_key="same-request", allow_duplicates=True)
        with self.assertRaises(jobs.DuplicateRequest) as duplicate:
            jobs.start("audio", ["https://example.com/a"], {}, request_key="same-request", allow_duplicates=True)
        self.assertTrue(duplicate.exception.active)
        jobs.finish(override, 1, self.root / "no-report")
        path.unlink()
        self.assertGreater(jobs.start("audio", ["https://example.com/a"], {}, request_key="same-request"), override)

    def test_concurrent_matching_requests_have_one_owner(self):
        def reserve(_):
            try:
                return jobs.start("video", ["https://example.com/a"], {}, request_key="shared")
            except jobs.DuplicateRequest:
                return None
        with ThreadPoolExecutor(max_workers=8) as workers:
            ids = list(workers.map(reserve, range(16)))
        self.assertEqual(sum(value is not None for value in ids), 1)

    def test_completed_playlists_are_refreshed_for_new_entries(self):
        first, _ = self.completed(key="playlist-request")
        second = jobs.start("playlist", ["https://example.com/list"], {}, request_key="playlist-request")
        self.assertGreater(second, first)

    def test_stop_is_cooperative_and_finish_records_stopped(self):
        job_id = jobs.start("audio", ["https://example.com/a"], {})
        with patch("fetchlane.jobs.os.kill") as kill:
            jobs.request_stop(job_id)
        kill.assert_not_called()
        self.assertEqual(jobs.get(job_id)["status"], "stopping")
        self.assertEqual(jobs.finish(job_id, 0, self.root / "no-report"), 130)
        self.assertEqual(jobs.get(job_id)["status"], "stopped")

    def test_remove_preserves_files_and_refuses_active_jobs(self):
        job_id, path = self.completed()
        jobs.remove(job_id)
        self.assertIsNone(jobs.get(job_id))
        self.assertTrue(path.is_file())
        active = jobs.start("video", ["https://example.com/a"], {})
        with self.assertRaises(ValueError):
            jobs.remove(active, force=True)
        with jobs.connect() as connection, connection:
            connection.execute("UPDATE jobs SET heartbeat_at = 0 WHERE id = ?", (active,))
        with self.assertRaises(ValueError):
            jobs.request_stop(active)
        jobs.remove(active, force=True)
        self.assertIsNone(jobs.get(active))

    def test_retry_uses_saved_settings_and_links_to_original(self):
        original = jobs.start("audio", ["https://example.com/a"],
                              {"output": str(self.root), "format": "mp3", "quality": "best"})
        jobs.finish(original, 1, self.root / "no-report")
        with patch("fetchlane.cli.run_downloader", return_value=0) as download, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["jobs", "retry", str(original)]), 0)
        arguments = download.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--audio-format") + 1], "mp3")
        self.assertEqual(jobs.get(2)["options"]["retry_of"], original)
        self.assertEqual(jobs.get(original)["status"], "failed")

    def test_stop_dry_run_is_rejected_without_stopping(self):
        job_id = jobs.start("audio", ["https://example.com/a"], {})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["jobs", "stop", str(job_id), "--dry-run"])
        self.assertEqual(jobs.get(job_id)["status"], "running")

    def test_legacy_database_is_migrated_without_losing_rows(self):
        self.database.parent.mkdir()
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("""CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, urls TEXT NOT NULL,
                options TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, exit_code INTEGER, pid INTEGER NOT NULL)""")
            connection.execute("""INSERT INTO jobs VALUES
                (1, 'audio', '["https://example.com/a"]', '{}', 'failed', '2026-01-01', NULL, 1, 123)""")
        row = jobs.get(1)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["request_key"])


class IdentityTests(unittest.TestCase):
    def test_youtube_aliases_and_settings(self):
        options = {"output": "/downloads", "format": "mp3"}
        urls = ["https://www.youtube.com/watch?v=Zg_zgXlmceg&list=LL&index=18",
                "https://youtu.be/Zg_zgXlmceg?t=30", "https://www.youtube.com/shorts/Zg_zgXlmceg"]
        keys = {request_key("audio", [url], options, []) for url in urls}
        self.assertEqual(len(keys), 1)
        self.assertNotEqual(request_key("audio", [urls[0]], options, []),
                            request_key("audio", [urls[0]], {**options, "format": "opus"}, []))
        self.assertNotEqual(request_key("audio", [urls[0]], options, []),
                            request_key("audio", [urls[0]], {**options, "output": "/other"}, []))
        self.assertNotEqual(request_key("audio", [urls[0]], options, []),
                            request_key("audio", [urls[0]], options, ["--download-sections", "*0-30"]))

    def test_advanced_url_dependent_options_preserve_timestamps(self):
        options = {"output": "/downloads", "format": "mp3"}
        extra = ["--download-sections", "*from-url"]
        self.assertNotEqual(request_key("audio", ["https://youtu.be/Zg_zgXlmceg?t=30"], options, extra),
                            request_key("audio", ["https://youtu.be/Zg_zgXlmceg?t=60"], options, extra))
