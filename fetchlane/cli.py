"""Translate a small command vocabulary into yt-dlp arguments."""

import argparse
import importlib.util
import io
from pathlib import Path
import shlex
import signal
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit

from . import __version__
from . import jobs
from . import batch
from . import duplicates
from . import config
from .identity import request_key


def media_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname
                 and not any(char.isspace() or ord(char) < 32 for char in value))
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("use a full http:// or https:// URL")
    return value


def quality(value: str) -> str:
    if value != "best" and (not value.isascii() or not value.isdecimal() or int(value) < 1):
        raise argparse.ArgumentTypeError("quality must be 'best' or a positive height, such as 1080")
    return value


def history_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= 200:
        raise argparse.ArgumentTypeError("limit must be between 1 and 200")
    return limit


def concurrency(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if not 1 <= limit <= 16:
        raise argparse.ArgumentTypeError("concurrency must be between 1 and 16")
    return limit


def apply_defaults(args: argparse.Namespace) -> dict:
    settings = config.load_settings()
    mode = getattr(args, "media_type", args.command)
    if args.format is None:
        args.format = settings["audio_format" if mode == "audio" else "video_format"]
    if args.max_concurrent is None:
        args.max_concurrent = settings["max_concurrent_downloads"]
    return settings


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetchlane", description="Download video and audio with simple yt-dlp commands.",
        usage="%(prog)s COMMAND [ARGUMENTS] [OPTIONS]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples (replace URL with a quoted video or playlist link):
  fetchlane video "URL"                    Download a video at best quality
  fetchlane video "URL" --quality 1080     Limit video height to 1080p
  fetchlane audio "URL" --format mp3       Save audio as MP3
  fetchlane playlist "URL"                Download an entire playlist
  fetchlane info "URL"                    Show details without downloading
  fetchlane jobs                          List recent download jobs
  fetchlane jobs 12                       Show job #12 and its saved files
  fetchlane jobs stop 12                  Stop a running job
  fetchlane jobs retry 12                 Retry a job with its saved options
  fetchlane jobs remove 12                Remove a job from history
  fetchlane batch links.txt               Download links from a text file
  fetchlane batch links.txt --type audio  Download the queue as audio
  fetchlane settings                     Show download preferences
  fetchlane settings --max-concurrent 3  Allow three downloads at once

Downloads use your saved folder, or your system Downloads folder. Use --output DIR for one run.
For command options: fetchlane video --help
Advanced yt-dlp options: fetchlane video "URL" -- --cookies-from-browser firefox''',
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, title="commands", metavar="COMMAND")
    preferences = commands.add_parser("settings", aliases=["config"], help="Show or change download preferences", allow_abbrev=False)
    preferences.add_argument("--downloads", "--output", dest="output", type=Path, metavar="DIR", help="Default download directory")
    preferences.add_argument("--max-concurrent", type=concurrency, metavar="N", help="Maximum simultaneous downloads per batch (1–16)")
    preferences.add_argument("--audio-format", choices=config.AUDIO_FORMATS, help="Default audio format")
    preferences.add_argument("--video-format", choices=config.VIDEO_FORMATS, help="Default video container")
    history = commands.add_parser("jobs", help="List download jobs or inspect a job by ID",
                                  description="List, inspect, stop, remove, or retry download jobs.",
                                  usage="%(prog)s [ID | stop ID | remove ID | retry ID] [OPTIONS]",
                                  epilog="Stop requests interrupt the owning command. Remove deletes history only. Retry starts a new job.",
                                  allow_abbrev=False)
    history.add_argument("target", nargs="?", metavar="ID|ACTION", help="Job ID, or stop, remove, retry")
    history.add_argument("action_id", nargs="?", type=int, metavar="ID", help="Job ID for an action")
    history.add_argument("--limit", type=history_limit, default=20, help="Number of recent jobs (1–200; default: 20)")
    history.add_argument("--status", choices=("running", "stopping", "stopped", "succeeded", "failed", "interrupted"),
                         help="Filter the recent jobs list")
    history.add_argument("--json", action="store_true", help="Print job data as JSON")
    history.add_argument("--force", action="store_true", help="Remove a stale active job after 60 seconds without a heartbeat")
    history.add_argument("--allow-duplicates", action="store_true", help="Retry even if matching saved files exist")
    history.add_argument("--dry-run", action="store_true", help="Preview a retry without creating jobs")
    history.add_argument("--on-duplicate", choices=("ask", "overwrite", "skip"), default="ask",
                         help="Duplicate handling for retries (default: ask in a terminal)")
    for name, help_text in (
        ("video", "Download a single video"),
        ("audio", "Download audio from a single video"),
        ("playlist", "Download every video in a playlist"),
        ("info", "Show title, creator, duration, and URL without downloading"),
        ("batch", "Download links from a text file, one at a time"),
    ):
        command = commands.add_parser(
            name, help=help_text, description=help_text + ".",
            usage="%(prog)s FILE [OPTIONS]" if name == "batch" else "%(prog)s URL [URL ...] [OPTIONS]",
            epilog='Example: fetchlane batch links.txt --type audio --format mp3' if name == "batch"
            else f'Example: fetchlane {name} "https://example.com/video"',
            allow_abbrev=False,
        )
        if name == "batch":
            command.add_argument("file", type=Path, metavar="FILE", help="UTF-8 text file with one URL per line")
            command.add_argument("--type", dest="media_type", choices=("video", "audio", "playlist"),
                                 default="video", help="What to download for each link (default: video)")
        else:
            command.add_argument("urls", metavar="URL", nargs="+", type=media_url,
                                 help="One or more full video or playlist links (quote each link)")
        command.add_argument("--dry-run", action="store_true", help="Print the command without running it")
        command.add_argument("--verbose", action="store_true", help="Show full yt-dlp logs for troubleshooting")
        command.add_argument("--impersonate", metavar="BROWSER",
                             help="Impersonate a browser, e.g. chrome or safari (default: site-selected)")
        if name == "info":
            command.add_argument("--json", action="store_true", help="Print complete metadata as JSON")
            continue
        command.add_argument("--allow-duplicates", action="store_true", help="Run again even if matching saved files exist")
        command.add_argument("--on-duplicate", choices=("ask", "overwrite", "skip"), default="ask",
                             help="Choose what to do with existing downloads (default: ask in a terminal)")
        command.add_argument("-o", "--output", type=Path, metavar="DIR",
                             help="Destination directory (default: saved folder or system Downloads folder)")
        formats = config.AUDIO_FORMATS if name == "audio" else config.VIDEO_FORMATS
        if name == "batch":
            formats = tuple(dict.fromkeys((*config.AUDIO_FORMATS, *config.VIDEO_FORMATS)))
        command.add_argument("--format", choices=formats, help="Output format (default: saved setting or best available)")
        command.add_argument("--max-concurrent", type=concurrency, metavar="N",
                             help="Simultaneous downloads for multiple links (default: saved setting or 1)")
        if name != "audio":
            command.add_argument("-q", "--quality", type=quality, default="best", metavar="HEIGHT",
                                 help="Maximum video height, e.g. 1080 (default: best)")
        if name in ("playlist", "batch"):
            command.add_argument("--items", metavar="RANGE", help="Playlist selection, e.g. 1:10 or 1,3,5")
    return parser


def build_arguments(args: argparse.Namespace, extra: list[str], report: Path | None = None) -> list[str]:
    result = ["--ignore-config", "--no-js-runtimes", "--js-runtimes", "node"]
    if args.verbose:
        result += ["--verbose"]
    if getattr(args, "worker_output", False):
        result += ["--progress-delta", "1"]
    if args.impersonate is not None:
        result += ["--impersonate", args.impersonate]
    if args.command == "info":
        result += ["--no-playlist", "--simulate"]
        if args.json:
            result += ["--dump-json"]
        else:
            result += ["--print", "%(title)s\nCreator: %(uploader)s\nDuration: %(duration_string)s\nURL: %(webpage_url)s\n"]
    else:
        if not args.verbose:
            # Explicitly keep downloads enabled: early --print stages imply simulation.
            result += [
                "--quiet", "--progress", "--no-simulate",
                "--print", "before_dl:Downloading: %(title)s",
                "--progress-template", "download:  %(progress._percent_str)s | %(progress._speed_str)s | ETA %(progress._eta_str)s",
                "--print", "post_process:Processing media...",
                "--print", "after_move:Saved: %(filepath)s",
            ]
        result += ["--yes-playlist" if args.command == "playlist" else "--no-playlist"]
        output = args.output if args.output is not None else config.default_directory()
        result += ["--paths", str(output.expanduser().absolute())]
        template = "%(title)s [%(id)s].%(ext)s"
        if args.command == "playlist":
            template = "%(playlist_title)s/%(playlist_index)03d - " + template
            if args.items:
                result += ["--playlist-items", args.items]
        result += ["--output", template]
        if args.command == "audio":
            audio_format = args.format or config.load_settings()["audio_format"]
            result += ["--extract-audio", "--audio-format", audio_format]
        else:
            video_format = args.format or config.load_settings()["video_format"]
            height = "" if args.quality == "best" else f"[height<={args.quality}]"
            extension = f"[ext={video_format}]" if video_format in ("mp4", "webm") else ""
            audio_extension = "[ext=m4a]" if video_format == "mp4" else "[ext=webm]" if video_format == "webm" else ""
            result += ["--format", f"bv*{height}{extension}+ba{audio_extension}/b{height}{extension}"]
            if video_format != "best":
                result += ["--merge-output-format", video_format, "--remux-video", video_format]
    if report is not None:
        result += ["--print-to-file",
                   'after_move:{"title":%(title)j,"path":%(filepath)j,"url":%(webpage_url)j}',
                   str(report)]
    # Raw options intentionally come last so advanced users can override defaults.
    overwrite = getattr(args, "overwrite", False) or getattr(args, "on_duplicate", "ask") == "overwrite"
    return result + extra + (["--force-overwrites"] if overwrite else []) + ["--", *args.urls]


def main(argv: list[str] | None = None) -> int:
    # Windows pipes otherwise use the legacy code page and can corrupt Unicode paths.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--internal-worker"]:
        from .workers import worker_main
        return worker_main()
    parser = create_parser()
    if not argv:
        if sys.stdin.isatty():
            try:
                if config.load_directory() is None:
                    config.setup()
            except (EOFError, KeyboardInterrupt):
                print("\nSetup cancelled. No download folder was saved.", file=sys.stderr)
                return 130
            except (OSError, ValueError) as error:
                print(f"fetchlane: {error}", file=sys.stderr)
                return 1
        parser.print_help()
        return 0
    extra: list[str] = []
    if "--" in argv:
        boundary = argv.index("--")
        extra, argv = argv[boundary + 1:], argv[:boundary]
    args = parser.parse_args(argv)
    if args.command in ("settings", "config"):
        if extra:
            parser.error("config does not accept yt-dlp options")
        try:
            changes = {}
            if args.output is not None:
                changes["download_directory"] = str(config.directory(args.output))
            for key, attribute in (("max_concurrent_downloads", "max_concurrent"),
                                   ("audio_format", "audio_format"), ("video_format", "video_format")):
                if getattr(args, attribute) is not None:
                    changes[key] = getattr(args, attribute)
            settings = config.save_settings(changes, repair=args.command == "config") if changes else config.load_settings()
            destination = settings.get("download_directory") or config.downloads_directory()
            print(f"Download folder: {destination}\nMax concurrent downloads: {settings['max_concurrent_downloads']}"
                  f"\nDefault audio format: {settings['audio_format']}\nDefault video format: {settings['video_format']}"
                  f"\nConfiguration: {config.config_path()}")
            return 0
        except (OSError, ValueError) as error:
            print(f"fetchlane: {error}", file=sys.stderr)
            return 1
    if args.command == "jobs":
        try:
            return handle_jobs(args, extra, parser)
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"fetchlane: {error}", file=sys.stderr)
            return 1
    if args.command == "batch":
        if args.items and args.media_type != "playlist":
            parser.error("--items requires --type playlist")
        allowed_formats = config.AUDIO_FORMATS if args.media_type == "audio" else config.VIDEO_FORMATS
        if args.format is not None and args.format not in allowed_formats:
            parser.error(f"--format for {args.media_type} must be one of: {', '.join(allowed_formats)}")
        if args.quality != "best" and args.media_type == "audio":
            parser.error("--quality applies to video and playlist downloads")
        try:
            urls = batch.read_urls(args.file, media_url)
        except (OSError, UnicodeError, ValueError) as error:
            parser.error(str(error))
        try:
            settings = apply_defaults(args)
            args.output = config.resolve_output(args.output, dry_run=args.dry_run, settings=settings)
            return batch.run(args, urls, extra, execute_media)
        except (EOFError, KeyboardInterrupt):
            print("\nBatch interrupted.", file=sys.stderr)
            return 130
        except (OSError, ValueError) as error:
            print(f"fetchlane: {error}", file=sys.stderr)
            return 1
    return execute_urls(args, extra)


def handle_jobs(args: argparse.Namespace, extra: list[str], parser: argparse.ArgumentParser) -> int:
    if args.target != "retry" and (args.dry_run or args.allow_duplicates or extra or args.on_duplicate != "ask"):
        parser.error("--dry-run, --allow-duplicates, --on-duplicate, and advanced options require jobs retry ID")
    if args.force and args.target != "remove":
        parser.error("--force requires jobs remove ID")
    if args.target in ("stop", "remove", "retry"):
        if args.action_id is None:
            parser.error(f"jobs {args.target} requires a job ID")
        if extra and args.target != "retry":
            parser.error("advanced yt-dlp options are only supported with jobs retry")
        if args.target == "stop":
            jobs.request_stop(args.action_id)
            print(f"Stop requested for job #{args.action_id}. Check with: fetchlane jobs {args.action_id}")
            return 0
        if args.target == "remove":
            jobs.remove(args.action_id, force=args.force)
            print(f"Removed job #{args.action_id} from history. Downloaded files were preserved.")
            return 0
        original = jobs.get(args.action_id)
        if original is None:
            raise ValueError(f"job #{args.action_id} was not found")
        if original["status"] in jobs.ACTIVE_STATUSES:
            raise ValueError(f"job #{args.action_id} is active; stop it before retrying")
        retry_argv = [original["command"], *original["urls"]]
        names = ["output", "impersonate"]
        names += ["format"] if original["command"] == "audio" else ["quality", "format"]
        if original["command"] == "playlist":
            names += ["items"]
        for name in names:
            if name in original["options"]:
                retry_argv += [f"--{name}", str(original["options"][name])]
        retry = parser.parse_args(retry_argv)
        if "format" not in original["options"]:
            retry.format = "best"
        retry.retry_of = original["id"]
        retry.allow_duplicates = args.allow_duplicates
        retry.dry_run = args.dry_run
        retry.on_duplicate = args.on_duplicate
        return execute_urls(retry, extra)
    if args.action_id is not None or extra:
        parser.error("use jobs ID, jobs stop ID, jobs remove ID, or jobs retry ID")
    if args.target is not None and not args.target.isdecimal():
        parser.error("expected a job ID or one of: stop, remove, retry")
    return jobs.display(int(args.target) if args.target is not None else None,
                        args.limit, args.status, args.json)


def execute_urls(args: argparse.Namespace, extra: list[str]) -> int:
    if args.command != "info":
        try:
            settings = apply_defaults(args)
            args.output = config.resolve_output(args.output, dry_run=args.dry_run, settings=settings)
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.", file=sys.stderr)
            return 130
        except (OSError, ValueError) as error:
            print(f"fetchlane: {error}", file=sys.stderr)
            return 1
    if args.command != "info" and len(args.urls) > 1:
        args.media_type = args.command
        return batch.run(args, args.urls, extra, execute_media)
    return execute_media(args, extra)


def execute_media(args: argparse.Namespace, extra: list[str]) -> int:
    args.skipped = False
    arguments = build_arguments(args, extra)
    if args.dry_run:
        print(shlex.join(["yt-dlp", *arguments]))
        return 0
    if args.command == "info":
        return run_downloader(arguments)
    try:
        job_id, code = prepare_job(args, extra)
        return run_prepared_job(job_id, args, extra) if job_id is not None else code
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        print(f"fetchlane: could not save job history: {error}", file=sys.stderr)
        return 1


def prepare_job(args: argparse.Namespace, extra: list[str]) -> tuple[int | None, int]:
    args.skipped = False
    args.job_id = None
    names = ["output", "impersonate", "format"]
    if args.command != "audio":
        names += ["quality"]
    if args.command == "playlist":
        names += ["items"]
    options = {key: str(getattr(args, key)) for key in names if getattr(args, key, None) is not None}
    if hasattr(args, "batch_position") and hasattr(args, "file"):
        options["batch_file"] = str(args.file.expanduser().absolute())
        options["batch_position"] = args.batch_position
    if hasattr(args, "retry_of"):
        options["retry_of"] = args.retry_of
    allow_duplicates = getattr(args, "allow_duplicates", False)
    while True:
        options["output"] = str(args.output.expanduser().resolve())
        key = request_key(args.command, args.urls, options, extra)
        try:
            job_id = jobs.start(args.command, args.urls, options, request_key=key,
                                allow_duplicates=allow_duplicates)
            args.job_id = job_id
            print(f"Job #{job_id} · Preparing {args.command} download...", flush=True)
            return job_id, 0
        except jobs.DuplicateRequest as duplicate:
            if duplicate.active:
                args.duplicate_active_job = duplicate.job_id
                if getattr(args, "defer_active", False):
                    return None, 1
                print(f"fetchlane: matching download is already active as job #{duplicate.job_id}", file=sys.stderr)
                return None, 1
            action = getattr(args, "on_duplicate", "ask")
            destination = None
            if action == "ask":
                try:
                    action, destination = duplicates.choose(duplicate.job_id)
                except (EOFError, KeyboardInterrupt):
                    print("\nDownload cancelled.", file=sys.stderr)
                    return None, 130
                except ValueError as error:
                    print(f"fetchlane: {error}", file=sys.stderr)
                    return None, 1
            if action == "skip":
                args.skipped = True
                print(f"Skipped: matching files already saved by job #{duplicate.job_id}.")
                return None, 0
            if action == "location":
                args.output = destination
                continue
            args.overwrite = True
            allow_duplicates = True


def run_prepared_job(job_id: int, args: argparse.Namespace, extra: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="fetchlane-") as temporary:
        report = Path(temporary) / "files.jsonl"
        jobs.claim(job_id)
        interrupted_by = None

        def interrupt(signum, frame):
            nonlocal interrupted_by
            interrupted_by = signum
            raise KeyboardInterrupt

        previous_handlers = {signum: signal.signal(signum, interrupt)
                             for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            try:
                if jobs.get(job_id)["status"] == "stopping":
                    code = 130
                else:
                    with jobs.monitor(job_id):
                        code = run_downloader(build_arguments(args, extra, report))
            except KeyboardInterrupt:
                code = 130
            except Exception:
                jobs.finish(job_id, 1, report)
                raise
            if interrupted_by is not None:
                code = 128 + interrupted_by
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        return jobs.finish(job_id, code, report)


def run_downloader(arguments: list[str]) -> int:
    if not getattr(sys, "frozen", False) and importlib.util.find_spec("yt_dlp") is None:
        print("fetchlane: yt-dlp is missing. Install it with: python -m pip install 'yt-dlp[default,curl-cffi]'", file=sys.stderr)
        return 127
    # Run in this process so cancellation also reaches yt-dlp's ffmpeg cleanup.
    import yt_dlp

    try:
        yt_dlp.main(arguments)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except KeyboardInterrupt:
        return 130
    return 0
