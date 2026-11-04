from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import time
from pathlib import Path

_LOCK_DIR = Path(os.environ.get("APPTOPICS_LOCK_DIR", Path(__file__).resolve().parents[2] / "data" / "cache"))


class RateLimiter:
    """Min-interval limiter (calls per minute). With `shared=name` the interval is enforced across all
    processes on this machine (file lock + timestamp), so a background collection and an on-demand research
    run together still respect the API's limit."""

    def __init__(self, per_minute: float, shared: str | None = None):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._last = 0.0
        self._tlock = threading.Lock()
        self._path = (_LOCK_DIR / f"ratelimit_{shared}") if shared else None

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._tlock:
            if self._path is None:
                delta = time.monotonic() - self._last
                if delta < self.interval:
                    time.sleep(self.interval - delta)
                self._last = time.monotonic()
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.seek(0)
                    try:
                        last = float(f.read().strip() or 0)
                    except ValueError:
                        last = 0.0
                    delta = time.time() - last
                    if delta < self.interval:
                        time.sleep(self.interval - delta)
                    f.seek(0); f.truncate(); f.write(str(time.time())); f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def machine_lock(name: str):
    """Exclusive cross-process lock (e.g. Astro, which crashes under parallel load)."""
    p = _LOCK_DIR / f"lock_{name}"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
