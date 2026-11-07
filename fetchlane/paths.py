"""Find the user's system Downloads folder without shelling out."""

from pathlib import Path
import os
import shlex
import sys


def windows_downloads_directory() -> Path | None:
    import ctypes
    import uuid

    # FOLDERID_Downloads, as defined by Windows' Known Folder API.
    folder = ctypes.create_string_buffer(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le, 16)
    get_path = ctypes.WinDLL("shell32").SHGetKnownFolderPath
    get_path.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)]
    get_path.restype = ctypes.c_long
    free = ctypes.WinDLL("ole32").CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    pointer = ctypes.c_void_p()
    try:
        # DONT_VERIFY allows a redirected folder that has not been created yet.
        result = get_path(ctypes.byref(folder), 0x4000, None, ctypes.byref(pointer))
        if result == 0 and pointer.value:
            return Path(ctypes.wstring_at(pointer))
        return None
    finally:
        if pointer.value:
            free(pointer)


def xdg_downloads_directory() -> Path | None:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    try:
        contents = (root / "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in contents.splitlines():
        key, separator, value = line.strip().partition("=")
        if not separator or key.strip() != "XDG_DOWNLOAD_DIR":
            continue
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            return None
        if len(parts) != 1:
            return None
        value = parts[0]
        # Only the HOME prefix is expanded; shell commands are never evaluated.
        if value in ("$HOME", "${HOME}"):
            value = str(Path.home())
        elif value.startswith("$HOME/"):
            value = str(Path.home() / value[len("$HOME/"):])
        elif value.startswith("${HOME}/"):
            value = str(Path.home() / value[len("${HOME}/"):])
        path = Path(value)
        return path if path.is_absolute() else None
    return None


def downloads_directory() -> Path:
    if sys.platform == "win32":
        try:
            found = windows_downloads_directory()
        except (OSError, AttributeError):
            found = None
        if found is not None:
            return found
    elif sys.platform != "darwin":
        if found := xdg_downloads_directory():
            return found
    return Path.home() / "Downloads"
