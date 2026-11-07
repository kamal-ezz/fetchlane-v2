# fetchlane

A simpler CLI for yt-dlp. Download videos and audio, queue links, and manage jobs.
Works on Linux, macOS, and Windows.

The standalone executable bundles Python, yt-dlp, and browser impersonation
support. Install **Node.js** and **ffmpeg/ffprobe** separately and put them on
your PATH.

## Download

Replace `URL` with a media link:

```sh
fetchlane video "URL"
fetchlane video "URL" --quality 1080
fetchlane audio "URL" --format mp3
fetchlane playlist "URL"
fetchlane info "URL"
```

On first launch, fetchlane asks you to confirm or change your Downloads folder.
Use `--output DIR` to choose a different destination for one download.

## Settings

```sh
fetchlane settings
fetchlane settings --downloads ~/Media
fetchlane settings --max-concurrent 3 --audio-format mp3 --video-format mp4
```

Defaults are the system Downloads folder, one download at a time, and `best`
formats. Concurrency supports 1–16 jobs per batch or multi-URL command.
Command options override saved settings.

## Batch queue

Put one URL per line in `links.txt`. Blank lines and `#` comments are ignored.

```sh
fetchlane batch links.txt
fetchlane batch links.txt --type audio --max-concurrent 3
```

Failed jobs don't stop the rest. Ctrl+C stops the whole queue.
Existing downloads prompt you to **overwrite**, **change location**, or **skip**.
For unattended runs, add `--on-duplicate skip` or `--on-duplicate overwrite`.

## Jobs

```sh
fetchlane jobs
fetchlane jobs 12
fetchlane jobs stop 12
fetchlane jobs retry 12
fetchlane jobs remove 12
```

History is saved in SQLite. Removing a job keeps its files; stopping one job
leaves the others running.

## More options

```sh
fetchlane video --help
fetchlane video "URL" --dry-run
fetchlane video "URL" --impersonate chrome
fetchlane video "URL" -- --cookies-from-browser firefox
```

Use `--verbose` for full logs. Flags after `--` go directly to yt-dlp.

## Build from source

Requires Python 3.11+. Run these inside a virtual environment:

```sh
python -m pip install -e ".[build]"
python -m unittest discover -s tests -v
python scripts/build_binary.py
python scripts/smoke_binary.py dist/fetchlane
```

The executable is `dist/fetchlane` (`dist/fetchlane.exe` on Windows; use that
path for the smoke check). Build separately for each OS and CPU architecture.
Rebuild to update the bundled yt-dlp version.
