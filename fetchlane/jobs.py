"""Small, transactional job history shared by concurrent CLI processes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import signal
import _thread


class DuplicateRequest(Exception):
    def __init__(self, job_id: int, active: bool):
        self.job_id = job_id
        self.active = active


ACTIVE_STATUSES = ("running", "stopping")


def database_path() -> Path:
    if override := os.environ.get("FETCHLANE_DB"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "fetchlane" / "jobs.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL, urls TEXT NOT NULL, options TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                exit_code INTEGER, pid INTEGER NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS files (
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                title TEXT NOT NULL, path TEXT NOT NULL, url TEXT,
                UNIQUE(job_id, path)
            )""")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, declaration in (("request_key", "TEXT"), ("heartbeat_at", "REAL")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_request
                ON jobs(request_key) WHERE status IN ('running', 'stopping')""")
            connection.execute("CREATE INDEX IF NOT EXISTS completed_request ON jobs(request_key, status, id)")
        yield connection
    finally:
        connection.close()


def start(command: str, urls: list[str], options: dict, request_key: str | None = None,
          allow_duplicates: bool = False) -> int:
    with connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if request_key is not None:
            active = connection.execute(
                "SELECT id FROM jobs WHERE request_key = ? AND status IN ('running', 'stopping')",
                (request_key,),
            ).fetchone()
            if active:
                raise DuplicateRequest(active["id"], active=True)
            # Playlists can gain new entries; always let yt-dlp refresh their contents.
            if not allow_duplicates and command != "playlist":
                previous = connection.execute(
                    "SELECT id FROM jobs WHERE request_key = ? AND status = 'succeeded' ORDER BY id DESC LIMIT 1",
                    (request_key,),
                ).fetchone()
                if previous:
                    paths = connection.execute("SELECT path FROM files WHERE job_id = ?", (previous["id"],))
                    found = False
                    intact = True
                    for row in paths:
                        found = True
                        if not Path(row["path"]).is_file():
                            intact = False
                            break
                    if found and intact:
                        raise DuplicateRequest(previous["id"], active=False)
        cursor = connection.execute(
            """INSERT INTO jobs (command, urls, options, status, started_at, pid, request_key, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (command, json.dumps(urls), json.dumps(options), "running", now(), os.getpid(), request_key, time.time()),
        )
        return cursor.lastrowid


def finish(job_id: int, exit_code: int, report: Path) -> int:
    status = "succeeded" if exit_code == 0 else "interrupted" if exit_code in (130, 143) else "failed"
    with connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status, heartbeat_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job #{job_id} was removed before its outcome could be saved")
        if row["status"] == "stopping":
            status = "stopped"
            exit_code = 130
        if report.exists():
            with report.open(encoding="utf-8") as records:
                for line in records:
                    record = json.loads(line)
                    connection.execute(
                        "INSERT OR IGNORE INTO files (job_id, title, path, url) VALUES (?, ?, ?, ?)",
                        (job_id, record["title"], record["path"], record.get("url")),
                    )
        connection.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, exit_code = ? WHERE id = ?",
            (status, now(), exit_code, job_id),
        )
    return exit_code


def claim(job_id: int) -> None:
    with connect() as connection, connection:
        updated = connection.execute(
            "UPDATE jobs SET pid = ?, heartbeat_at = ? WHERE id = ? AND status IN ('running', 'stopping')",
            (os.getpid(), time.time(), job_id),
        )
        if updated.rowcount != 1:
            raise ValueError(f"job #{job_id} is no longer available to run")


def request_stop(job_id: int) -> None:
    with connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status, heartbeat_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job #{job_id} was not found")
        if row["status"] not in ACTIVE_STATUSES:
            raise ValueError(f"job #{job_id} is {row['status']}, not running")
        if row["heartbeat_at"] is None or time.time() - row["heartbeat_at"] >= 60:
            raise ValueError(f"job #{job_id} has no recent heartbeat; stop its original terminal process, or remove stale history with --force")
        connection.execute("UPDATE jobs SET status = 'stopping' WHERE id = ?", (job_id,))


def remove(job_id: int, force: bool = False) -> None:
    with connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status, heartbeat_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job #{job_id} was not found")
        if row["status"] in ACTIVE_STATUSES:
            fresh = row["heartbeat_at"] is not None and time.time() - row["heartbeat_at"] < 60
            if fresh or not force:
                raise ValueError(f"job #{job_id} is active; stop it first. Use --force only for stale jobs after 60 seconds")
        connection.execute("DELETE FROM files WHERE job_id = ?", (job_id,))
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


@contextmanager
def monitor(job_id: int):
    """Cooperate with stop requests; only ever interrupt this process itself."""
    done = threading.Event()

    def watch():
        while not done.wait(0.25):
            try:
                with connect() as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    if row is None or row["status"] not in ACTIVE_STATUSES:
                        return
                    connection.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (time.time(), job_id))
                    stopping = row["status"] == "stopping"
            except (OSError, sqlite3.Error) as error:
                print(f"fetchlane: job control is unavailable: {error}", file=sys.stderr)
                return
            if stopping and not done.is_set():
                if sys.platform == "win32":
                    _thread.interrupt_main()
                else:
                    os.kill(os.getpid(), signal.SIGINT)
                return

    thread = threading.Thread(target=watch, name=f"job-{job_id}-control", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()


def decode(row) -> dict:
    result = dict(row)
    result["urls"] = json.loads(result["urls"])
    result["options"] = json.loads(result["options"])
    return result


def recent(limit: int, status: str | None = None) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE (? IS NULL OR status = ?) ORDER BY id DESC LIMIT ?",
            (status, status, limit),
        )
        return [decode(row) for row in rows]


def get(job_id: int) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        result = decode(row)
        result["files"] = [dict(row) for row in connection.execute(
            "SELECT title, path, url FROM files WHERE job_id = ? ORDER BY rowid", (job_id,),
        )]
        return result


def display(job_id: int | None, limit: int, status: str | None, as_json: bool) -> int:
    if job_id is not None:
        job = get(job_id)
        if job is None:
            print(f"fetchlane: job #{job_id} was not found", file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
            return 0
        print(f"Job #{job['id']} · {job['command']} · {job['status']}")
        print(f"Started:  {job['started_at']}")
        print(f"Finished: {job['finished_at'] or 'No final outcome recorded'}")
        print(f"Process:  {job['pid']}")
        if job['exit_code'] is not None:
            print(f"Exit code: {job['exit_code']}")
        for url in job['urls']:
            print(f"URL: {url}")
        for name, value in job['options'].items():
            print(f"{name}: {value}")
        for file in job['files']:
            print(f"Saved: {file['path']}")
    else:
        rows = recent(limit, status)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("No jobs found. Start one with: fetchlane video \"URL\"")
        else:
            print(f"{'ID':>6}  {'STATUS':12}  {'COMMAND':8}  STARTED (UTC)")
            for job in rows:
                print(f"{job['id']:>6}  {job['status']:12}  {job['command']:8}  {job['started_at']}")
            print("\nDetails: fetchlane jobs ID")
    return 0
