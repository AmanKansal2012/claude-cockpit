"""Tests for git checkpoint functionality."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cockpit import data

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "git_checkpoint.py"
COMMITTER_PATH = Path(__file__).parent.parent / "hooks" / "git_committer.py"


def _init_git_repo(path: Path) -> None:
    """Initialize a real git repo in path with an initial commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, check=True)
    # Initial commit
    (path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, capture_output=True, check=True)


# ── _safe_git ──────────────────────────────────────────────────────────────


class TestSafeGit:
    def test_success(self, tmp_path):
        _init_git_repo(tmp_path)
        rc, out, err = data._safe_git(str(tmp_path), "rev-parse", "--is-inside-work-tree")
        assert rc == 0
        assert "true" in out

    def test_bad_cwd(self, tmp_path):
        rc, out, err = data._safe_git(str(tmp_path / "nonexistent"), "status")
        assert rc != 0

    def test_timeout(self, tmp_path):
        _init_git_repo(tmp_path)
        # Use a very short timeout — git status should still succeed quickly
        rc, out, err = data._safe_git(str(tmp_path), "status", timeout=1)
        assert rc == 0


# ── is_git_repo ────────────────────────────────────────────────────────────


class TestIsGitRepo:
    def test_git_dir_returns_true(self, tmp_path):
        _init_git_repo(tmp_path)
        assert data.is_git_repo(str(tmp_path)) is True

    def test_non_git_returns_false(self, tmp_path):
        assert data.is_git_repo(str(tmp_path)) is False


# ── git_get_branch ─────────────────────────────────────────────────────────


class TestGitGetBranch:
    def test_returns_branch_name(self, tmp_path):
        _init_git_repo(tmp_path)
        branch = data.git_get_branch(str(tmp_path))
        # Git default branch is "main" or "master"
        assert branch in ("main", "master")

    def test_empty_on_detached(self, tmp_path):
        _init_git_repo(tmp_path)
        # Get current commit and detach
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        )
        sha = result.stdout.strip()
        subprocess.run(
            ["git", "checkout", sha], cwd=tmp_path,
            capture_output=True, check=True,
        )
        assert data.git_get_branch(str(tmp_path)) == ""


# ── git_is_detached_head ───────────────────────────────────────────────────


class TestGitIsDetachedHead:
    def test_not_detached(self, tmp_path):
        _init_git_repo(tmp_path)
        assert data.git_is_detached_head(str(tmp_path)) is False

    def test_detached(self, tmp_path):
        _init_git_repo(tmp_path)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        )
        sha = result.stdout.strip()
        subprocess.run(
            ["git", "checkout", sha], cwd=tmp_path,
            capture_output=True, check=True,
        )
        assert data.git_is_detached_head(str(tmp_path)) is True


# ── git_has_uncommitted_changes ────────────────────────────────────────────


class TestGitHasUncommittedChanges:
    def test_clean(self, tmp_path):
        _init_git_repo(tmp_path)
        assert data.git_has_uncommitted_changes(str(tmp_path)) is False

    def test_dirty(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "new.txt").write_text("hello")
        assert data.git_has_uncommitted_changes(str(tmp_path)) is True


# ── git_create_manual_checkpoint ───────────────────────────────────────────


class TestGitCreateManualCheckpoint:
    def test_creates_commit(self, tmp_path):
        _init_git_repo(tmp_path)
        # Make a change
        (tmp_path / "README.md").write_text("# Updated\n")
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", tmp_path / "git-index.json"):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "git-pending"):
                ok, msg = data.git_create_manual_checkpoint(
                    str(tmp_path), "test checkpoint", "sess-test"
                )
        assert ok is True
        assert "test checkpoint" in msg

        # Verify commit exists
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert "[cockpit manual" in result.stdout

    def test_fails_with_no_changes(self, tmp_path):
        _init_git_repo(tmp_path)
        ok, msg = data.git_create_manual_checkpoint(
            str(tmp_path), "nothing", "sess-test"
        )
        assert ok is False
        assert "No changes" in msg

    def test_fails_on_detached_head(self, tmp_path):
        _init_git_repo(tmp_path)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        )
        subprocess.run(
            ["git", "checkout", result.stdout.strip()], cwd=tmp_path,
            capture_output=True, check=True,
        )
        (tmp_path / "README.md").write_text("# Changed\n")
        ok, msg = data.git_create_manual_checkpoint(
            str(tmp_path), "test", "sess-test"
        )
        assert ok is False
        assert "detached" in msg.lower()

    def test_fails_on_non_git(self, tmp_path):
        ok, msg = data.git_create_manual_checkpoint(
            str(tmp_path), "test", "sess-test"
        )
        assert ok is False
        assert "git" in msg.lower()


