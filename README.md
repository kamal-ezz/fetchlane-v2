# fetchlane

A command-based Python wrapper for yt-dlp. No TUI, menus, or GUI.

## Standalone executable

The standalone build bundles Python, yt-dlp, and curl_cffi (including its native
browser impersonation library) into one executable. Users do
not need Python, pip, or a separate yt-dlp installation. Build on each target
OS; executables are specific to its OS and CPU architecture.

To build from source (Python is needed on the build machine only):

```sh
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install '.[build]'
python scripts/build_binary.py
python scripts/smoke_binary.py dist/fetchlane
./dist/fetchlane video 'https://www.youtube.com/watch?v=VIDEO_ID'
```

On Windows the output is `dist/fetchlane.exe`; use that path for the smoke
check. Copy the executable onto your PATH for convenient use. The GitHub Actions
`Standalone binaries` workflow builds and checks Linux x64, Windows x64, and
macOS arm64 and Intel x64 artifacts on pushes, pull requests, and manual runs. Every platform
runs unit tests plus executable checks for saved preferences, Unicode paths,
and browser impersonation. These are
unsigned build artifacts, not published releases. Linux builds require glibc
compatible with Ubuntu 22.04 or newer. macOS and Windows may require approval
to run an unsigned downloaded executable.

**Media tools remain external:** install ffmpeg and ffprobe for audio extraction
and video/audio merging. **Node.js is the default JavaScript runtime**; install
Node.js and make sure `node` is on your PATH for sites such as YouTube.
The executable bundles yt-dlp's Python dependencies and EJS
scripts, but does not bundle these external programs. See the
[upstream dependencies](https://github.com/yt-dlp/yt-dlp#dependencies).

To select another runtime explicitly, append
`-- --no-js-runtimes --js-runtimes deno`. To specify a Node.js executable,
append `-- --js-runtimes node:/path/to/node`.

Update a standalone installation by replacing it with a newly built executable;
upgrading a separate Python package will not update the bundled downloader.

### Browser impersonation

Browser impersonation support is included in every standalone build using
[yt-dlp's curl-cffi extra](https://github.com/yt-dlp/yt-dlp#impersonation).
No installed browser or separate curl program is needed for impersonation.
Let yt-dlp select it when a site requires it, or choose a target explicitly:

```sh
fetchlane video 'https://example.com/video' --impersonate chrome
fetchlane info 'https://example.com/video' -- --list-impersonate-targets
```

The smoke check requires an available Chrome target from the bundled backend,
so a build with a missing native library or incompatible backend fails the check.
Impersonation is available by default but is not forced for every request;
[upstream notes](https://github.com/yt-dlp/yt-dlp#network-options) that forcing it
can affect speed and stability. Available browser versions depend on the bundled
curl_cffi version. This emulates browser network fingerprints; it does not run
a browser or supply login cookies.

## Python installation (alternative)

Requires Python 3.11 or newer. From this directory:

```sh
python -m pip install .
fetchlane --help
```

This installs yt-dlp, its default Python dependencies, and curl_cffi. Install the **ffmpeg
and ffprobe executables** using your system package manager for audio extraction
and merging high-quality video with audio. Install Node.js for JavaScript
execution on sites such as YouTube; see [yt-dlp's installation guide](https://github.com/yt-dlp/yt-dlp#dependencies).

## Settings and download folder

Use `settings` to inspect or update persistent defaults:

```sh
fetchlane settings
fetchlane settings --downloads ~/Media --max-concurrent 3 --audio-format mp3 --video-format mp4
```

| Setting | Initial default | Values |
| --- | --- | --- |
| Downloads location | System Downloads folder | A directory |
| Max concurrent downloads | 1 | 1–16 per batch or multi-URL command |
| Audio format | `best` | `best`, `mp3`, `m4a`, `opus`, `flac`, `wav` |
| Video container | `best` | `best`, `mp4`, `mkv`, `webm` |

Settings updates preserve your other preferences. Legacy folder-only config
files still work, and `config --output DIR` remains an alias for changing the
download folder. Command options override saved settings for one invocation:

```sh
fetchlane batch links.txt --max-concurrent 2
fetchlane audio 'URL' --format opus
fetchlane video 'URL' --format mkv
```

Audio formats may require conversion with ffmpeg. Video container selection
does not re-encode: MP4/WebM select compatible available formats, and MKV can
remux the best available streams. If a requested container has no compatible
source format, the job fails with an error. `--quality` separately limits video
height. Retries retain the format recorded by the original job.

Downloads default to the current user's system Downloads folder:

- Windows: the configured Downloads location, including redirected folders,
  obtained using the [Windows Known Folder API](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath).
- macOS: `~/Downloads`.
- Linux: the configured or localized Downloads location from
  [XDG user directories](https://wiki.freedesktop.org/www/Software/xdg-user-dirs/), falling back to `~/Downloads`.

If system folder discovery is unavailable, `~/Downloads` is used. On your first
interactive launch (`fetchlane` with no arguments), fetchlane asks whether this
folder is right and lets you enter another directory. It saves the choice for
later runs. Starting a download without `--output` also offers this setup when
no preference has been saved.

```sh
fetchlane settings                        # Show your preferences
fetchlane settings --downloads ~/Media    # Change and save the default
fetchlane video 'URL' --output ~/Videos   # Override it for this download only
```

Help, version, metadata, and job management commands do not trigger setup.
Explicit `--output` also bypasses setup without changing the saved default.
Scripts without a terminal and dry runs use the system Downloads folder until configured;
they do not prompt or save a confirmation. You can configure scripts in advance
with `fetchlane settings --downloads DIR`. Retries use the original job's directory,
and choosing another location at a duplicate prompt only affects that download.

Preferences are saved atomically in:

- Linux: `$XDG_CONFIG_HOME/fetchlane/config.json`, or `~/.config/fetchlane/config.json`
- macOS: `~/Library/Application Support/fetchlane/config.json`
- Windows: `%APPDATA%/fetchlane/config.json`

Set `FETCHLANE_CONFIG` to use another configuration file. Missing destination
directories are created when yt-dlp downloads media. Configuration is separate
from SQLite job history.

## Commands

Run `fetchlane` with no arguments for a quick guide and examples. Use
`fetchlane video --help` (or another command) to see its options.

```sh
fetchlane video 'https://www.youtube.com/watch?v=VIDEO_ID'
fetchlane video 'https://www.youtube.com/watch?v=VIDEO_ID' --quality 1080 --output ~/Videos
fetchlane audio 'https://www.youtube.com/watch?v=VIDEO_ID' --format mp3
fetchlane playlist 'https://www.youtube.com/playlist?list=PLAYLIST_ID' --items 1:10
fetchlane info 'https://www.youtube.com/watch?v=VIDEO_ID'
fetchlane info 'https://www.youtube.com/watch?v=VIDEO_ID' --json
```

Downloads show a short status, live percentage/speed/ETA, and the final saved
path after processing. Routine extractor and ffmpeg logs are hidden; warnings
and errors remain visible. Add `--verbose` for full yt-dlp diagnostic logs.

All commands accept multiple URLs. Quote URLs to protect shell characters such
as `&`. Video and audio commands download only the individual video, even if its
URL includes a playlist. Use `playlist` to download the entire collection.

Downloads go to your saved folder (initially the system Downloads folder) unless `--output DIR` is supplied. Video
uses the best available quality by default; `--quality 1080` sets a strict maximum
height and fails if no suitable format exists. Audio uses your saved format,
initially the best available audio; choosing another format may transcode it. Playlist
downloads get their own folder with numbered filenames. IDs in filenames avoid
collisions between videos with identical titles. Existing files and partial
downloads follow yt-dlp's normal skip/resume behavior.

## Batch queue from a text file

Create `links.txt` with one full URL per line, without shell quotes:

```text
# Downloads for later
https://www.youtube.com/watch?v=FIRST_VIDEO_ID
https://www.youtube.com/watch?v=SECOND_VIDEO_ID
```

```sh
fetchlane batch links.txt                              # Videos, best quality
fetchlane batch links.txt --quality 1080 --output ~/Videos
fetchlane batch links.txt --type audio --format mp3
fetchlane batch playlists.txt --type playlist --items 1:10
fetchlane batch links.txt --dry-run                     # Preview every command
```

The queue runs sequentially by default. Increase `--max-concurrent` or the saved
setting to run up to 16 link jobs simultaneously. This limit applies to one
foreground invocation; separate invocations have separate limits. A single
playlist's entries still follow yt-dlp's normal sequential behavior. Worker
output is labelled with job IDs, while duplicate prompts stay in the parent
terminal. Repeated links wait for their matching active job, then offer the
normal duplicate choices. Defaults are captured at the start of the command;
editing settings affects subsequent commands, not active workers.

The queue shows `[1/10]` progress and final success/failure/skipped/stopped
counts. Blank lines and lines starting with `#` are ignored;
repeated links remain in file order and are checked for duplicates. UTF-8 files with a BOM and Windows line endings
are supported. Files are validated completely before starting any jobs; invalid
links report the file and line number. Limits are 10,000 links and 8,192
characters per line.

Each attempted link gets its own SQLite job, including the source file and
position in the batch. Failed downloads do not stop later links. The command
exits 1 if any link fails or is stopped, or 0 if all succeed or are skipped.
Ctrl+C stops the whole queue, cancels active workers, and returns
130; links not yet attempted are not added to history. The queue runs in the
foreground and does not automatically resume after exiting. Rerunning the file
prompts for matching successful jobs whose files still exist, and creates new
jobs for the remaining links. Use `--on-duplicate skip` for unattended batches
that should skip existing downloads. Existing media follows the normal
skip/resume behavior unless overwrite is selected.
All download options apply to every link. Advanced yt-dlp options after `--`
are forwarded to each download as well.

## Job history

Each new download creates a job with a printed ID. Multiple URL arguments and
batch queues create one per link; detected duplicates reuse the existing history.
Jobs record the command, URLs, selected wrapper options, process ID, UTC
timestamps, exit code, outcome, and final media paths. `info` and `--dry-run`
do not create jobs.

```sh
fetchlane jobs                       # Most recent 20 jobs
fetchlane jobs --status failed       # Filter by outcome
fetchlane jobs --limit 50            # Up to 200 recent jobs
fetchlane jobs 12                    # Details and saved files
fetchlane jobs 12 --json             # Machine-readable details
fetchlane jobs stop 12               # Request cancellation from another terminal
fetchlane jobs remove 12             # Remove history; keep downloaded files
fetchlane jobs retry 12              # New job using saved links and wrapper options
fetchlane jobs retry 12 --dry-run    # Preview a retry
```

Stop requests are sent through SQLite to the owning process, which cancels its
download at the next cancellation point. No stored PID is signalled. Status
changes to `stopping`, then `stopped`. Stopping a job leaves other jobs in its
batch running; Ctrl+C in the batch terminal stops the whole queue. Partial
media remains available for retries.
The original job remains in history when retried; the new job records `retry_of`.
Advanced yt-dlp arguments are not saved: supply those again after `--` on the
retry command, including cookies or custom output flags when needed.

Active jobs cannot be removed or retried. After a crash, use
`fetchlane jobs remove ID --force` once its heartbeat has been absent for 60
seconds; legacy rows without a heartbeat can also be removed this way. Remove
only deletes database records and never deletes media or partial files.

History is stored in SQLite using Python's standard library; no database server
is needed. The default path is:

- Linux: `$XDG_STATE_HOME/fetchlane/jobs.sqlite3`, or `~/.local/state/fetchlane/jobs.sqlite3`
- macOS: `~/Library/Application Support/fetchlane/jobs.sqlite3`
- Windows: `%LOCALAPPDATA%/fetchlane/jobs.sqlite3`

Set `FETCHLANE_DB` to use a different database file. Multiple invocations can
share the database; no database transaction stays open during a download.
Raw yt-dlp arguments are not stored, since they can contain credentials. URLs
and paths are stored locally, so treat the database as private history.

Jobs run in the foreground. Status changes from `running` to `succeeded`,
`failed`, `interrupted`, or `stopped` when the command exits. `succeeded` means yt-dlp
returned zero; explicit advanced simulation or filtering options may produce
no files. Final file paths are collected after media processing and committed
when the invocation ends, including completed files from partially failed
playlists. Ctrl+C is recorded as interrupted. An abrupt kill or machine crash
can leave a `running` row with no final outcome; it does not guarantee the
process is still alive. This is history tracking, not a background queue or
automatic retry system. Tracking is required for downloads; database access
errors stop the command with an actionable error instead of silently losing
history.

## Duplicate detection

Matching successful audio/video requests prompt when **all recorded files
still exist**:

```text
Already downloaded by job #12:
  /path/to/song.mp3
[o] Overwrite  [l] Change location  [s] Skip:
```

Overwrite runs a new job with yt-dlp's `--force-overwrites` flag. Change location
asks for a directory, then checks that destination again before downloading.
Skip keeps the existing file and creates no new job. Each duplicate in a batch
gets its own choice. Ctrl+C or EOF at the prompt cancels the current command.

Removed files are downloaded again. Matches include the media
URL, audio format or video quality, destination directory, and advanced options.
YouTube watch, short-link, shorts, embed, and live URLs for the same video are
treated as equivalent when no advanced options are supplied. With advanced
options, the full URL is retained because timestamps can affect downloaded clips.
This detects matching requests, not identical file contents across unrelated sites.

```sh
fetchlane audio 'URL' --format mp3 --on-duplicate overwrite
fetchlane batch links.txt --type audio --on-duplicate skip
fetchlane jobs retry 12 --on-duplicate overwrite
```

Without a terminal, the default `ask` policy reports an error for a duplicate
and requires `--on-duplicate overwrite` or `--on-duplicate skip`; use `--output`
to select another directory. It never reads piped input as a prompt response.
The legacy `--allow-duplicates` flag still bypasses history checks using yt-dlp's
normal existing-file behavior; prefer `--on-duplicate` for an explicit choice.
Matching active requests are refused regardless of policy, so two copies of
the same request cannot start together. Playlist contents
are always refreshed to discover new entries, with existing files handled by
yt-dlp. History created before duplicate detection lacks request fingerprints
and is not used for these checks. Existing databases are upgraded automatically
without deleting their history.

## Preview and advanced options

```sh
fetchlane video 'https://example.com/video' --quality 720 --dry-run
fetchlane video 'https://example.com/video' -- --cookies-from-browser firefox
fetchlane audio 'https://example.com/video' --format mp3 -- --audio-quality 0
```

`--dry-run` prints a shell-quoted yt-dlp command without requiring yt-dlp or
accessing the network. It is displayed in POSIX shell syntax. Options after `--`
are passed straight to yt-dlp and can override wrapper defaults. Put URLs and
fetchlane options before this separator. Raw options retain all yt-dlp powers,
including options that execute programs.

fetchlane ignores existing yt-dlp configuration files for predictable defaults.
Load one explicitly with `-- --config-locations PATH` if needed. Progress uses
fetchlane's concise templates. Detailed download errors come from yt-dlp. Its exit status is
preserved; interruption returns 130 and a missing yt-dlp installation in source
mode returns 127. For Python installations, update the downloader with
`python -m pip install -U 'yt-dlp[default,curl-cffi]'`.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python -m fetchlane --help
```
