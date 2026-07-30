"""BL-21 — daemon and companion resolve the SAME runtime-dir socket path.

The companion cannot import cao, so it duplicates _runtime_socket logic inline;
this cross-checks the two copies never diverge (else they compute different sockets
and never connect). Existing parity tests are extended to also verify the socket
lives in the runtime dir (not state_dir) and stays short (AF_UNIX safe).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from cao.runtime import daemon, workspace

_COMPANION_PATH = Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "cao-companion.py"


def _load_companion() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cao_companion", _COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


companion = _load_companion()


def test_daemon_and_companion_resolve_same_anchored_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a worker-created subdir, both daemon and companion anchor to the parent
    (which bears the root marker) and compute the identical runtime-dir socket path."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    parent = tmp_path / "proj"
    parent.mkdir()
    sub = parent / "super_mario"
    sub.mkdir()
    marker_dir = workspace.state_dir(parent)
    marker_dir.mkdir(parents=True)
    (marker_dir / "root").touch()
    monkeypatch.chdir(sub)

    d_sock = daemon.socket_path()
    c_sock = Path(companion._socket_path())
    assert d_sock == c_sock
    assert not str(d_sock).startswith(str(workspace.state_dir(parent)))
    assert len(str(d_sock)) < 100


def test_daemon_and_companion_agree_on_marker_and_env_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover the OTHER two resolution branches so a divergence in _MARKERS or the
    CAO_WORKSPACE handling between the daemon copy and the companion copy is caught."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "pkg" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert daemon.socket_path() == Path(companion._socket_path())
    assert not str(daemon.socket_path()).startswith(str(workspace.state_dir(repo.resolve())))

    ws = tmp_path / "explicit"
    ws.mkdir()
    monkeypatch.setenv("CAO_WORKSPACE", str(ws))
    assert daemon.socket_path() == Path(companion._socket_path())
    assert not str(daemon.socket_path()).startswith(str(workspace.state_dir(ws.resolve())))


def test_daemon_and_companion_agree_on_blocklisted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-21 regression parity: a stray root marker at /tmp must NOT make EITHER the
    daemon or the companion anchor to /tmp."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    tmp_state = workspace.state_dir(Path("/tmp"))
    tmp_state.mkdir(parents=True, exist_ok=True)
    (tmp_state / "root").touch()
    sub = tmp_path / "proj" / "sub"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    assert daemon.socket_path() == Path(companion._socket_path())
    assert daemon.socket_path() != workspace._runtime_socket(Path("/tmp"))


def test_parity_with_xdg_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon and companion agree when XDG_RUNTIME_DIR is set."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path / "proj"))
    monkeypatch.delenv("CAO_PLUGIN_DATA", raising=False)
    assert daemon.socket_path() == Path(companion._socket_path())
    sock = daemon.socket_path()
    assert str(sock).startswith(str(tmp_path / "runtime"))
    # No length assertion here: the caller supplied the directory, so its length is the
    # environment's property, not the code's. The other tests unset XDG_RUNTIME_DIR and
    # do assert it, which is where the AF_UNIX sun_path cap is actually ours to keep.


def test_parity_without_xdg_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon and companion agree when XDG_RUNTIME_DIR is unset.

    The fallback is per-user on both platforms, but gets there differently: POSIX
    namespaces a shared /tmp by uid, Windows inherits a per-user %TEMP%.
    """
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path / "proj"))
    monkeypatch.delenv("CAO_PLUGIN_DATA", raising=False)
    assert daemon.socket_path() == Path(companion._socket_path())
    sock = daemon.socket_path()
    if sys.platform == "win32":
        assert sock.parent == Path(tempfile.gettempdir())
    else:
        assert f"cao-{os.getuid()}" in str(sock)
    assert len(str(sock)) < 100


def test_socket_not_in_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Socket lives in runtime dir, never under state_dir; state_dir is unchanged."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path / "proj"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    ws_path = (tmp_path / "proj").resolve()
    sd = workspace.state_dir(ws_path)
    sock = workspace.socket_path()
    assert not str(sock).startswith(str(sd))
    assert sd.parent == tmp_path / "data" / "state"
    assert len(str(sock)) < 100


def test_long_workspace_socket_stays_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a very long workspace path produces a short socket path (< 100 bytes)."""
    long_proj = tmp_path / ("a" * 60) / ("b" * 60)
    monkeypatch.setenv("CAO_WORKSPACE", str(long_proj))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    sock = workspace.socket_path()
    assert len(str(sock)) < 100
    assert daemon.socket_path() == Path(companion._socket_path())
