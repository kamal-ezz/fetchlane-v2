"""Bounded process workers; prompts and output stay in the parent CLI."""

import argparse
from collections import deque
from copy import copy
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from . import jobs


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-worker"]
    return [sys.executable, "-m", "fetchlane", "--internal-worker"]


def worker_main() -> int:
    from .cli import run_prepared_job

    try:
        payload = json.loads(sys.stdin.read(1_048_577))
        job_id = payload["job_id"]
        values = payload["args"]
        extra = payload["extra"]
        if type(job_id) is not int or not isinstance(values, dict) or not isinstance(extra, list):
            raise ValueError("invalid worker request")
        if not all(isinstance(value, str) for value in extra):
            raise ValueError("invalid advanced options")
        job = jobs.get(job_id)
        if job is None or job["command"] != values["command"] or job["urls"] != values["urls"]:
            raise ValueError("worker request does not match its job")
        args = argparse.Namespace(**values)
        args.worker_output = True
        args.output = Path(args.output)
        if hasattr(args, "file"):
            args.file = Path(args.file)
        return run_prepared_job(job_id, args, extra)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        print(f"fetchlane: worker failed: {error}", file=sys.stderr)
        return 1


@dataclass
class Worker:
    process: subprocess.Popen
    reader: threading.Thread
    ended: bool = False


