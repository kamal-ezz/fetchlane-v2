"""Cross-platform advisory lock for short settings updates."""

from contextlib import contextmanager
import errno
import os
import time


@contextmanager
def file_lock(path):
    with path.open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"\0")
            lock.flush()
        deadline = time.monotonic() + 10
        while True:
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"settings are busy: {path}") from error
                time.sleep(0.05)
        yield
        # Closing the descriptor releases the lock, including when an exception occurs.
