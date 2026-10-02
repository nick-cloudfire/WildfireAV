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

import io
import subprocess
import sys
import threading
import time
from typing import Callable, Optional, TypeVar

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

def run_subprocess(
    cmd: list,
    check: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess and route its stdout/stderr through Python's sys.stdout.

    Works whether sys.stdout is a real file (e.g. pipeline.log via
    redirect_stdout) or a virtual stream (e.g. Tee).  Output is streamed
    line-by-line so progress appears in real time.

    Use this instead of subprocess.run() for any process whose output should
    land in pipeline.log rather than leaking to the terminal.
    WindNinja is exempt — it writes to its own chunk log intentionally.

    If the caller is interrupted (e.g. SIGTERM at a SLURM time limit) the child
    is killed rather than left running.
    """
    try:
        sys.stdout.fileno()
        # sys.stdout is a real file descriptor — hand it to the OS directly
        return subprocess.run(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=check,
            **kwargs,
        )
    except (AttributeError, io.UnsupportedOperation):
        # sys.stdout is a virtual stream (Tee, StringIO …) — stream line by line
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **kwargs,
        ) as proc:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    print(line, end="", flush=True)
            except BaseException:
                proc.kill()
                raise
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        return subprocess.CompletedProcess(cmd, proc.returncode)


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
