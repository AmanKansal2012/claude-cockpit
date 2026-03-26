#!/usr/bin/env python3
"""Detached git committer — creates checkpoint commits from accumulated edits.

Runs as a fire-and-forget subprocess spawned by git_checkpoint.py.
Input: path to accumulator JSON file as argv[1].
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GIT_INDEX_PATH = Path.home() / ".claude" / "checkpoints" / "git-index.json"
LOCK_STALE_SECS = 30
CHECKPOINT_PREFIX = "[cockpit"
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _safe_git(cwd: str, *args: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run git command safely."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def _acquire_lock(acc_path: Path) -> int | None:
    """Acquire file lock via fcntl.flock. Returns fd if acquired, None otherwise."""
    lock_path = acc_path.with_suffix(".lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (OSError, IOError):
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        return None


def _release_lock(acc_path: Path, fd: int | None) -> None:
    """Release file lock."""
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass
    lock_path = acc_path.with_suffix(".lock")
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _update_git_index(session_id: str, cwd: str, commit_hash: str, files: list[str]) -> None:
    """Update git-index.json with new checkpoint."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        return
    GIT_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(GIT_INDEX_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = []

    entry = next((s for s in raw if s.get("session_id") == session_id), None)
    now = time.time()

    # Get branch
    rc, branch_out, _ = _safe_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_out.strip() if rc == 0 and branch_out.strip() != "HEAD" else ""

    if entry is None:
        entry = {
            "session_id": session_id,
            "cwd": cwd,
            "project": Path(cwd).name if cwd else "unknown",
            "branch": branch,
            "checkpoint_count": 0,
            "first_checkpoint_ts": now,
            "last_checkpoint_ts": now,
            "total_files_changed": 0,
        }
        raw.append(entry)

    entry["checkpoint_count"] = entry.get("checkpoint_count", 0) + 1
    entry["last_checkpoint_ts"] = now
    entry["total_files_changed"] = entry.get("total_files_changed", 0) + len(files)
    if branch:
        entry["branch"] = branch

    # Atomic write
    fd, tmp = tempfile.mkstemp(dir=GIT_INDEX_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(raw, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, GIT_INDEX_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    acc_path = Path(sys.argv[1])
    if not acc_path.exists():
        sys.exit(1)

    # 1. Acquire lock (fcntl-based, no TOCTOU)
    lock_fd = _acquire_lock(acc_path)
    if lock_fd is None:
        sys.exit(0)  # Another committer is running

    try:
        # 2. Read accumulator
        acc = json.loads(acc_path.read_text())
        cwd = acc.get("cwd", "")
        session_id = acc.get("session_id", "")
        edits = acc.get("edits", [])

        if not cwd or not edits:
            return

        # Validate session_id
        if not _SESSION_ID_RE.fullmatch(session_id):
            print(f"Invalid session_id: {session_id!r}", file=sys.stderr)
            return

        # 3. Validate: is git repo, not detached HEAD
        rc, _, _ = _safe_git(cwd, "rev-parse", "--is-inside-work-tree")
        if rc != 0:
            return

        rc, head, _ = _safe_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if rc == 0 and head.strip() == "HEAD":
            return  # Detached HEAD, skip

        # 4. Stage only edited files (validate they're inside the git worktree)
        rc, worktree_out, _ = _safe_git(cwd, "rev-parse", "--show-toplevel")
        worktree = Path(worktree_out.strip()).resolve() if rc == 0 else Path(cwd).resolve()

        unique_files = list(dict.fromkeys(e.get("file_path", "") for e in edits))
        for fp in unique_files:
            if not fp:
                continue
            try:
                resolved = Path(fp).resolve()
                # Validate file is inside git worktree (prevent staging arbitrary files)
                if not str(resolved).startswith(str(worktree)):
                    print(f"Skipping file outside worktree: {fp}", file=sys.stderr)
                    continue
                if resolved.exists():
                    _safe_git(cwd, "add", "--", str(resolved))
            except (OSError, ValueError):
                continue

        # 5. Check if anything staged
        rc, staged, _ = _safe_git(cwd, "diff", "--cached", "--name-only")
        if rc != 0 or not staged.strip():
            return  # Nothing to commit

        staged_files = staged.strip().splitlines()

        # 6. Generate commit message
        ts = time.strftime("%H:%M:%S")
        if len(staged_files) <= 5:
            file_list = ", ".join(Path(f).name for f in staged_files)
            msg = f"{CHECKPOINT_PREFIX} {ts}] checkpoint: update {file_list}"
        else:
            msg = f"{CHECKPOINT_PREFIX} {ts}] checkpoint: update {len(staged_files)} files"

        # 7. Commit
        rc, _, err = _safe_git(
            cwd, "commit", "-m", msg,
            "--author=Cockpit <cockpit@local>",
        )
        if rc != 0:
            print(f"Commit failed: {err}", file=sys.stderr)
            return

        # 8. Get SHA (full hash)
        rc, sha, _ = _safe_git(cwd, "rev-parse", "HEAD")
        sha = sha.strip() if rc == 0 else "unknown"

        # 9. Update index
        _update_git_index(session_id, cwd, sha, staged_files)

        # 10. Delete accumulator
        try:
            acc_path.unlink()
        except OSError:
            pass

    finally:
        _release_lock(acc_path, lock_fd)


if __name__ == "__main__":
    main()
