r"""SessionStart env hook: session_id/transcript_path have to survive $CLAUDE_ENV_FILE.

The hook is an inline `python3 -c` inside hooks.json, so nothing else in the suite ever runs it.
It was wrong in two ways that only show up once a shell sources the file it writes:

  * the values went in unquoted, so every backslash of a Windows path was eaten as an escape --
    `C:\Users\demo\t.jsonl` came back as `C:Usersdemot.jsonl`, and a path with a space split;
  * the file was opened in text mode, which on Windows turns "\n" into CRLF and leaves a stray
    carriage return at the end of each value.

Neither is visible from Python alone, so these tests source the file with bash and compare what a
slash command would actually see. The consumer (`session.handoff`) treats an unreadable transcript
as "no summary" rather than an error, so a regression here is silent -- hence the coverage.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_HOOKS_JSON = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "hooks.json"

# Absolute path, not the bare name: on Windows CreateProcess searches System32 before PATH,
# so "bash" would launch the WSL stub there instead of the Git Bash that PATH points at.
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None or shutil.which("python3") is None, reason="needs bash and python3 on PATH"
)


def _env_hook_command() -> str:
    """The one SessionStart hook that populates $CLAUDE_ENV_FILE."""
    hooks = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    commands = [
        hook["command"]
        for entry in hooks
        for hook in entry["hooks"]
        if "CLAUDE_ENV_FILE" in hook["command"]
    ]
    assert len(commands) == 1, f"expected exactly one env-file hook, found {len(commands)}"
    return commands[0]


def _run_hook(payload: dict[str, str], env_file: Path) -> None:
    assert _BASH is not None
    result = subprocess.run(
        [_BASH, "-c", _env_hook_command()],
        input=json.dumps(payload),
        env={"CLAUDE_ENV_FILE": str(env_file), "PATH": str(Path(shutil.which("python3")).parent)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _source(env_file: Path, name: str) -> str:
    """What the shell hands a slash command after sourcing the file."""
    assert _BASH is not None
    # Pass the path through the environment: embedding it in the script would put a Windows
    # tmp_path -- backslashes and all -- back into a shell word, which is the very bug under test.
    result = subprocess.run(
        [_BASH, "-c", f'. "$ENVF"; printf %s "${name}"'],
        env={"ENVF": str(env_file), "PATH": "/usr/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "transcript_path",
    [
        pytest.param(r"C:\Users\demo\.claude\projects\proj\t.jsonl", id="windows"),
        pytest.param(r"C:\Users\demo\.claude\projects\a proj\t.jsonl", id="windows-space"),
        pytest.param("/home/demo/.claude/projects/proj/t.jsonl", id="posix"),
        pytest.param("/home/demo/a proj/t.jsonl", id="posix-space"),
    ],
)
def test_session_env_hook_round_trips_the_transcript_path(
    tmp_path: Path, transcript_path: str
) -> None:
    env_file = tmp_path / "env_file"
    _run_hook({"session_id": "sess-1", "transcript_path": transcript_path}, env_file)

    assert _source(env_file, "CLAUDE_TRANSCRIPT_PATH") == transcript_path
    assert _source(env_file, "CLAUDE_SESSION_ID") == "sess-1"


def test_session_env_hook_writes_lf_only(tmp_path: Path) -> None:
    env_file = tmp_path / "env_file"
    _run_hook({"session_id": "sess-1", "transcript_path": r"C:\tmp\t.jsonl"}, env_file)

    # Text mode on Windows would make these CRLF, putting a carriage return inside every value.
    assert b"\r" not in env_file.read_bytes(), env_file.read_bytes()


def test_session_env_hook_tolerates_a_payload_without_the_keys(tmp_path: Path) -> None:
    env_file = tmp_path / "env_file"
    _run_hook({}, env_file)

    # Empty has to stay empty rather than becoming a bare `export X=`, which is still valid but
    # would make a later `set -u` consumer see an unset-looking value.
    assert _source(env_file, "CLAUDE_TRANSCRIPT_PATH") == ""
    assert "export CLAUDE_TRANSCRIPT_PATH=''" in env_file.read_text(encoding="utf-8")
