#!/usr/bin/env python3
"""PostToolUse hook: accumulate file edits, spawn git committer at threshold.

Reads JSON from stdin (Claude Code hook protocol). When Claude edits/writes
a file in a git repo, appends to an accumulator. At threshold (3+ edits AND
30s elapsed, or 10 edits), spawns a detached git_committer.py subprocess.

Performance budget: <100ms (no git operations in hot path).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SETTINGS_FILE = Path.home() / ".claude" / "cockpit-settings.json"
PENDING_DIR = Path.home() / ".claude" / "checkpoints" / "git-pending"
COMMITTER_SCRIPT = Path(__file__).parent / "git_committer.py"
CHECKPOINTED_TOOLS = ("Edit", "Write")
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

# Thresholds (can be overridden via cockpit-settings.json)
DEFAULT_MIN_EDITS = 3
DEFAULT_MAX_WAIT_SECS = 30
DEFAULT_FORCE_AT_EDITS = 10


def _read_settings() -> dict:
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        return settings
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def main():
    # 1. Fast exit if disabled
    settings = _read_settings()
    if not settings.get("git_checkpoints_enabled", True):
        return

    # 2. Read hook input from stdin
    try:
        raw = sys.stdin.read()
        if not raw:
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    tool = data.get("tool_name", "")
    if tool not in CHECKPOINTED_TOOLS:
        return

    file_path = data.get("tool_input", {}).get("file_path")
    if not file_path:
        return

    session_id = data.get("session_id", "unknown")
    cwd = data.get("cwd", "")
    if not cwd:
        return

    # Validate session_id to prevent path traversal
    if not _SESSION_ID_RE.fullmatch(session_id):
        return

    # 3. Append to accumulator
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    acc_path = PENDING_DIR / f"{session_id}.json"

    try:
        acc = json.loads(acc_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        acc = {
            "session_id": session_id,
            "cwd": cwd,
            "is_git": None,
            "first_edit_ts": time.time(),
            "last_edit_ts": time.time(),
            "edits": [],
        }

    # Invalidate cached is_git if cwd changed (session may switch projects)
    if acc.get("cwd") != cwd:
        acc["cwd"] = cwd
        acc["is_git"] = None

    # Cache is_git check per accumulator
    if acc.get("is_git") is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=cwd, capture_output=True, text=True, timeout=3,
            )
            acc["is_git"] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            acc["is_git"] = False

    if not acc["is_git"]:
        return  # Not a git repo, skip

    now = time.time()
    acc["last_edit_ts"] = now
    acc["edits"].append({
        "tool": tool,
        "file_path": file_path,
        "timestamp": now,
    })

    # 4. Write accumulator atomically
    try:
        fd, tmp = tempfile.mkstemp(dir=str(PENDING_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(acc, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, acc_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return
    except OSError:
        return

    # 5. Evaluate threshold
    edit_count = len(acc["edits"])
    elapsed = now - acc["first_edit_ts"]

    min_edits = settings.get("git_checkpoint_min_edits", DEFAULT_MIN_EDITS)
    max_wait = settings.get("git_checkpoint_max_wait_secs", DEFAULT_MAX_WAIT_SECS)
    force_at = settings.get("git_checkpoint_force_at_edits", DEFAULT_FORCE_AT_EDITS)

    should_commit = False
    if edit_count >= force_at:
        should_commit = True
    elif edit_count >= min_edits and elapsed >= max_wait:
        should_commit = True

    if not should_commit:
        return

    # 6. Spawn detached committer (log stderr for debugging)
    if not COMMITTER_SCRIPT.exists():
        return

    log_dir = PENDING_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{session_id}.log"

    try:
        stderr_fh = open(log_file, "a")
        subprocess.Popen(
            [sys.executable, str(COMMITTER_SCRIPT), str(acc_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        pass


if __name__ == "__main__":
    main()