# ── git_rollback_to_checkpoint ─────────────────────────────────────────────


class TestGitRollback:
    def test_rollback_restores_content(self, tmp_path):
        _init_git_repo(tmp_path)
        # Make a change and commit
        (tmp_path / "README.md").write_text("# Version 1\n")
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit 00:00:00] v1"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        v1_sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Another change
        (tmp_path / "README.md").write_text("# Version 2\n")
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit 00:00:01] v2"],
            cwd=tmp_path, capture_output=True, check=True,
        )

        # Rollback to v1
        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), v1_sha)
        assert ok is True
        assert "Safety tag" in msg
        assert (tmp_path / "README.md").read_text() == "# Version 1\n"

    def test_creates_safety_tag(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Changed\n")
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit] test"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD~1"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), sha)
        assert ok is True

        # Check safety tag exists
        result = subprocess.run(
            ["git", "tag", "-l", "cockpit-safety-*"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert "cockpit-safety-" in result.stdout

    def test_fails_on_non_git(self, tmp_path):
        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), "abc123")
        assert ok is False


# ── git_squash_checkpoints ─────────────────────────────────────────────────


class TestGitSquash:
    def test_squashes_commits(self, tmp_path):
        _init_git_repo(tmp_path)

        # Create 3 cockpit commits
        for i in range(3):
            (tmp_path / f"file{i}.txt").write_text(f"content {i}")
            subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"[cockpit 00:00:0{i}] update file{i}"],
                cwd=tmp_path, capture_output=True, check=True,
            )

        # Get hashes
        from_sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD~2"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        to_sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ok, msg = data.git_squash_checkpoints(
            str(tmp_path), from_sha, to_sha, "all changes"
        )
        assert ok is True
        assert "Safety tag" in msg

        # Verify single commit replaced the 3
        result = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path,
            capture_output=True, text=True,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        # Should be: initial + squashed = 2 commits
        assert len(lines) == 2
        assert "squash" in lines[0].lower()

    def test_fails_with_dirty_tree(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "dirty.txt").write_text("uncommitted")
        # Use valid hex hashes (validation happens before dirty check)
        ok, msg = data.git_squash_checkpoints(str(tmp_path), "abcd1234", "abcd5678", "test")
        assert ok is False
        assert "uncommitted" in msg.lower()

    def test_fails_with_invalid_hash(self, tmp_path):
        _init_git_repo(tmp_path)
        ok, msg = data.git_squash_checkpoints(str(tmp_path), "not-hex!", "also-bad!", "test")
        assert ok is False
        assert "invalid" in msg.lower()

    def test_recovers_on_failure(self, tmp_path):
        _init_git_repo(tmp_path)
        # Try squash with valid-looking but non-existent hash
        ok, msg = data.git_squash_checkpoints(
            str(tmp_path), "deadbeef", "cafebabe", "test"
        )
        assert ok is False
        # Repo should still be functional
        rc, _, _ = data._safe_git(str(tmp_path), "status")
        assert rc == 0


# ── get_git_checkpoint_diff ────────────────────────────────────────────────


class TestGitCheckpointDiff:
    def test_returns_diff(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Changed\n")
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit] test"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        diff = data.get_git_checkpoint_diff(str(tmp_path), sha)
        assert "Changed" in diff or "diff" in diff.lower()

    def test_non_git_returns_message(self, tmp_path):
        diff = data.get_git_checkpoint_diff(str(tmp_path), "abcd")
        assert "not a git repo" in diff.lower()


# ── get_git_checkpoint_sessions ────────────────────────────────────────────


