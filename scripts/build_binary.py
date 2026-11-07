"""Build a single executable on the target operating system."""

from pathlib import Path
import importlib
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    # Fail before packaging if the optional native backend cannot be loaded.
    import curl_cffi
    importlib.import_module("yt_dlp.networking._curlcffi")
    print(f"Bundling browser impersonation backend: curl_cffi {curl_cffi.__version__}", flush=True)
    return subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--onefile", "--name", "fetchlane", "--paths", str(ROOT),
         "--collect-all", "yt_dlp", "--collect-all", "yt_dlp_ejs",
         "--collect-all", "curl_cffi", "--collect-all", "certifi",
         "--hidden-import", "_cffi_backend",
         "--copy-metadata", "curl-cffi",
         "--copy-metadata", "yt-dlp", "--copy-metadata", "yt-dlp-ejs",
         "--specpath", str(ROOT / "build"),
         "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build" / "pyinstaller"),
         str(ROOT / "scripts" / "standalone.py")],
        cwd=ROOT, check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
