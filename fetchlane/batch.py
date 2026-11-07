"""Read a validated text-file queue and process it in source order."""

import argparse
from copy import copy
from pathlib import Path


MAX_URLS = 10_000
MAX_LINE_LENGTH = 8192


def read_urls(path: Path, validate) -> list[str]:
    urls = []
    with path.expanduser().open(encoding="utf-8-sig") as source:
        line_number = 0
        while line := source.readline(MAX_LINE_LENGTH + 1):
            line_number += 1
            if len(line) > MAX_LINE_LENGTH:
                raise ValueError(f"{path}:{line_number}: line exceeds {MAX_LINE_LENGTH} characters")
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            try:
                urls.append(validate(url))
            except argparse.ArgumentTypeError as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
            if len(urls) > MAX_URLS:
                raise ValueError(f"{path}: queue exceeds {MAX_URLS:,} links")
    if not urls:
        raise ValueError(f"{path}: no links found; add one full URL per line")
    return urls


def run(args: argparse.Namespace, urls: list[str], extra: list[str], execute) -> int:
    if getattr(args, "max_concurrent", 1) > 1 and not args.dry_run:
        from .workers import run_parallel
        return run_parallel(args, urls, extra)
    total = len(urls)
    print(f"Batch: {total} links · {args.media_type}", flush=True)
    succeeded = failed = skipped = stopped = 0
    for position, url in enumerate(urls, 1):
        print(f"\n[{position}/{total}] {url}", flush=True)
        item = copy(args)
        item.command = args.media_type
        item.urls = [url]
        item.batch_position = position
        try:
            code = execute(item, extra)
        except KeyboardInterrupt:
            code = 130
        if code in (130, 143):
            from . import jobs
            job = jobs.get(item.job_id) if getattr(item, "job_id", None) is not None else None
            if job is not None and job["status"] == "stopped":
                stopped += 1
                continue
            print(f"\nBatch interrupted: {succeeded} succeeded, {failed} failed, "
                  f"{total - position} remaining.", flush=True)
            return code
        if getattr(item, "skipped", False):
            skipped += 1
        elif code:
            failed += 1
        else:
            succeeded += 1
    if args.dry_run:
        print(f"\nPreviewed {total} links. No downloads or jobs created.")
    else:
        print(f"\nBatch complete: {succeeded} succeeded, {failed} failed, {skipped} skipped, {stopped} stopped.")
    return 1 if failed or stopped else 0