class TestGitCheckpointSessions:
    def test_empty_index_returns_empty(self, tmp_path):
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", tmp_path / "nonexistent.json"):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "nonexistent"):
                sessions = data.get_git_checkpoint_sessions()
        assert sessions == []

    def test_loads_from_index(self, tmp_path):
        index_file = tmp_path / "git-index.json"
        index_file.write_text(json.dumps([{
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "project": "test",
            "branch": "main",
            "checkpoint_count": 3,
            "first_checkpoint_ts": 1000,
            "last_checkpoint_ts": 2000,
            "total_files_changed": 5,
        }]))
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "nonexistent"):
                sessions = data.get_git_checkpoint_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-1"
        assert sessions[0].checkpoint_count == 3

    def test_detects_pending_edits(self, tmp_path):
        index_file = tmp_path / "git-index.json"
        index_file.write_text(json.dumps([{
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "project": "test",
            "branch": "main",
            "checkpoint_count": 1,
            "first_checkpoint_ts": 1000,
            "last_checkpoint_ts": 2000,
            "total_files_changed": 2,
        }]))
        pending_dir = tmp_path / "git-pending"
        pending_dir.mkdir()
        (pending_dir / "sess-1.json").write_text(json.dumps({
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "first_edit_ts": 3000,
            "last_edit_ts": 3001,
            "edits": [{"tool": "Edit", "file_path": "/a.py", "timestamp": 3000}],
        }))
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", pending_dir):
                sessions = data.get_git_checkpoint_sessions()
        assert len(sessions) == 1
        assert sessions[0].has_pending_edits is True
        assert sessions[0].pending_edit_count == 1


# ── git settings ───────────────────────────────────────────────────────────


class TestGitSettings:
    def test_default_enabled(self, tmp_path):
        with patch.object(data, "SETTINGS_FILE", tmp_path / "nonexistent.json"):
            assert data.is_git_checkpoints_enabled() is True

    def test_disabled(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"git_checkpoints_enabled": False}))
        with patch.object(data, "SETTINGS_FILE", settings):
            assert data.is_git_checkpoints_enabled() is False

    def test_toggle(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({}))
        with patch.object(data, "SETTINGS_FILE", settings):
            new_state, msg = data.toggle_git_checkpoints()
            assert new_state is False
            assert "disabled" in msg
            new_state, msg = data.toggle_git_checkpoints()
            assert new_state is True
            assert "enabled" in msg


# ── git_committer ──────────────────────────────────────────────────────────


class TestGitCommitter:
    def test_commits_accumulated_edits(self, tmp_path):
        _init_git_repo(tmp_path)

        # Create some files and stage them
        (tmp_path / "file1.py").write_text("print('hello')")
        (tmp_path / "file2.py").write_text("print('world')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add files"],
            cwd=tmp_path, capture_output=True, check=True,
        )

        # Modify files (simulate edits)
        (tmp_path / "file1.py").write_text("print('hello updated')")
        (tmp_path / "file2.py").write_text("print('world updated')")

        # Create accumulator
        acc_dir = tmp_path / "pending"
        acc_dir.mkdir()
        acc_path = acc_dir / "sess-test.json"
        acc_path.write_text(json.dumps({
            "session_id": "sess-test",
            "cwd": str(tmp_path),
            "is_git": True,
            "first_edit_ts": time.time() - 60,
            "last_edit_ts": time.time(),
            "edits": [
                {"tool": "Edit", "file_path": str(tmp_path / "file1.py"), "timestamp": time.time()},
                {"tool": "Edit", "file_path": str(tmp_path / "file2.py"), "timestamp": time.time()},
            ],
        }))

        # Run committer with patched index path
        code = f"""
import sys, os
sys.path.insert(0, '{COMMITTER_PATH.parent.parent}')
import hooks.git_committer as committer
committer.GIT_INDEX_PATH = __import__('pathlib').Path('{tmp_path / "git-index.json"}')
sys.argv = ['committer', '{acc_path}']
committer.main()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=15,
        )

        # Verify commit was created
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert "[cockpit" in log_result.stdout

        # Verify accumulator was deleted
        assert not acc_path.exists()

        # Verify index was updated
        index = json.loads((tmp_path / "git-index.json").read_text())
        assert len(index) == 1
        assert index[0]["session_id"] == "sess-test"

    def test_skips_detached_head(self, tmp_path):
        _init_git_repo(tmp_path)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", sha], cwd=tmp_path,
            capture_output=True, check=True,
        )

        (tmp_path / "README.md").write_text("# Changed\n")

        acc_dir = tmp_path / "pending"
        acc_dir.mkdir()
        acc_path = acc_dir / "sess-test.json"
        acc_path.write_text(json.dumps({
            "session_id": "sess-test",
            "cwd": str(tmp_path),
            "edits": [{"tool": "Edit", "file_path": str(tmp_path / "README.md"), "timestamp": time.time()}],
        }))

        code = f"""
