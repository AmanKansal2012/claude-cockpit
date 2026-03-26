"""Tests for the checkpoint PreToolUse hook."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "checkpoint.py"


def _run_hook(input_data: dict, env_override: dict | None = None, checkpoint_dir: Path | None = None, settings_file: Path | None = None):
    """Run the checkpoint hook as a subprocess with JSON on stdin."""
    env = os.environ.copy()
    # Patch paths via environment (hook reads from constants, so we monkeypatch below)
    # Instead, we'll use a wrapper approach
    code = f"""
import sys, json
sys.path.insert(0, '{HOOK_PATH.parent.parent}')

# Monkeypatch paths before importing
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{checkpoint_dir or "/tmp/test-checkpoints"}')
hook.SETTINGS_FILE = Path('{settings_file or "/tmp/nonexistent-settings.json"}')

# Feed stdin
import io
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=10,
    )
    return result


class TestCheckpointHook:
    def test_creates_snapshot_on_edit(self, tmp_path):
        """Edit tool should create a snapshot of the target file."""
        target = tmp_path / "source.py"
        target.write_text("original content")
        ckpt_dir = tmp_path / "checkpoints"

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
            "session_id": "sess-001",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        snapshot = ckpt_dir / "sess-001" / "0001" / "snapshot"
        meta = ckpt_dir / "sess-001" / "0001" / "meta.json"
        assert snapshot.exists()
        assert snapshot.read_text() == "original content"
        assert meta.exists()
        m = json.loads(meta.read_text())
        assert m["tool"] == "Edit"
        assert m["filename"] == "source.py"

    def test_creates_snapshot_on_write(self, tmp_path):
        """Write tool should snapshot existing file before overwrite."""
        target = tmp_path / "existing.py"
        target.write_text("old content")
        ckpt_dir = tmp_path / "checkpoints"

        input_data = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "new content"},
            "session_id": "sess-002",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        snapshot = ckpt_dir / "sess-002" / "0001" / "snapshot"
        assert snapshot.exists()
        assert snapshot.read_text() == "old content"

    def test_skips_when_disabled(self, tmp_path):
        """Hook should not create checkpoint when disabled in settings."""
        target = tmp_path / "file.py"
        target.write_text("content")
        ckpt_dir = tmp_path / "checkpoints"
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"checkpoints_enabled": False}))

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
            "session_id": "sess-disabled",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{settings}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        assert not (ckpt_dir / "sess-disabled").exists()

    def test_skips_nonexistent_file(self, tmp_path):
        """Hook should skip if target file doesn't exist (new file creation)."""
        ckpt_dir = tmp_path / "checkpoints"

        input_data = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "new_file.py")},
            "session_id": "sess-new",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        assert not (ckpt_dir / "sess-new").exists()

    def test_skips_non_edit_tools(self, tmp_path):
        """Hook should skip Bash, Read, Grep, etc."""
        target = tmp_path / "file.py"
        target.write_text("content")
        ckpt_dir = tmp_path / "checkpoints"

        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "sess-bash",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        assert not (ckpt_dir / "sess-bash").exists()

    def test_sequential_numbering(self, tmp_path):
        """Multiple edits should create sequentially numbered dirs."""
        target = tmp_path / "file.py"
        ckpt_dir = tmp_path / "checkpoints"

        for i in range(3):
            target.write_text(f"version {i}")
            input_data = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target)},
                "session_id": "sess-seq",
                "cwd": str(tmp_path),
            }
            code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
            subprocess.run([sys.executable, "-c", code], timeout=10)

        session_dir = ckpt_dir / "sess-seq"
        assert (session_dir / "0001" / "snapshot").exists()
        assert (session_dir / "0002" / "snapshot").exists()
        assert (session_dir / "0003" / "snapshot").exists()

    def test_updates_sessions_index(self, tmp_path):
        """Hook should create/update sessions.json index."""
        target = tmp_path / "file.py"
        target.write_text("content")
        ckpt_dir = tmp_path / "checkpoints"

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
            "session_id": "sess-idx",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.checkpoint as hook
from pathlib import Path
hook.CHECKPOINT_DIR = Path('{ckpt_dir}')
hook.SETTINGS_FILE = Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        index = json.loads((ckpt_dir / "sessions.json").read_text())
        assert len(index) == 1
        assert index[0]["session_id"] == "sess-idx"
        assert index[0]["action_count"] == 1
