"""Tests for cockpit.data — the read-only data layer."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from cockpit import data


# ── _tail_read_lines ──────────────────────────────────────────────────────────


class TestTailReadLines:
    """The critical tail-reader for history.jsonl."""

    def _write_file(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        f.write(content)
        f.close()
        return Path(f.name)

    def _write_bytes(self, content: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_basic_lines_with_trailing_newline(self):
        path = self._write_file("aaa\nbbb\nccc\n")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["aaa", "bbb", "ccc"]
        os.unlink(path)

    def test_basic_lines_without_trailing_newline(self):
        path = self._write_file("aaa\nbbb\nccc")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["aaa", "bbb", "ccc"]
        os.unlink(path)

    def test_tail_last_n(self):
        path = self._write_file("L1\nL2\nL3\nL4\nL5\n")
        assert data._tail_read_lines(path, 2, chunk_size=4) == ["L4", "L5"]
        os.unlink(path)

    def test_empty_file(self):
        path = self._write_file("")
        assert data._tail_read_lines(path, 10) == []
        os.unlink(path)

    def test_single_line(self):
        path = self._write_file("only line")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["only line"]
        os.unlink(path)

    def test_single_line_with_newline(self):
        path = self._write_file("only line\n")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["only line"]
        os.unlink(path)

    def test_multibyte_utf8_emoji(self):
        # Each emoji is 4 bytes; small chunk_size forces splits mid-character
        path = self._write_bytes("hello\n🎉🎊\nworld\n".encode("utf-8"))
        result = data._tail_read_lines(path, 10, chunk_size=5)
        assert result[-1] == "world"
        # Middle line should contain the emojis (decoded as one unit)
        assert "🎉" in result[1] or "🎊" in result[1]
        os.unlink(path)

    def test_chunk_boundary_at_newline(self):
        """The bug that splitlines() caused: chunks split exactly at \\n."""
        path = self._write_file("aaa\nbbb\n")
        result = data._tail_read_lines(path, 10, chunk_size=4)
        assert result == ["aaa", "bbb"], f"Got: {result}"
        os.unlink(path)

    def test_nonexistent_file(self):
        assert data._tail_read_lines(Path("/nonexistent/file.txt"), 10) == []

    def test_large_chunk_size(self):
        path = self._write_file("a\nb\nc\n")
        assert data._tail_read_lines(path, 10, chunk_size=65536) == ["a", "b", "c"]
        os.unlink(path)

    def test_jsonl_lines_are_valid(self):
        """Each line should be independently parseable JSON."""
        lines = [json.dumps({"i": i}) for i in range(20)]
        path = self._write_file("\n".join(lines) + "\n")
        result = data._tail_read_lines(path, 10, chunk_size=32)
        for line in result:
            json.loads(line)  # Should not raise
        assert len(result) == 10
        os.unlink(path)


# ── _decode_project_name ──────────────────────────────────────────────────────


class TestDecodeProjectName:
    def test_org_with_trailing_path(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-go-ios"
            )
            == "go-ios"
        )

    def test_org_as_last_segment(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-iSweep17"
            )
            == "iSweep17"
        )

    def test_multi_word_project(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-mobile-automation"
            )
            == "mobile-automation"
        )

    def test_documents_poc(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-Documents-poc-xcresult"
            )
            == "xcresult"
        )

    def test_poc_in_project_name(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-Documents-poc-patrol-segregation-poc"
            )
            == "patrol-segregation-poc"
        )

    def test_bare_username(self):
        assert data._decode_project_name("-Users-amankansal") == "amankansal"

    def test_empty_string(self):
        assert data._decode_project_name("") == "unknown"

    def test_rpi_manager(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-rpi-manager"
            )
            == "rpi-manager"
        )


# ── MemoryFile lazy loading ───────────────────────────────────────────────────


class TestMemoryFileLazy:
    def test_content_lazy_loaded(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("# Hello\nWorld\n")
        mf = data.MemoryFile(
            project="test",
            name="test.md",
            path=md,
            size=md.stat().st_size,
        )
        # _content should be None initially
        assert mf._content is None
        # Accessing .content should load it
        assert "Hello" in mf.content
        assert mf._content is not None

    def test_content_missing_file(self, tmp_path):
        mf = data.MemoryFile(
            project="test",
            name="gone.md",
            path=tmp_path / "gone.md",
            size=0,
        )
        assert mf.content == ""


# ── search_memory ─────────────────────────────────────────────────────────────


class TestSearchMemory:
    def test_basic_search(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("line one\nfoo bar baz\nline three\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        results = data.search_memory("bar", [mf])
        assert len(results) == 1
        assert results[0].line_num == 2
        assert "bar" in results[0].line

    def test_empty_query(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("content\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        assert data.search_memory("", [mf]) == []
        assert data.search_memory("   ", [mf]) == []

    def test_case_insensitive(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("Hello World\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        results = data.search_memory("hello", [mf])
        assert len(results) == 1


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_format_size(self):
        assert data.format_size(500) == "500B"
        assert data.format_size(1024) == "1.0K"
        assert data.format_size(1024 * 1024) == "1.0M"

    def test_format_number(self):
        assert data.format_number(42) == "42"
        assert data.format_number(1500) == "1.5K"
        assert data.format_number(2_500_000) == "2.5M"

    def test_time_ago(self):
        import time

        now = time.time()
        assert data.time_ago(now) == "just now"
        assert data.time_ago(now - 300) == "5m ago"
        assert data.time_ago(now - 7200) == "2h ago"
        assert data.time_ago(now - 172800) == "2d ago"
        # Future timestamps should not crash
        assert data.time_ago(now + 100) == "just now"


# ── Task loading ──────────────────────────────────────────────────────────────


class TestTasks:
    def test_load_tasks_from_dir(self, tmp_path):
        task = {
            "id": "1",
            "subject": "Test task",
            "description": "Do something",
            "status": "pending",
        }
        (tmp_path / "1.json").write_text(json.dumps(task))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1
        assert tasks[0].subject == "Test task"

    def test_corrupted_json_skipped(self, tmp_path):
        (tmp_path / "1.json").write_text("{bad json")
        (tmp_path / "2.json").write_text(json.dumps({"id": "2", "subject": "OK"}))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1
        assert tasks[0].id == "2"

    def test_hidden_files_skipped(self, tmp_path):
        (tmp_path / ".hidden.json").write_text(json.dumps({"id": "h"}))
        (tmp_path / "1.json").write_text(json.dumps({"id": "1", "subject": "Vis"}))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1

    def test_task_summary(self):
        tasks = [
            data.Task("1", "A", "", "pending"),
            data.Task("2", "B", "", "in_progress"),
            data.Task("3", "C", "", "completed"),
            data.Task("4", "D", "", "completed"),
        ]
        s = data.task_summary(tasks)
        assert s == {"pending": 1, "active": 1, "done": 2, "total": 4}
