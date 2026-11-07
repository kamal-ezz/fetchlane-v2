"""Persistent user preferences and a short first-run setup."""

import json
import os
from pathlib import Path
import sys
import tempfile

from .paths import downloads_directory
from .locking import file_lock


AUDIO_FORMATS = ("best", "mp3", "m4a", "opus", "flac", "wav")
VIDEO_FORMATS = ("best", "mp4", "mkv", "webm")
DEFAULTS = {"max_concurrent_downloads": 1, "audio_format": "best", "video_format": "best"}


def config_path() -> Path:
    if override := os.environ.get("FETCHLANE_CONFIG"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "fetchlane" / "config.json"


def directory(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"{path} is a file; choose a directory")
    return path


def validate_settings(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    result = {**DEFAULTS, **data}
    if "download_directory" in result:
        value = result["download_directory"]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("download_directory must be an absolute path")
        result["download_directory"] = str(directory(value))
    limit = result["max_concurrent_downloads"]
    if type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError("max_concurrent_downloads must be an integer between 1 and 16")
    if result["audio_format"] not in AUDIO_FORMATS:
        raise ValueError(f"audio_format must be one of: {', '.join(AUDIO_FORMATS)}")
    if result["video_format"] not in VIDEO_FORMATS:
        raise ValueError(f"video_format must be one of: {', '.join(VIDEO_FORMATS)}")
    return result


def load_settings() -> dict:
    path = config_path()
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULTS.copy()
    try:
        return validate_settings(json.loads(contents))
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"invalid configuration in {path}: {error}") from error


def load_directory() -> Path | None:
    value = load_settings().get("download_directory")
    return Path(value) if value else None


def default_directory() -> Path:
    return load_directory() or downloads_directory()


def save_directory(value: str | Path) -> Path:
    destination = directory(value)
    save_settings({"download_directory": str(destination)}, repair=True)
    return destination


def save_settings(changes: dict, *, repair: bool = False) -> dict:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock")):
        try:
            existing = load_settings()
        except ValueError:
            if not repair:
                raise
            existing = DEFAULTS.copy()
        settings = validate_settings({**existing, **changes})
        write_settings(path, settings)
        return settings


def write_settings(path: Path, settings: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".fetchlane-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(settings, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def setup() -> Path:
    proposed = downloads_directory()
    print(f"Welcome to fetchlane. Default download folder: {proposed}")
    while True:
        answer = input("Is this folder right? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            chosen = proposed
        elif answer in ("n", "no"):
            value = input("Download folder: ").strip()
            if not value:
                print("Enter a directory to save your downloads.")
                continue
            chosen = Path(value)
        else:
            print("Choose y to keep Downloads, or n to change it.")
            continue
        try:
            chosen = directory(chosen)
        except ValueError as error:
            print(error)
            continue
        saved = save_directory(chosen)
        print(f"Download folder saved: {saved}\nChange it later: fetchlane settings --downloads DIR\n")
        return saved


def resolve_output(explicit: Path | None, *, dry_run: bool = False, settings: dict | None = None) -> Path:
    if explicit is not None:
        return directory(explicit)
    values = load_settings() if settings is None else settings
    saved = Path(values["download_directory"]) if "download_directory" in values else None
    if saved is not None:
        return saved
    if not dry_run and sys.stdin.isatty():
        return setup()
    # Scripts and previews use Downloads without consuming stdin or saving consent.
    return downloads_directory()
