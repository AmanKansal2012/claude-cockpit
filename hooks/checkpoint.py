#!/usr/bin/env python3
"""PreToolUse hook: snapshot files before Claude edits them.

Reads JSON from stdin (Claude Code hook protocol), copies the target file
to ~/.claude/checkpoints/<session>/<seq>/snapshot with metadata.

Performance budget: <100ms (Python startup ~30ms + IO ~15ms).
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

CHECKPOINT_DIR = Path.home() / ".claude" / "checkpoints"
SETTINGS_FILE = Path.home() / ".claude" / "cockpit-settings.json"
MAX_FILE_SIZE = 10_000_000  # 10MB — skip huge files
CHECKPOINTED_TOOLS = ("Edit", "Write")


def main():
    # 1. Fast exit if disabled
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        if not settings.get("checkpoints_enabled", True):
            return
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass  # Default: enabled

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

    fp = Path(file_path)
    if not fp.exists() or not fp.is_file():
        return  # New file creation via Write — nothing to snapshot
    try:
        fsize = fp.stat().st_size
    except OSError:
        return
    if fsize > MAX_FILE_SIZE:
        return  # Skip huge files

    session_id = data.get("session_id", "unknown")

    # 3. Determine next sequence number
    session_dir = CHECKPOINT_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(
        (d for d in session_dir.iterdir() if d.is_dir() and d.name.isdigit()),
        key=lambda d: d.name,
    )
    seq = f"{len(existing) + 1:04d}"

    # 4. Copy file + write metadata
    action_dir = session_dir / seq
    action_dir.mkdir()
    shutil.copy2(fp, action_dir / "snapshot")

    meta = {
        "tool": tool,
        "file_path": str(fp),
        "filename": fp.name,
        "timestamp": time.time(),
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "size_before": fsize,
        "cwd": data.get("cwd", ""),
    }
    (action_dir / "meta.json").write_text(json.dumps(meta))

    # 5. Update sessions index
    _update_sessions_index(session_id, data, meta)


def _update_sessions_index(session_id, hook_data, meta):
    """Append/update session entry in the global index."""
    index_path = CHECKPOINT_DIR / "sessions.json"
    try:
        sessions = json.loads(index_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        sessions = []

    entry = next((s for s in sessions if s["session_id"] == session_id), None)
    if entry is None:
        cwd = hook_data.get("cwd", "")
        entry = {
            "session_id": session_id,
            "project": Path(cwd).name if cwd else "unknown",
            "cwd": cwd,
            "created": meta["timestamp"],
            "action_count": 0,
            "total_bytes": 0,
        }
        sessions.append(entry)

    entry["action_count"] += 1
    entry["total_bytes"] += meta["size_before"]
    entry["last_action"] = meta["timestamp"]

    # Atomic write
    fd, tmp = tempfile.mkstemp(dir=CHECKPOINT_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sessions, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, index_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
