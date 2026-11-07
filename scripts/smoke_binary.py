"""Verify the executable starts and can invoke its bundled extractor registry."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile


def check_impersonation_targets(output: str) -> None:
    # yt-dlp also prints placeholder rows for missing backends. Those must not pass.
    if not any("chrome" in line.lower() and "curl_cffi" in line.lower()
               and "unavailable" not in line.lower() for line in output.splitlines()):
        raise RuntimeError("Bundled yt-dlp has no available Chrome impersonation target:\n" + output)


def main() -> None:
    executable = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="fetchlane-smoke-") as temporary:
        root = Path(temporary)
        destination = root / "Downloads with spaces 日本語"
        configuration = root / "preferences" / "config.json"
        environment = {**os.environ, "FETCHLANE_CONFIG": str(configuration),
                       "FETCHLANE_DB": str(root / "jobs.sqlite3")}

        def invoke(*arguments):
            try:
                return subprocess.run([executable, *arguments], check=True, env=environment,
                                      timeout=120, capture_output=True, text=True, encoding="utf-8")
            except subprocess.CalledProcessError as error:
                raise RuntimeError(f"{error}\n{error.stderr or error.stdout}") from error

        print(invoke("--version").stdout.strip())
        invoke("video", "https://example.com/video", "--dry-run")
        # Real downloader invocations that don't depend on a public site or network.
        result = invoke("info", "https://example.com/video", "--", "--list-extractors")
        if "youtube" not in result.stdout.lower():
            raise RuntimeError("Bundled yt-dlp did not list its YouTube extractor")
        targets = invoke("info", "https://example.com/video", "--impersonate", "chrome",
                         "--", "--list-impersonate-targets", "--color", "never")
        check_impersonation_targets(targets.stdout)
        # Check configuration persistence and Unicode paths on every OS in the matrix.
        invoke("settings", "--downloads", str(destination), "--max-concurrent", "2",
               "--audio-format", "mp3", "--video-format", "mkv")
        saved = json.loads(configuration.read_text(encoding="utf-8"))
        if Path(saved["download_directory"]) != destination.resolve():
            raise RuntimeError("Binary did not save its configured download directory")
        if (saved["max_concurrent_downloads"], saved["audio_format"], saved["video_format"]) != (2, "mp3", "mkv"):
            raise RuntimeError("Binary did not persist its download defaults")
        preview = invoke("video", "https://example.com/video", "--dry-run")
        if str(destination.resolve()) not in preview.stdout:
            raise RuntimeError("Binary did not reuse its saved download directory")
        if "--remux-video mkv" not in preview.stdout:
            raise RuntimeError("Binary did not use its default video format")
        audio = invoke("audio", "https://example.com/video", "--dry-run")
        if "--audio-format mp3" not in audio.stdout:
            raise RuntimeError("Binary did not use its default audio format")
        links = root / "links.txt"
        links.write_text("https://example.com/one\nhttps://example.com/two\n", encoding="utf-8")
        # Listing extractors exercises worker startup without network or ffmpeg.
        invoke("batch", str(links), "--", "--list-extractors")
        records = json.loads(invoke("jobs", "--json").stdout)
        if len(records) != 2 or any(job["status"] != "succeeded" for job in records):
            raise RuntimeError("Binary parallel workers did not complete their jobs")
    print("Standalone executable smoke checks passed.")


if __name__ == "__main__":
    main()
