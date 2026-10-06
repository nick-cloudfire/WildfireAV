# parallel_api.py
"""
Shared utilities for the Elmfire validation pipeline.

Exports
-------
Tee                – write to multiple streams simultaneously (for log tee-ing)
run_subprocess     – run a command with its output routed through sys.stdout
get_thread_session – per-thread requests.Session (connection reuse)
retry_call         – exponential-backoff retry wrapper
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Callable, Optional, TypeVar

import requests

R = TypeVar("R")

_thread_local = threading.local()


# ---------------------------------------------------------------------------
# Tee  – write to multiple streams at once (useful for log + stdout)
# ---------------------------------------------------------------------------

class Tee:
    """
    Write to multiple streams simultaneously.

    Typical usage::

        with open("pipeline.log", "w") as log:
            tee = Tee(sys.stdout, log)
            with redirect_stdout(tee), redirect_stderr(tee):
                main()
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


# ---------------------------------------------------------------------------
# run_subprocess – route subprocess output through Python's sys.stdout
# ---------------------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started (wine, conda run → WindNinja, …)."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def run_subprocess(
    cmd: list,
    check: bool = True,
    timeout: float | None = None,
    log: Optional[IO[str]] = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess, streaming its stdout/stderr line by line to ``log`` (an
    open text file) or, by default, through Python's sys.stdout — so output
    lands in pipeline.log whether sys.stdout is a real file or a virtual
    stream (Tee, the per-case stamper …).

    The child runs in its own process group.  If ``timeout`` (seconds) expires
    the whole group is killed and ``subprocess.TimeoutExpired`` is raised, so a
    hung program fails its step instead of holding a worker until the job's
    wall time.  If the caller is interrupted (e.g. SIGTERM at a SLURM time
    limit) the group is killed too.
    """
    expired = threading.Event()

    def _expire(proc):
        expired.set()
        _kill_tree(proc)

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=(os.name == "posix"),
        **kwargs,
    ) as proc:
        assert proc.stdout is not None
        timer = threading.Timer(timeout, _expire, [proc]) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            for line in proc.stdout:
                if log is None:
                    print(line, end="", flush=True)
                else:
                    log.write(line)
                    log.flush()
            proc.wait()
        except BaseException:
            _kill_tree(proc)
            raise
        finally:
            if timer:
                timer.cancel()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)


def hours(h: float) -> float | None:
    """Config timeouts are in hours; 0 / None means no limit."""
    return h * 3600 if h else None


# ---------------------------------------------------------------------------
# get_thread_session – per-thread requests.Session
# ---------------------------------------------------------------------------

def get_thread_session() -> requests.Session:
    """Return (or create) a requests.Session local to the current thread."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


# ---------------------------------------------------------------------------
# retry_call – exponential-backoff retry
# ---------------------------------------------------------------------------

def retry_call(
    fn: Callable[[], R],
    *,
    tries: int = 4,
    base_sleep_s: float = 1.0,
    max_sleep_s: float = 20.0,
    retry_on: tuple = (requests.RequestException, TimeoutError),
    log: Optional[Callable[[str], None]] = None,
) -> R:
    """
    Call *fn* up to *tries* times with exponential back-off on *retry_on*.

    Parameters
    ----------
    fn          : zero-argument callable to attempt
    tries       : maximum number of attempts
    base_sleep_s: initial sleep before the second attempt
    max_sleep_s : sleep cap
    retry_on    : exception types that trigger a retry
    log         : optional logger for retry messages
    """
    sleep_s = base_sleep_s
    last_exc: Optional[BaseException] = None

    for attempt in range(1, tries + 1):
        try:
            return fn()
        except retry_on as e:
            last_exc = e
            if attempt == tries:
                break
            if log:
                log(f"Retry {attempt}/{tries} after {type(e).__name__}: {e}")
            time.sleep(min(max_sleep_s, sleep_s))
            sleep_s *= 2

    assert last_exc is not None
    raise last_exc
