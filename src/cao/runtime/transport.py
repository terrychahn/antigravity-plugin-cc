"""The daemon's listening endpoint — AF_UNIX on POSIX, a named pipe on Windows.

CPython does not expose ``socket.AF_UNIX`` on Windows and ``asyncio.start_unix_server``
does not exist there at all (the ProactorEventLoop cannot drive a domain socket), so the
endpoint itself has to differ per platform. Everything above this module works in terms of
``(StreamReader, StreamWriter)`` and is unchanged: both backends hand the same pair to the
same ``handle_client``.

An *address* is the opaque endpoint string; ``address()`` derives it from the socket path
``workspace.socket_path()`` already computes, so the per-workspace digest scheme, the
runtime-dir placement, and the companion's byte-identical copy of it all keep working. On
POSIX the address IS that path. On Windows it is ``\\\\.\\pipe\\cao-<hash of that path>``:
the pipe namespace is flat, so the path is hashed rather than embedded.

Three POSIX concerns simply do not exist on the Windows side:

* **Access control.** A domain socket is guarded by directory permissions, hence
  ``verify_owner``'s chmod + st_uid check. A named pipe's default ACL already grants access
  only to the creating user, so the Windows branch is a no-op rather than a weaker check.
* **Stale endpoints.** A socket file outlives a crashed daemon and must be unlinked; a pipe
  is released by the kernel with the process, so ``cleanup`` has nothing to do.
* **Double binds.** Two POSIX daemons can both bind, which is why serve() holds a lock and
  pings first. A second ``CreateNamedPipe`` on a live name fails with ``PermissionError``.

plugin/scripts/cao-companion.py carries a mirror of the client half (it cannot import cao);
tests/test_companion_socket.py cross-checks that the copies never diverge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol, cast

# Every branch below tests ``sys.platform`` literally rather than a module-level constant.
# That is not a style choice: mypy only narrows platform-specific code on a direct comparison
# against the literal, and this module by design names APIs that exist on one OS and not the
# other (AF_UNIX, start_serving_pipe). Routed through a constant, --strict reports nine
# attr-defined errors here and the only cure is nine ``type: ignore``s. CI must run the suite
# on both platforms for each half to be checked.

ClientHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]


class Server(Protocol):
    """The subset of asyncio.Server that daemon.serve() and the tests use."""

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, *exc: Any) -> None: ...


def address(sock_path: Path) -> str:
    """The endpoint to bind/connect, derived from *sock_path*.

    POSIX returns the path itself. Windows hashes it into the flat pipe namespace, so two
    workspaces whose socket files differ anywhere in the path still get distinct pipes.
    """
    if sys.platform != "win32":
        return str(sock_path)
    digest = hashlib.sha256(str(sock_path).encode()).hexdigest()[:16]
    return rf"\\.\pipe\cao-{digest}"


def prepare(sock_path: Path) -> None:
    """Make the endpoint bindable. POSIX needs the parent dir; a pipe has no parent."""
    if sys.platform == "win32":
        return
    sock_path.parent.mkdir(parents=True, exist_ok=True)


def verify_owner(sock_path: Path) -> None:
    """Ensure only this user can reach the endpoint, or raise RuntimeError.

    POSIX tightens the containing directory to 0700 and refuses to bind under a directory
    owned by someone else. Windows gets the same guarantee from the pipe's default ACL, so
    there is nothing to assert.
    """
    if sys.platform == "win32":
        return
    sock_path.parent.chmod(0o700)
    if sock_path.parent.stat().st_uid != os.getuid():
        raise RuntimeError(
            f"Socket directory {sock_path.parent} is not owned by current user"
            f" (uid {os.getuid()}); refusing to bind."
        )


def cleanup(sock_path: Path) -> None:
    """Remove a leftover endpoint. No-op on Windows — a pipe dies with its process."""
    if sys.platform == "win32":
        return
    sock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class _PipeServer:
    """asyncio.Server's close()/wait_closed() over a Windows PipeServer, which has neither.

    PipeServer.close() stops accepting and drops the unconnected instance, but exposes no
    way to await that. Already-accepted connections are owned by daemon.serve(), which
    closes its own writers, so waiting only has to yield control long enough for the
    accept loop's pending future to finish cancelling.
    """

    def __init__(self, servers: list[Any]) -> None:
        self._servers = servers

    def close(self) -> None:
        for server in self._servers:
            server.close()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    async def __aenter__(self) -> _PipeServer:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.close()
        await self.wait_closed()


async def serve(handler: ClientHandler, sock_path: Path) -> Server:
    """Listen on *sock_path*'s endpoint, calling *handler* per connection."""
    addr = address(sock_path)
    if sys.platform != "win32":
        return await asyncio.start_unix_server(handler, path=addr)

    loop = asyncio.get_running_loop()

    def factory() -> asyncio.StreamReaderProtocol:
        reader = asyncio.StreamReader(loop=loop)
        return asyncio.StreamReaderProtocol(reader, handler, loop=loop)

    # Pipe I/O lives on ProactorEventLoop, not the AbstractEventLoop get_running_loop()
    # advertises; it is the Windows default, and a SelectorEventLoop cannot serve a pipe at
    # all, so the cast documents a real precondition rather than papering over a maybe.
    proactor = cast("asyncio.ProactorEventLoop", loop)
    # Same protocol object start_unix_server builds internally, so the handler receives an
    # identical (StreamReader, StreamWriter) pair on both platforms.
    return _PipeServer(await proactor.start_serving_pipe(factory, addr))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


async def open_connection(sock_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to *sock_path*'s endpoint. Raises OSError when nothing is listening."""
    addr = address(sock_path)
    if sys.platform != "win32":
        return await asyncio.open_unix_connection(addr)

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    proactor = cast("asyncio.ProactorEventLoop", loop)  # see serve()
    transport, _ = await proactor.create_pipe_connection(lambda: protocol, addr)
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


def request_sync(sock_path: Path, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Send one newline-framed JSON request and read one response, synchronously.

    For callers with no event loop: the daemon's own startup probe, and (via its mirror)
    the companion, which is a short-lived script.

    Raises OSError when nothing is listening, ValueError on an unparseable reply.
    """
    line = json.dumps(payload).encode() + b"\n"
    if sys.platform == "win32":
        raw = _pipe_roundtrip(address(sock_path), line, timeout)
    else:
        raw = _unix_roundtrip(str(sock_path), line, timeout)
    parsed: Any = json.loads(raw.split(b"\n")[0])
    if not isinstance(parsed, dict):
        raise ValueError(f"Unexpected response type: {type(parsed)}")
    return parsed


def _unix_roundtrip(addr: str, line: bytes, timeout: float) -> bytes:
    if sys.platform == "win32":  # unreachable: request_sync routes Windows to _pipe_roundtrip
        raise RuntimeError("no AF_UNIX on Windows")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(addr)
        client.sendall(line)
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
    return buf


def _pipe_roundtrip(addr: str, line: bytes, timeout: float) -> bytes:
    # A pipe instance serves one client at a time: while the daemon is mid-handshake with
    # someone else the open fails with ERROR_PIPE_BUSY, which is transient, unlike the
    # FileNotFoundError raised when no daemon is listening at all. Retry only the former,
    # so "no daemon" still fails fast instead of burning the whole timeout.
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = open(addr, "r+b", buffering=0)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) != 231 or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    with handle:
        handle.write(line)
        handle.flush()
        buf = b""
        while b"\n" not in buf:
            chunk = handle.read(4096)
            if not chunk:
                break
            buf += chunk
    return buf