import sys
sys.path.insert(0, '{COMMITTER_PATH.parent.parent}')
import hooks.git_committer as committer
committer.GIT_INDEX_PATH = __import__('pathlib').Path('{tmp_path / "git-index.json"}')
sys.argv = ['committer', '{acc_path}']
committer.main()
"""
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=15,
        )

        # Accumulator should still exist (not committed, not deleted)
        # Actually, the committer exits early and doesn't delete — but the lock is released
        # so let's just verify no cockpit commit was made
        log_result = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path,
            capture_output=True, text=True,
        )
        assert "[cockpit" not in log_result.stdout


# ── git_checkpoint hook ────────────────────────────────────────────────────


class TestGitCheckpointHook:
    def test_creates_accumulator(self, tmp_path):
        _init_git_repo(tmp_path)
        pending_dir = tmp_path / "pending"

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "README.md")},
            "session_id": "sess-hook",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
hook.COMMITTER_SCRIPT = __import__('pathlib').Path('{tmp_path / "nonexistent_committer.py"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        acc_path = pending_dir / "sess-hook.json"
        assert acc_path.exists()
        acc = json.loads(acc_path.read_text())
        assert acc["session_id"] == "sess-hook"
        assert len(acc["edits"]) == 1

    def test_appends_on_multiple_edits(self, tmp_path):
        _init_git_repo(tmp_path)
        pending_dir = tmp_path / "pending"

        for i in range(3):
            input_data = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(tmp_path / f"file{i}.py")},
                "session_id": "sess-multi",
                "cwd": str(tmp_path),
            }
            code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
hook.COMMITTER_SCRIPT = __import__('pathlib').Path('{tmp_path / "nonexistent_committer.py"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
            subprocess.run([sys.executable, "-c", code], timeout=10)

        acc = json.loads((pending_dir / "sess-multi.json").read_text())
        assert len(acc["edits"]) == 3

    def test_skips_when_disabled(self, tmp_path):
        _init_git_repo(tmp_path)
        pending_dir = tmp_path / "pending"
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"git_checkpoints_enabled": False}))

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "README.md")},
            "session_id": "sess-disabled",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{settings}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        assert not (pending_dir / "sess-disabled.json").exists()

    def test_skips_non_edit_tools(self, tmp_path):
        _init_git_repo(tmp_path)
        pending_dir = tmp_path / "pending"

        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "sess-bash",
            "cwd": str(tmp_path),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        assert not pending_dir.exists() or not list(pending_dir.glob("*.json"))

    def test_skips_non_git_repo(self, tmp_path):
        """Non-git dirs should not create accumulators."""
        pending_dir = tmp_path / "pending"
        non_git_dir = tmp_path / "not-a-repo"
        non_git_dir.mkdir()

        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(non_git_dir / "file.py")},
            "session_id": "sess-nongit",
            "cwd": str(non_git_dir),
        }

        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        if pending_dir.exists():
            acc_files = list(pending_dir.glob("*.json"))
            # If accumulator was created, it should have is_git=False
            # and should not have grown
            for f in acc_files:
                acc = json.loads(f.read_text())
                assert acc.get("is_git") is False


# ── _update_git_index ──────────────────────────────────────────────────────


class TestUpdateGitIndex:
    def test_creates_new_index(self, tmp_path):
        _init_git_repo(tmp_path)
        index_file = tmp_path / "git-index.json"
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                data._update_git_index("sess-1", str(tmp_path), "abc123", "msg", ["a.py"])
        raw = json.loads(index_file.read_text())
        assert len(raw) == 1
        assert raw[0]["session_id"] == "sess-1"
        assert raw[0]["checkpoint_count"] == 1

    def test_updates_existing_entry(self, tmp_path):
        _init_git_repo(tmp_path)
        index_file = tmp_path / "git-index.json"
        index_file.write_text(json.dumps([{
            "session_id": "sess-1",
            "cwd": str(tmp_path),
            "project": "test",
            "branch": "main",
            "checkpoint_count": 1,
            "first_checkpoint_ts": 1000,
            "last_checkpoint_ts": 1000,
            "total_files_changed": 2,
        }]))
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                data._update_git_index("sess-1", str(tmp_path), "def456", "msg", ["b.py", "c.py"])
        raw = json.loads(index_file.read_text())
        assert len(raw) == 1
        assert raw[0]["checkpoint_count"] == 2
        assert raw[0]["total_files_changed"] == 4


# ── get_git_recent_commits ─────────────────────────────────────────────────


class TestGetGitRecentCommits:
    def test_returns_commits(self, tmp_path):
        _init_git_repo(tmp_path)
        # Create a cockpit commit
        (tmp_path / "README.md").write_text("# Updated\n")
        subprocess.run(["git", "add", "-u"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit 12:00:00] update readme"],
            cwd=tmp_path, capture_output=True, check=True,
        )

        commits = data.get_git_recent_commits(str(tmp_path))
        assert len(commits) >= 2  # initial + cockpit
        assert any(c.is_cockpit for c in commits)
        assert any(not c.is_cockpit for c in commits)

    def test_non_git_returns_empty(self, tmp_path):
        assert data.get_git_recent_commits(str(tmp_path)) == []


# ── Validation boundary tests ────────────────────────────────────────────


class TestValidateSessionId:
    def test_valid_uuid(self):
        assert data._validate_session_id("abc-123_DEF") is True

    def test_single_char(self):
        assert data._validate_session_id("a") is True

    def test_max_length_128(self):
        assert data._validate_session_id("a" * 128) is True

    def test_exceeds_128(self):
        assert data._validate_session_id("a" * 129) is False

    def test_empty(self):
        assert data._validate_session_id("") is False

    def test_path_traversal(self):
        assert data._validate_session_id("../etc/passwd") is False
        assert data._validate_session_id("..%2f..%2f") is False

    def test_null_byte(self):
        assert data._validate_session_id("abc\x00def") is False

    def test_spaces(self):
        assert data._validate_session_id("abc def") is False

    def test_dots_only(self):
        assert data._validate_session_id("..") is False
        assert data._validate_session_id(".") is False


class TestValidateCommitHash:
    def test_min_4_chars(self):
        assert data._validate_commit_hash("abcd") is True

    def test_3_chars_fails(self):
        assert data._validate_commit_hash("abc") is False

    def test_max_40_chars(self):
        assert data._validate_commit_hash("a" * 40) is True

    def test_41_chars_fails(self):
        assert data._validate_commit_hash("a" * 41) is False

    def test_full_sha(self):
        assert data._validate_commit_hash("d3adb33f" * 5) is True

    def test_non_hex_rejected(self):
        assert data._validate_commit_hash("ghijklmn") is False
        assert data._validate_commit_hash("abcd!@#$") is False

    def test_empty(self):
        assert data._validate_commit_hash("") is False

    def test_mixed_case(self):
        assert data._validate_commit_hash("aBcDeF12") is True


# ── Edge case tests ──────────────────────────────────────────────────────


class TestEdgeCases:
    def test_rollback_invalid_hash(self, tmp_path):
        _init_git_repo(tmp_path)
        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), "xyz!")
        assert ok is False
        assert "invalid" in msg.lower()

    def test_rollback_nonexistent_hash(self, tmp_path):
        _init_git_repo(tmp_path)
        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), "deadbeef")
        assert ok is False
        assert "cannot resolve" in msg.lower()

    def test_rollback_non_git(self, tmp_path):
        ok, msg = data.git_rollback_to_checkpoint(str(tmp_path), "abcd1234")
        assert ok is False

    def test_squash_reversed_hashes(self, tmp_path):
        """Squash with from > to (reversed order) should fail or handle gracefully."""
        _init_git_repo(tmp_path)
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text(f"v{i}")
            subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"[cockpit 00:0{i}:00] update f{i}"],
                cwd=tmp_path, capture_output=True, check=True,
            )
        oldest = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD~2"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        newest = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Reversed: newest as "from", oldest as "to"
        ok, msg = data.git_squash_checkpoints(str(tmp_path), newest, oldest, "reversed")
        # Should either fail gracefully or produce valid result
        # After: repo should still be functional
        rc, _, _ = data._safe_git(str(tmp_path), "status")
        assert rc == 0

    def test_squash_refuses_user_commits_in_range(self, tmp_path):
        """Squash must refuse when user commits are interleaved."""
        _init_git_repo(tmp_path)
        # cockpit commit
        (tmp_path / "a.txt").write_text("a")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit 00:00:00] update a"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        from_sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # USER commit (not cockpit)
        (tmp_path / "b.txt").write_text("b")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: user change"],
            cwd=tmp_path, capture_output=True, check=True,
        )

        # cockpit commit
        (tmp_path / "c.txt").write_text("c")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[cockpit 00:00:01] update c"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        to_sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ok, msg = data.git_squash_checkpoints(str(tmp_path), from_sha, to_sha, "squash all")
        assert ok is False
        assert "user commit" in msg.lower()

    def test_manual_checkpoint_sanitizes_newlines(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Changed\n")
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", tmp_path / "git-index.json"):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                ok, msg = data.git_create_manual_checkpoint(
                    str(tmp_path), "line1\nline2\rline3", "sess-test"
                )
        assert ok is True
        # Verify no newlines in commit message
        result = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert "\n" not in result.stdout.strip()

    def test_manual_checkpoint_truncates_long_message(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Changed\n")
        long_msg = "x" * 500
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", tmp_path / "git-index.json"):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                ok, msg = data.git_create_manual_checkpoint(
                    str(tmp_path), long_msg, "sess-test"
                )
        assert ok is True
        result = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        # [cockpit manual HH:MM:SS] prefix + 200 chars max
        assert len(result.stdout.strip()) <= 230

    def test_manual_checkpoint_invalid_session_id(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Changed\n")
        ok, msg = data.git_create_manual_checkpoint(
            str(tmp_path), "test", "../../../etc"
        )
        assert ok is False
        assert "invalid" in msg.lower()

    def test_flush_invalid_session_id(self):
        ok, msg = data.flush_pending_git_checkpoint("../../../etc")
        assert ok is False
        assert "invalid" in msg.lower()

    def test_diff_invalid_hash(self, tmp_path):
        _init_git_repo(tmp_path)
        diff = data.get_git_checkpoint_diff(str(tmp_path), "not-hex!")
        assert "invalid" in diff.lower()

    def test_diff_nonexistent_hash(self, tmp_path):
        _init_git_repo(tmp_path)
        diff = data.get_git_checkpoint_diff(str(tmp_path), "deadbeef")
        assert "unknown" in diff.lower()

    def test_update_git_index_rejects_invalid_session(self, tmp_path):
        """_update_git_index should silently reject bad session IDs."""
        index_file = tmp_path / "git-index.json"
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                data._update_git_index("../../../etc", str(tmp_path), "abc", "msg", ["a.py"])
        # Index file should NOT be created
        assert not index_file.exists()

    def test_update_git_index_caps_sessions(self, tmp_path):
        """Index should prune oldest sessions when exceeding limit."""
        _init_git_repo(tmp_path)
        index_file = tmp_path / "git-index.json"
        # Create index with MAX sessions
        sessions = []
        for i in range(data.GIT_MAX_INDEX_SESSIONS):
            sessions.append({
                "session_id": f"sess-{i}",
                "cwd": str(tmp_path),
                "project": "test",
                "branch": "main",
                "checkpoint_count": 1,
                "first_checkpoint_ts": float(i),
                "last_checkpoint_ts": float(i),
                "total_files_changed": 1,
            })
        index_file.write_text(json.dumps(sessions))

        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "pending"):
                data._update_git_index("sess-new", str(tmp_path), "abc", "msg", ["a.py"])

        raw = json.loads(index_file.read_text())
        assert len(raw) <= data.GIT_MAX_INDEX_SESSIONS
        # New session should exist
        assert any(s["session_id"] == "sess-new" for s in raw)

    def test_sessions_loads_with_stronger_assertions(self, tmp_path):
        """Verify all fields of loaded GitCheckpointSession."""
        index_file = tmp_path / "git-index.json"
        index_file.write_text(json.dumps([{
            "session_id": "sess-full",
            "cwd": "/tmp/project",
            "project": "project",
            "branch": "feature-x",
            "checkpoint_count": 7,
            "first_checkpoint_ts": 1000.5,
            "last_checkpoint_ts": 2000.5,
            "total_files_changed": 15,
        }]))
        with patch.object(data, "GIT_CHECKPOINTS_INDEX", index_file):
            with patch.object(data, "GIT_CHECKPOINTS_DIR", tmp_path / "nonexistent"):
                sessions = data.get_git_checkpoint_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "sess-full"
        assert s.cwd == "/tmp/project"
        assert s.project == "project"
        assert s.branch == "feature-x"
        assert s.checkpoint_count == 7
        assert isinstance(s.checkpoint_count, int)
        assert s.first_checkpoint_ts == 1000.5
        assert s.last_checkpoint_ts == 2000.5
        assert s.total_files_changed == 15
        assert s.is_git_repo is True
        assert s.has_pending_edits is False
        assert s.pending_edit_count == 0

    def test_committer_validates_file_outside_worktree(self, tmp_path):
        """Committer should reject files outside the git worktree."""
        _init_git_repo(tmp_path)
        outside_file = tmp_path.parent / "outside.txt"
        outside_file.write_text("malicious")

        acc_dir = tmp_path / "pending"
        acc_dir.mkdir()
        acc_path = acc_dir / "sess-test.json"
        acc_path.write_text(json.dumps({
            "session_id": "sess-test",
            "cwd": str(tmp_path),
            "edits": [{"file_path": str(outside_file), "timestamp": time.time()}],
        }))

        code = f"""
