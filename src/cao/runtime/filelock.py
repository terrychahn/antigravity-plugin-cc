"""Cross-platform exclusive file lock for the BL-24 cold-start barrier.

``daemon.serve()`` serializes cold starts under this lock so two near-simultaneous
starts can never both bind. POSIX uses ``fcntl.flock``; Windows has no ``fcntl`` at
all (the bare ``import`` used to abort every module that pulls in ``daemon``), so it
uses ``msvcrt.locking`` over a one-byte region of the same lock file.

The two primitives are NOT interchangeable: ``flock(LOCK_EX)`` blocks until the lock
is free, while ``msvcrt.locking(LK_LOCK)`` retries internally for ~10s and then raises
``OSError(EDEADLOCK)``. A straight substitution would turn a slow-but-fine cold start
into a dead daemon, so the Windows path re-issues LK_LOCK until it wins — matching
flock's block-until-acquired contract — and logs once so a genuinely stuck lock stays
diagnosable instead of hanging silently.
"""

from __future__ import annotations

import errno
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

# msvcrt locks a byte range, not the whole file; offset 0 + one byte is the usual
# stand-in for "the file". The region may extend past EOF, so the empty lock file
# never needs padding.
_LOCK_BYTES = 1

# LK_LOCK gives up with this after its internal 10 x 1s retries. EDEADLOCK is the
# documented code; keep EDEADLK too since the two are aliases on some builds.
_RETRY_ERRNOS = frozenset(
    {getattr(errno, name) for name in ("EDEADLOCK", "EDEADLK") if hasattr(errno, name)}
)


def _acquire(fd: int) -> None:
    if sys.platform == "win32":
        waited = False
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, _LOCK_BYTES)
                return
            except OSError as exc:
                if exc.errno not in _RETRY_ERRNOS:
                    raise
                if not waited:
                    logger.warning("waiting on daemon cold-start lock (held by another start)")
                    waited = True
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on *path* for the duration of the block.

    Closing the descriptor releases the lock on both platforms, so the caller never
    has to unlock explicitly — which matters because ``serve()`` returns from inside
    the block when it yields to an already-live daemon.
    """
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(fd)
        yield
    finally:
        os.close(fd)
