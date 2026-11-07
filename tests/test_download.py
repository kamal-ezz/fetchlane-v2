"""Exercise real yt-dlp downloads against local media without external services."""

from functools import partial
import contextlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/timed-"):
            data = (Path(self.directory) / self.path.lstrip("/")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with self.server.request_lock:
                self.server.active_requests += 1
                self.server.peak_requests = max(self.server.peak_requests, self.server.active_requests)
            try:
                for index in range(0, len(data), 4096):
                    self.wfile.write(data[index:index + 4096])
                    self.wfile.flush()
                    time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with self.server.request_lock:
                    self.server.active_requests -= 1
            return
        if self.path != "/slow.wav":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(32 * 1024 * 1024))
        self.end_headers()
        self.server.slow_started.set()
        try:
            for _ in range(512):
                self.wfile.write(b"\0" * 65536)
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(importlib.util.find_spec("yt_dlp") and shutil.which("ffmpeg"),
                     "requires yt-dlp and ffmpeg")
class DownloadOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "jobs.sqlite3"
        self.environment = {**os.environ, "FETCHLANE_DB": str(self.database),
                            "FETCHLANE_CONFIG": str(self.root / "config.json")}
        with wave.open(str(self.root / "sample.wav"), "wb") as audio:
            audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 8000)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=self.root))
        self.server.slow_started = threading.Event()
        self.server.request_lock = threading.Lock()
        self.server.active_requests = self.server.peak_requests = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_download(self, filename, *options):
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        return subprocess.run(
            [*command, "audio", f"{self.url}/{filename}",
             "--format", "mp3", "--output", str(self.root / "downloads"), *options],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=self.environment,
        )

    def cli(self, *arguments, **kwargs):
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        return subprocess.run([*command, *arguments], capture_output=True, text=True, encoding="utf-8", timeout=30,
                              env=self.environment, **kwargs)

    def test_clean_output_reports_final_converted_file(self):
        result = self.run_download("sample.wav")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Downloading: sample", result.stdout)
        self.assertIn("Processing media...", result.stdout)
        saved = next(line.removeprefix("Saved: ") for line in result.stdout.splitlines()
                     if line.startswith("Saved: "))
        self.assertEqual(Path(saved).suffix, ".mp3")
        self.assertTrue(Path(saved).is_file(), result.stdout)
        self.assertNotIn("[generic]", result.stdout + result.stderr)
        self.assertNotIn("[ExtractAudio]", result.stdout + result.stderr)
        self.assertNotIn("[download]", result.stdout + result.stderr)
        history_command = [os.environ["FETCHLANE_TEST_BINARY"]] if os.environ.get("FETCHLANE_TEST_BINARY") \
            else [sys.executable, "-m", "fetchlane"]
        history = subprocess.run([*history_command, "jobs", "1", "--json"], check=True,
                                 capture_output=True, text=True, encoding="utf-8", timeout=30,
                                 env=self.environment)
        job = json.loads(history.stdout)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["files"][0]["path"], saved)

    def test_failed_download_keeps_error_and_does_not_report_saved(self):
        result = self.run_download("missing.wav")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)
        self.assertNotIn("Saved:", result.stdout)

    def test_duplicates_skip_until_file_is_deleted(self):
        first = self.run_download("sample.wav")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_download("sample.wav", "--on-duplicate", "skip")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Skipped:", second.stdout)
        job = json.loads(self.cli("jobs", "1", "--json").stdout)
        Path(job["files"][0]["path"]).unlink()
        third = self.run_download("sample.wav")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("Saved:", third.stdout)

    def test_retry_failed_job_after_source_recovers(self):
        failed = self.run_download("missing.wav")
        self.assertEqual(failed.returncode, 1, failed.stderr)
        (self.root / "missing.wav").write_bytes((self.root / "sample.wav").read_bytes())
        retry = self.cli("jobs", "retry", "1")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        original = json.loads(self.cli("jobs", "1", "--json").stdout)
        replacement = json.loads(self.cli("jobs", "2", "--json").stdout)
        self.assertEqual(original["status"], "failed")
        self.assertEqual(replacement["status"], "succeeded")
        self.assertEqual(replacement["options"]["retry_of"], 1)
        self.assertTrue(Path(replacement["files"][0]["path"]).is_file())

    def test_stop_retry_and_remove_from_another_process(self):
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        process = subprocess.Popen(
            [*command, "audio", f"{self.url}/slow.wav", "--output", str(self.root / "downloads")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            env=self.environment,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.database.exists():
                    with contextlib.closing(sqlite3.connect(self.database)) as connection:
                        try:
                            row = connection.execute("SELECT id FROM jobs WHERE status = 'running'").fetchone()
                        except sqlite3.OperationalError:
                            row = None  # Schema creation may still be committing.
                    if row:
                        break
                time.sleep(0.05)
            else:
                self.fail("Download never created a running job")
            self.assertTrue(self.server.slow_started.wait(timeout=20), "Download never reached the HTTP server")
            stopped = self.cli("jobs", "stop", str(row[0]))
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            job = json.loads(self.cli("jobs", str(row[0]), "--json").stdout)
            self.assertEqual(job["status"], "stopped")
            retry = self.cli("jobs", "retry", str(row[0]), "--dry-run")
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertIn(f"{self.url}/slow.wav", retry.stdout)
            removed = self.cli("jobs", "remove", str(row[0]))
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(self.cli("jobs", str(row[0])).returncode, 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def test_batch_skips_repeated_saved_links(self):
        links = self.root / "duplicates.txt"
        links.write_text(f"{self.url}/sample.wav\n{self.url}/sample.wav\n", encoding="utf-8")
        result = self.cli("batch", str(links), "--type", "audio", "--format", "mp3",
                          "--output", str(self.root / "downloads"), "--on-duplicate", "skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 succeeded, 0 failed, 1 skipped", result.stdout)
        self.assertEqual(len(json.loads(self.cli("jobs", "--json").stdout)), 1)

    def test_duplicate_overwrite_replaces_existing_media(self):
        first = self.run_download("sample.wav")
        self.assertEqual(first.returncode, 0, first.stderr)
        job = json.loads(self.cli("jobs", "1", "--json").stdout)
        saved = Path(job["files"][0]["path"])
        saved.write_bytes(b"replace this file")
        overwritten = self.run_download("sample.wav", "--on-duplicate", "overwrite")
        self.assertEqual(overwritten.returncode, 0, overwritten.stderr)
        self.assertGreater(saved.stat().st_size, len(b"replace this file"))
        self.assertEqual(json.loads(self.cli("jobs", "2", "--json").stdout)["status"], "succeeded")

    @unittest.skipUnless(hasattr(os, "openpty"), "requires a pseudo-terminal")
    def test_first_launch_saves_default_for_later_downloads(self):
        destination = self.root / "chosen downloads"
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        master, slave = os.openpty()
        process = None
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", env=self.environment)
            os.close(slave)
            slave = None
            os.write(master, f"n\n{destination}\n".encode())
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("Is this folder right?", stdout)
            self.assertIn("Download folder saved:", stdout)
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
        configuration = json.loads(Path(self.environment["FETCHLANE_CONFIG"]).read_text(encoding="utf-8"))
        self.assertEqual(configuration["download_directory"], str(destination))
        result = self.cli("audio", f"{self.url}/sample.wav", "--format", "mp3")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(self.cli("jobs", "1", "--json").stdout)
        self.assertEqual(job["options"]["output"], str(destination))
        self.assertTrue(Path(job["files"][0]["path"]).is_file())

    def test_saved_concurrency_runs_two_downloads_at_a_time(self):
        for index in range(4):
            (self.root / f"timed-{index}.wav").write_bytes((self.root / "sample.wav").read_bytes())
        links = self.root / "parallel.txt"
        links.write_text("\n".join(f"{self.url}/timed-{index}.wav" for index in range(4)), encoding="utf-8")
        configured = self.cli("settings", "--downloads", str(self.root / "downloads"),
                              "--max-concurrent", "2", "--audio-format", "mp3")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        process = subprocess.Popen([*command, "batch", str(links), "--type", "audio"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", env=self.environment)
        peak = 0
        try:
            deadline = time.monotonic() + 60
            while process.poll() is None and time.monotonic() < deadline:
                if self.database.exists():
                    with contextlib.closing(sqlite3.connect(self.database)) as connection:
                        try:
                            count = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
                            peak = max(peak, count)
                        except sqlite3.OperationalError:
                            pass  # The schema may still be initializing.
                time.sleep(0.05)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(peak, 2)
            self.assertGreaterEqual(self.server.peak_requests, 2)
            self.assertIn("4 succeeded", stdout)
            history = json.loads(self.cli("jobs", "--json").stdout)
            self.assertEqual(len(history), 4)
            self.assertTrue(all(job["status"] == "succeeded" for job in history))
            self.assertGreaterEqual(len({job["pid"] for job in history}), 2)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=30)

    def test_default_video_container_is_used_and_remembered_for_retry(self):
        video = self.root / "video.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=size=16x16:rate=1", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                        "-t", "1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)],
                       check=True, capture_output=True, timeout=20)
        configured = self.cli("settings", "--downloads", str(self.root / "downloads"), "--video-format", "mkv")
        self.assertEqual(configured.returncode, 0, configured.stderr)
        downloaded = self.cli("video", f"{self.url}/video.mp4")
        self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
        job = json.loads(self.cli("jobs", "1", "--json").stdout)
        self.assertEqual(job["options"]["format"], "mkv")
        self.assertEqual(Path(job["files"][0]["path"]).suffix, ".mkv")
        self.assertEqual(self.cli("settings", "--video-format", "mp4").returncode, 0)
        retry = self.cli("jobs", "retry", "1", "--on-duplicate", "overwrite")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        replacement = json.loads(self.cli("jobs", "2", "--json").stdout)
        self.assertEqual(replacement["options"]["format"], "mkv")

    def test_parallel_job_stop_does_not_cancel_other_downloads(self):
        for index in range(3):
            (self.root / f"timed-{index}.wav").write_bytes((self.root / "sample.wav").read_bytes())
        links = self.root / "parallel-stop.txt"
        links.write_text(f"{self.url}/slow.wav\n{self.url}/timed-1.wav\n{self.url}/timed-2.wav\n", encoding="utf-8")
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        process = subprocess.Popen([*command, "batch", str(links), "--type", "audio", "--format", "mp3",
                                    "--max-concurrent", "2", "--output", str(self.root / "downloads")],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", env=self.environment)
        try:
            deadline = time.monotonic() + 30
            job_id = None
            while time.monotonic() < deadline and process.poll() is None:
                if self.database.exists():
                    with contextlib.closing(sqlite3.connect(self.database)) as connection:
                        try:
                            rows = connection.execute("SELECT id, pid FROM jobs WHERE status = 'running' ORDER BY id").fetchall()
                        except sqlite3.OperationalError:
                            rows = []
                    if len(rows) == 2 and all(pid != process.pid for _, pid in rows):
                        job_id = rows[0][0]
                        break
                time.sleep(0.05)
            self.assertIsNotNone(job_id, "Two workers never started")
            stop = self.cli("jobs", "stop", str(job_id))
            self.assertEqual(stop.returncode, 0, stop.stderr)
            stdout, stderr = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 1, stdout + stderr)
            records = json.loads(self.cli("jobs", "--json").stdout)
            self.assertEqual([job["status"] for job in records].count("stopped"), 1)
            self.assertEqual([job["status"] for job in records].count("succeeded"), 2)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=30)

    def test_parallel_duplicate_waits_for_first_download_then_skips(self):
        links = self.root / "parallel-duplicates.txt"
        links.write_text(f"{self.url}/sample.wav\n{self.url}/sample.wav\n", encoding="utf-8")
        result = self.cli("batch", str(links), "--type", "audio", "--max-concurrent", "2",
                          "--on-duplicate", "skip", "--output", str(self.root / "downloads"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 succeeded, 0 failed, 1 skipped", result.stdout)
        self.assertEqual(len(json.loads(self.cli("jobs", "--json").stdout)), 1)

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery; Windows uses console events")
    def test_ctrl_c_stops_all_parallel_workers(self):
        for index in range(4):
            (self.root / f"timed-{index}.wav").write_bytes((self.root / "sample.wav").read_bytes())
        links = self.root / "cancel.txt"
        links.write_text("\n".join(f"{self.url}/timed-{index}.wav" for index in range(4)), encoding="utf-8")
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        process = subprocess.Popen([*command, "batch", str(links), "--type", "audio", "--max-concurrent", "2",
                                    "--output", str(self.root / "downloads")], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", env=self.environment)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                if self.database.exists():
                    with contextlib.closing(sqlite3.connect(self.database)) as connection:
                        try:
                            count = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
                        except sqlite3.OperationalError:
                            count = 0
                    if count == 2 and self.server.peak_requests >= 2:
                        break
                time.sleep(0.05)
            else:
                self.fail("Two workers never became active")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            records = json.loads(self.cli("jobs", "--json").stdout)
            self.assertTrue(all(job["status"] not in ("running", "stopping") for job in records))
            self.assertLessEqual(len(records), 2)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=30)

    @unittest.skipUnless(hasattr(os, "openpty"), "requires a pseudo-terminal")
    def test_interactive_change_location_prompt(self):
        first = self.run_download("sample.wav")
        self.assertEqual(first.returncode, 0, first.stderr)
        destination = self.root / "another folder"
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        master, slave = os.openpty()
        process = None
        try:
            process = subprocess.Popen(
                [*command, "audio", f"{self.url}/sample.wav", "--format", "mp3",
                 "--output", str(self.root / "downloads")],
                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                env=self.environment,
            )
            os.close(slave)
            slave = None
            os.write(master, f"l\n{destination}\n".encode())
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertIn("[o] Overwrite", stdout)
            self.assertIn("New output directory:", stdout)
            job = json.loads(self.cli("jobs", "2", "--json").stdout)
            self.assertEqual(job["options"]["output"], str(destination))
            self.assertTrue(Path(job["files"][0]["path"]).is_relative_to(destination))
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    def test_batch_continues_after_real_failure_and_records_saved_files(self):
        links = self.root / "links.txt"
        links.write_text(f"# Local queue\n{self.url}/missing.wav\n{self.url}/sample.wav\n", encoding="utf-8")
        binary = os.environ.get("FETCHLANE_TEST_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "fetchlane"]
        environment = self.environment
        result = subprocess.run([*command, "batch", str(links), "--type", "audio", "--format", "mp3",
                                 "--output", str(self.root / "downloads")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60, env=environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("1 succeeded, 1 failed", result.stdout)
        history = subprocess.run([*command, "jobs", "2", "--json"], capture_output=True, text=True, encoding="utf-8",
                                 check=True, timeout=30, env=environment)
        job = json.loads(history.stdout)
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(Path(job["files"][0]["path"]).is_file())
        self.assertEqual(job["options"]["batch_position"], 2)