import sys
sys.path.insert(0, '{COMMITTER_PATH.parent.parent}')
import hooks.git_committer as committer
committer.GIT_INDEX_PATH = __import__('pathlib').Path('{tmp_path / "git-index.json"}')
sys.argv = ['committer', '{acc_path}']
committer.main()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=15,
        )

        # Should NOT have committed (nothing staged from outside worktree)
        log_result = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path,
            capture_output=True, text=True,
        )
        assert "[cockpit" not in log_result.stdout

    def test_hook_invalidates_git_cache_on_cwd_change(self, tmp_path):
        """Hook should re-check is_git when cwd changes between two git repos."""
        git_dir1 = tmp_path / "git-project-1"
        git_dir1.mkdir()
        _init_git_repo(git_dir1)

        git_dir2 = tmp_path / "git-project-2"
        git_dir2.mkdir()
        _init_git_repo(git_dir2)

        pending_dir = tmp_path / "pending"

        # First call: git repo 1
        input_data1 = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(git_dir1 / "README.md")},
            "session_id": "sess-switch",
            "cwd": str(git_dir1),
        }
        code = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
hook.COMMITTER_SCRIPT = __import__('pathlib').Path('{tmp_path / "nonexistent_committer.py"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data1)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code], timeout=10)

        acc = json.loads((pending_dir / "sess-switch.json").read_text())
        assert acc["is_git"] is True
        assert acc["cwd"] == str(git_dir1)

        # Second call: different git repo (cwd changed)
        input_data2 = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(git_dir2 / "README.md")},
            "session_id": "sess-switch",
            "cwd": str(git_dir2),
        }
        code2 = f"""
import sys, json, io
sys.path.insert(0, '{HOOK_PATH.parent.parent}')
import hooks.git_checkpoint as hook
hook.PENDING_DIR = __import__('pathlib').Path('{pending_dir}')
hook.SETTINGS_FILE = __import__('pathlib').Path('{tmp_path / "nonexistent.json"}')
hook.COMMITTER_SCRIPT = __import__('pathlib').Path('{tmp_path / "nonexistent_committer.py"}')
sys.stdin = io.StringIO(json.dumps({json.dumps(input_data2)}))
hook.main()
"""
        subprocess.run([sys.executable, "-c", code2], timeout=10)

        # Accumulator should now reflect new cwd (cache was invalidated)
        acc2 = json.loads((pending_dir / "sess-switch.json").read_text())
        assert acc2["cwd"] == str(git_dir2)
        assert acc2["is_git"] is True
        assert len(acc2["edits"]) == 2  # Both edits accumulated
