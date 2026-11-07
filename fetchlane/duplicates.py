"""Resolve existing-download conflicts using a small command-line prompt."""

from pathlib import Path
import sys

from . import jobs


def choose(job_id: int) -> tuple[str, Path | None]:
    if not sys.stdin.isatty():
        raise ValueError("duplicate needs a choice; use --on-duplicate overwrite or --on-duplicate skip, "
                         "or choose a different --output directory")
    print(f"Already downloaded by job #{job_id}:")
    previous = jobs.get(job_id)
    if previous:
        for file in previous["files"]:
            print(f"  {file['path']}")
    while True:
        choice = input("[o] Overwrite  [l] Change location  [s] Skip: ").strip().lower()
        if choice in ("o", "overwrite", "1"):
            return "overwrite", None
        if choice in ("s", "skip", "3"):
            return "skip", None
        if choice in ("l", "location", "2"):
            destination = input("New output directory: ").strip()
            if destination:
                return "location", Path(destination).expanduser().resolve()
            print("Enter a directory, or choose skip to leave this download unchanged.")
        else:
            print("Choose o, l, or s.")
