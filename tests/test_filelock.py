"""BL-24 cold-start barrier: the lock must actually serialize across processes.

The daemon's split-brain guard is only as good as the lock under it, and the two
platform primitives differ enough (flock blocks forever; msvcrt.locking gives up after
~10s with EDEADLOCK) that "it imported fine" proves nothing. These tests run a real
second process so the POSIX and Windows paths are held to the same contract.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from cao.runtime.filelock import exclusive_lock

# Waiter child: block on the lock, then report how long it waited.
_WAITER = """
import sys, time
from pathlib import Path
from cao.runtime.filelock import exclusive_lock

start = time.monotonic()
with exclusive_lock(Path(sys.argv[1])):
    print(time.monotonic() - start)
"""

_HOLD_SECONDS = 1.5


def _spawn_waiter(lock_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _WAITER, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    """A second process must not enter the block until the first one leaves it."""
    lock_path = tmp_path / "daemon.lock"

    with exclusive_lock(lock_path):
        waiter = _spawn_waiter(lock_path)
        time.sleep(_HOLD_SECONDS)

    stdout, stderr = waiter.communicate(timeout=30)
    assert waiter.returncode == 0, f"waiter failed: {stderr}"

    # The waiter timestamps from its own start, so its wait includes interpreter
    # startup — it can only be LONGER than the hold, never shorter, unless the lock
    # failed to exclude it.
    waited = float(stdout.strip())
    assert waited >= _HOLD_SECONDS, (
        f"waiter acquired the lock after {waited:.2f}s while it was held for "
        f"{_HOLD_SECONDS}s — the lock did not exclude it"
    )


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    """Leaving the block frees the lock for the next acquirer (no explicit unlock)."""
    lock_path = tmp_path / "daemon.lock"

    with exclusive_lock(lock_path):
        pass

    waiter = _spawn_waiter(lock_path)
    stdout, stderr = waiter.communicate(timeout=30)
    assert waiter.returncode == 0, f"waiter failed: {stderr}"
    assert float(stdout.strip()) < _HOLD_SECONDS


def test_lock_is_released_on_exception(tmp_path: Path) -> None:
    """serve() can raise inside the block; a leaked lock would wedge every later start."""
    lock_path = tmp_path / "daemon.lock"

    class _Boom(Exception):
        pass

    try:
        with exclusive_lock(lock_path):
            raise _Boom
    except _Boom:
        pass

    waiter = _spawn_waiter(lock_path)
    stdout, stderr = waiter.communicate(timeout=30)
    assert waiter.returncode == 0, f"waiter failed: {stderr}"
    assert float(stdout.strip()) < _HOLD_SECONDS