def kill_worker(worker: Worker) -> None:
    if worker.process.poll() is not None:
        return
    if os.name == "nt":
        # The process handle remains owned here; never use a PID from history.
        try:
            subprocess.run(["taskkill", "/PID", str(worker.process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"fetchlane: could not stop the worker tree: {error}", file=sys.stderr)
        if worker.process.poll() is None:
            worker.process.kill()
    else:
        try:
            os.killpg(worker.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@contextmanager
def handle_signals(handler):
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, handler)
        yield
    finally:
        for signum, original in previous.items():
            signal.signal(signum, original)


def run_parallel(args, urls: list[str], extra: list[str]) -> int:
    from .cli import prepare_job

    pending = deque()
    for position, url in enumerate(urls, 1):
        item = copy(args)
        item.command = args.media_type
        item.urls = [url]
        item.batch_position = position
        item.defer_active = True
        pending.append(item)
    total = len(pending)
    print(f"Batch: {total} links · {args.media_type} · up to {args.max_concurrent} concurrent", flush=True)
    active: dict[int, Worker] = {}
    deferred: dict[int, list] = {}
    messages = queue.Queue(maxsize=256)
    closing = threading.Event()
    succeeded = failed = skipped = stopped = 0
    cancellation_signal = None
    cancelled = False
    stop_deadline = None

    def interrupt(signum, frame):
        nonlocal cancellation_signal
        cancellation_signal = signum
        if not cancelled:
            raise KeyboardInterrupt

    def read_output(job_id, process):
        def send(line):
            while not closing.is_set():
                try:
                    messages.put((job_id, line), timeout=0.1)
                    return
                except queue.Full:
                    continue
        try:
            while line := process.stdout.readline(8192):
                send(line.rstrip("\r\n"))
        except (OSError, ValueError) as error:
            send(f"Could not read worker output: {error}")
        finally:
            send(None)

    def launch(job_id, item):
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **options)
        reader = threading.Thread(target=read_output, args=(job_id, process), daemon=True)
        active[job_id] = Worker(process, reader)
        reader.start()
        try:
            packet = json.dumps({"job_id": job_id, "args": vars(item), "extra": extra}, default=str)
            if len(packet.encode("utf-8")) > 1_048_576:
                raise ValueError("download options exceed the worker request limit")
            process.stdin.write(packet)
        finally:
            process.stdin.close()

    with handle_signals(interrupt), tempfile.TemporaryDirectory(prefix="fetchlane-queue-") as temporary:
        missing_report = Path(temporary) / "no-report.jsonl"
        try:
            while active or (pending and not cancelled):
                try:
                    if not cancelled and pending and len(active) < args.max_concurrent:
                        item = pending.popleft()
                        print(f"\n[{item.batch_position}/{total}] {item.urls[0]}", flush=True)
                        item.duplicate_active_job = None
                        try:
                            job_id, code = prepare_job(item, extra)
                        except (OSError, sqlite3.Error, ValueError) as error:
                            print(f"fetchlane: cannot prepare download: {error}", file=sys.stderr)
                            job_id, code = None, 1
                        if job_id is not None:
                            try:
                                launch(job_id, item)
                            except (OSError, ValueError) as error:
                                print(f"fetchlane: could not start job #{job_id}: {error}", file=sys.stderr)
                                if job_id not in active:
                                    jobs.finish(job_id, 126, missing_report)
                                    failed += 1
                        elif item.duplicate_active_job in active:
                            deferred.setdefault(item.duplicate_active_job, []).append(item)
                            print(f"Waiting for matching job #{item.duplicate_active_job}...", flush=True)
                        elif code in (130, 143):
                            raise KeyboardInterrupt
                        elif getattr(item, "skipped", False):
                            skipped += 1
                        else:
                            if item.duplicate_active_job is not None:
                                print(f"fetchlane: matching download is already active as job #{item.duplicate_active_job}",
                                      file=sys.stderr)
                            failed += 1
                    try:
                        job_id, line = messages.get(timeout=0.05)
                        if line is None:
                            active[job_id].ended = True
                        elif line:
                            print(f"[Job #{job_id}] {line}", flush=True)
                    except queue.Empty:
                        pass
                    for job_id, worker in list(active.items()):
                        code = worker.process.poll()
                        if code is None or not worker.ended:
                            continue
                        worker.reader.join()
                        worker.process.stdout.close()
                        worker.process.wait()
                        job = jobs.get(job_id)
                        if job is not None and job["status"] in jobs.ACTIVE_STATUSES:
                            normalized = code if code >= 0 else 128 - code
                            code = jobs.finish(job_id, normalized or 1, missing_report)
                            job = jobs.get(job_id)
                        if job is not None and job["status"] in ("stopped", "interrupted"):
                            stopped += 1
                        elif code:
                            failed += 1
                        else:
                            succeeded += 1
                        del active[job_id]
                        pending.extendleft(reversed(deferred.pop(job_id, [])))
                    if cancelled and stop_deadline is not None and time.monotonic() >= stop_deadline:
                        for worker in active.values():
                            kill_worker(worker)
                except KeyboardInterrupt:
                    cancelled = True
                    cancellation_signal = cancellation_signal or signal.SIGINT
                    stop_deadline = time.monotonic() + 20
                    print("\nStopping active downloads...", flush=True)
                    for job_id in active:
                        try:
                            jobs.request_stop(job_id)
                        except ValueError:
                            pass  # The worker may already have finished.
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"fetchlane: queue failed: {error}", file=sys.stderr)
            failed += 1
        finally:
            closing.set()
            for worker in active.values():
                kill_worker(worker)
                worker.process.wait(timeout=10)
                worker.reader.join(timeout=2)
                worker.process.stdout.close()
            # Include a job interrupted in the tiny gap between reservation and launch.
            try:
                with jobs.connect() as connection:
                    unfinished = [row["id"] for row in connection.execute(
                        "SELECT id FROM jobs WHERE pid = ? AND status IN ('running', 'stopping')", (os.getpid(),))]
                for job_id in unfinished:
                    jobs.finish(job_id, 130 if cancelled else 1, missing_report)
                for job_id in active:
                    job = jobs.get(job_id)
                    if job is not None and job["status"] in jobs.ACTIVE_STATUSES:
                        jobs.finish(job_id, 130 if cancelled else 1, missing_report)
            except (OSError, sqlite3.Error, ValueError) as error:
                print(f"fetchlane: could not finalize queue history: {error}", file=sys.stderr)
                failed += 1
    print(f"\nBatch {'interrupted' if cancelled else 'complete'}: {succeeded} succeeded, {failed} failed, "
          f"{skipped} skipped, {stopped} stopped.")
    if cancelled:
        print(f"{total - succeeded - failed - skipped - stopped} links remaining.")
        return 128 + cancellation_signal
    return 1 if failed or stopped else 0
