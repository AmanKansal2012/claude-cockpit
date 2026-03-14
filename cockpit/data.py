"""Data layer — reads all structured data from ~/.claude/ without writing anything."""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TASKS_DIR = CLAUDE_DIR / "tasks"
PLANS_DIR = CLAUDE_DIR / "plans"
DEBUG_DIR = CLAUDE_DIR / "debug"
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"

# Directories the file watcher should monitor
WATCH_PATHS = [TASKS_DIR, PLANS_DIR, DEBUG_DIR, STATS_FILE.parent]


def _decode_project_name(encoded: str) -> str:
    """Convert URL-encoded project dir name to readable path.

    e.g. '-Users-amankansal-go-src-github-com-LambdatestIncPrivate-go-ios'
    becomes 'go-ios'

    Strategy: find the last org-like segment (capitalized, not a common dir name),
    take everything after it. Fallback to last non-trivial segments.
    """
    if not encoded:
        return "unknown"
    parts = encoded.strip("-").split("-")
    # Common directory names that look like orgs but aren't
    not_orgs = {"Users", "Documents", "Library", "Applications", "Desktop", "Downloads"}
    # Find the last segment that looks like an org name (has uppercase, not a common dir)
    last_org_idx = -1
    for i, p in enumerate(parts):
        if any(c.isupper() for c in p) and p not in not_orgs:
            last_org_idx = i
    if last_org_idx >= 0:
        after = parts[last_org_idx + 1:]
        if after:
            return "-".join(after)
        return parts[last_org_idx]
    # Fallback: strip contiguous leading path segments (left-to-right only)
    skip = {"users", "go", "src", "github", "com", "documents", "poc"}
    # Drop "Users/username" prefix if present
    tail = parts
    if len(parts) >= 2 and parts[0].lower() == "users":
        tail = parts[2:]
    # Strip leading path-like segments, keep everything from first meaningful one
    start = 0
    for i, p in enumerate(tail):
        if p.lower() not in skip:
            start = i
            break
    else:
        start = len(tail)
    remaining = tail[start:]
    if not remaining:
        return tail[-1] if tail else (parts[-1] if parts else encoded)
    return "-".join(remaining)


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB — skip files larger than this


@dataclass
class MemoryFile:
    project: str
    name: str
    path: Path
    size: int
    lines: int = 0
    _content: str | None = field(default=None, repr=False)

    @property
    def display_name(self) -> str:
        return f"{self.project}/{self.name}"

    @property
    def content(self) -> str:
        """Lazy-load file content on first access."""
        if self._content is None:
            try:
                self._content = self.path.read_text(errors="replace")
            except OSError:
                self._content = ""
        return self._content

    def load_content(self) -> None:
        """Force-load content (e.g. before search)."""
        _ = self.content


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str  # pending, in_progress, completed
    active_form: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    session_dir: str = ""


@dataclass
class Plan:
    name: str
    path: Path
    content: str
    lines: int
    size: int
    mtime: float


@dataclass
class HistoryEntry:
    display: str
    timestamp: float
    project: str
    session_id: str


@dataclass
class DayStats:
    date: str
    messages: int = 0
    sessions: int = 0
    tool_calls: int = 0


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    size: int
    mtime: float


@dataclass
class SearchResult:
    file: MemoryFile
    line_num: int
    line: str
    context_before: str = ""
    context_after: str = ""


# ---------- Memory ----------

def get_memory_files() -> list[MemoryFile]:
    """Find all memory files across all projects.

    Content is lazy-loaded — only metadata (path, size, line count estimate)
    is read eagerly. Call .content or .load_content() to read file contents.
    Files larger than MAX_FILE_SIZE (10MB) are skipped.
    """
    files = []
    if not PROJECTS_DIR.exists():
        return files
    for memory_dir in sorted(PROJECTS_DIR.glob("*/memory")):
        project_name = _decode_project_name(memory_dir.parent.name)
        for md_file in sorted(memory_dir.glob("*.md")):
            try:
                stat = md_file.stat()
                if stat.st_size > MAX_FILE_SIZE:
                    continue  # Skip files > 10MB
                # Estimate line count from size (avoid reading content)
                # Actual count computed on first content access
                est_lines = max(1, stat.st_size // 40)  # ~40 chars/line avg
                files.append(MemoryFile(
                    project=project_name,
                    name=md_file.name,
                    path=md_file,
                    size=stat.st_size,
                    lines=est_lines,
                ))
            except OSError:
                continue
    return files


def search_memory(query: str, files: list[MemoryFile], context: int = 1) -> list[SearchResult]:
    """Full-text search across all memory files. Case-insensitive."""
    if not query.strip():
        return []
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for mf in files:
        lines = mf.content.splitlines()
        for i, line in enumerate(lines):
            if pattern.search(line):
                before = "\n".join(lines[max(0, i - context):i]) if context > 0 else ""
                after = "\n".join(lines[i + 1:i + 1 + context]) if context > 0 else ""
                results.append(SearchResult(
                    file=mf,
                    line_num=i + 1,
                    line=line,
                    context_before=before,
                    context_after=after,
                ))
    return results


def memory_summary(files: list[MemoryFile]) -> dict:
    """Quick summary stats for memory."""
    total_lines = sum(f.lines for f in files)
    total_size = sum(f.size for f in files)
    projects = len({f.project for f in files})
    return {
        "files": len(files),
        "lines": total_lines,
        "size": total_size,
        "projects": projects,
    }


# ---------- Tasks ----------

def _get_task_dirs_sorted() -> list[tuple[Path, float]]:
    """Get task directories sorted by most recent modification time."""
    if not TASKS_DIR.exists():
        return []
    task_dirs = []
    try:
        for d in TASKS_DIR.iterdir():
            if d.is_dir():
                json_files = list(d.glob("*.json"))
                if json_files:
                    latest_mtime = max(f.stat().st_mtime for f in json_files)
                    task_dirs.append((d, latest_mtime))
    except OSError:
        return []
    task_dirs.sort(key=lambda x: x[1], reverse=True)
    return task_dirs


def get_tasks() -> list[Task]:
    """Get tasks from the most recent active task session."""
    task_dirs = _get_task_dirs_sorted()
    if not task_dirs:
        return []
    return _load_tasks_from_dir(task_dirs[0][0])


def get_all_recent_tasks(limit: int = 3) -> list[Task]:
    """Get tasks from the N most recent task sessions."""
    task_dirs = _get_task_dirs_sorted()
    all_tasks = []
    for task_dir, _ in task_dirs[:limit]:
        all_tasks.extend(_load_tasks_from_dir(task_dir))
    return all_tasks


def _load_tasks_from_dir(directory: Path) -> list[Task]:
    tasks = []
    for json_file in sorted(directory.glob("*.json")):
        if json_file.name.startswith("."):
            continue
        try:
            raw = json.loads(json_file.read_text())
            tasks.append(Task(
                id=raw.get("id", json_file.stem),
                subject=raw.get("subject", "Untitled"),
                description=raw.get("description", ""),
                status=raw.get("status", "pending"),
                active_form=raw.get("activeForm", ""),
                blocks=raw.get("blocks", []),
                blocked_by=raw.get("blockedBy", []),
                session_dir=directory.name,
            ))
        except (json.JSONDecodeError, OSError):
            continue
    return tasks


def task_summary(tasks: list[Task]) -> dict:
    pending = sum(1 for t in tasks if t.status == "pending")
    active = sum(1 for t in tasks if t.status == "in_progress")
    done = sum(1 for t in tasks if t.status == "completed")
    return {"pending": pending, "active": active, "done": done, "total": len(tasks)}


# ---------- Plans ----------

def get_plans() -> list[Plan]:
    """Get all plan files, sorted by modification time (newest first)."""
    if not PLANS_DIR.exists():
        return []
    plans = []
    for md_file in PLANS_DIR.glob("*.md"):
        try:
            stat = md_file.stat()
            content = md_file.read_text(errors="replace")
            plans.append(Plan(
                name=md_file.stem,
                path=md_file,
                content=content,
                lines=content.count("\n") + 1,
                size=stat.st_size,
                mtime=stat.st_mtime,
            ))
        except OSError:
            continue
    plans.sort(key=lambda p: p.mtime, reverse=True)
    return plans


# ---------- Stats ----------

def get_stats() -> list[DayStats]:
    """Parse stats-cache.json for daily usage metrics."""
    if not STATS_FILE.exists():
        return []
    try:
        raw = json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    stats = []
    # v2 format uses "dailyActivity" as a list of dicts
    daily = raw.get("dailyActivity", [])
    if isinstance(daily, list):
        for day_data in daily:
            stats.append(DayStats(
                date=day_data.get("date", ""),
                messages=day_data.get("messageCount", 0),
                sessions=day_data.get("sessionCount", 0),
                tool_calls=day_data.get("toolCallCount", 0),
            ))
    elif isinstance(daily, dict):
        for date_str, day_data in sorted(daily.items()):
            stats.append(DayStats(
                date=date_str,
                messages=day_data.get("messageCount", 0),
                sessions=day_data.get("sessionCount", 0),
                tool_calls=day_data.get("toolCallCount", 0),
            ))
    return stats


def get_stats_overview() -> dict:
    """Get the top-level summary from stats-cache.json."""
    if not STATS_FILE.exists():
        return {}
    try:
        raw = json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        "total_sessions": raw.get("totalSessions", 0),
        "total_messages": raw.get("totalMessages", 0),
        "first_session": raw.get("firstSessionDate", ""),
        "models": raw.get("modelUsage", []),
        "longest_session": raw.get("longestSession", {}),
    }


def stats_summary(stats: list[DayStats]) -> dict:
    """Aggregate stats summary."""
    if not stats:
        return {"total_messages": 0, "total_sessions": 0, "total_tools": 0, "days": 0,
                "avg_daily_messages": 0, "last_7": []}
    total_messages = sum(s.messages for s in stats)
    total_sessions = sum(s.sessions for s in stats)
    total_tools = sum(s.tool_calls for s in stats)
    recent = stats[-7:] if len(stats) >= 7 else stats
    avg_messages = sum(s.messages for s in recent) // max(len(recent), 1)
    return {
        "total_messages": total_messages,
        "total_sessions": total_sessions,
        "total_tools": total_tools,
        "days": len(stats),
        "avg_daily_messages": avg_messages,
        "last_7": recent,
    }


# ---------- History ----------

def _tail_read_lines(filepath: Path, max_lines: int, chunk_size: int = 8192) -> list[str]:
    """Read the last max_lines lines from a file efficiently without loading it all.

    Reads raw bytes backwards in chunks, concatenates, then decodes once to
    avoid splitting multi-byte UTF-8 characters across chunk boundaries.
    Uses split("\\n") instead of splitlines() to preserve line boundaries.
    """
    try:
        file_size = filepath.stat().st_size
    except OSError:
        return []
    if file_size == 0:
        return []
    with open(filepath, "rb") as f:
        # Read backwards in chunks, accumulating raw bytes
        raw_chunks: list[bytes] = []
        offset = 0
        # Estimate: we need enough bytes for max_lines lines.
        # Typical JSONL line is ~200 bytes. Read conservatively.
        while offset < file_size:
            read_size = min(chunk_size, file_size - offset)
            offset += read_size
            f.seek(file_size - offset)
            chunk = f.read(read_size)
            raw_chunks.insert(0, chunk)
            # Check if we have enough newlines (quick byte scan)
            newline_count = sum(c.count(b"\n") for c in raw_chunks)
            if newline_count >= max_lines + 1:
                break
        # Concatenate bytes and decode once (safe for multi-byte UTF-8)
        raw = b"".join(raw_chunks)
        text = raw.decode("utf-8", errors="replace")
    # Use split("\n") to preserve empty strings at boundaries (not splitlines)
    lines = text.split("\n")
    # If we didn't read the whole file, first "line" is partial — drop it
    if offset < file_size and lines:
        lines = lines[1:]
    # Remove trailing empty string from final \n
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines[-max_lines:]


def get_history(limit: int = 200) -> list[HistoryEntry]:
    """Parse the last N entries from history.jsonl using efficient tail reading."""
    if not HISTORY_FILE.exists():
        return []
    entries = []
    lines = _tail_read_lines(HISTORY_FILE, limit)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            display = raw.get("display", raw.get("message", ""))
            if not display:
                continue
            entries.append(HistoryEntry(
                display=display[:200],
                timestamp=raw.get("timestamp", 0),
                project=_decode_project_name(raw.get("project", "")),
                session_id=raw.get("sessionId", ""),
            ))
        except json.JSONDecodeError:
            continue
    entries.reverse()  # Most recent first
    return entries


# ---------- Sessions ----------

def get_recent_sessions(limit: int = 20) -> list[SessionInfo]:
    """Get the most recent debug session files."""
    if not DEBUG_DIR.exists():
        return []
    sessions = []
    for txt_file in DEBUG_DIR.glob("*.txt"):
        try:
            stat = txt_file.stat()
            sessions.append(SessionInfo(
                session_id=txt_file.stem,
                path=txt_file,
                size=stat.st_size,
                mtime=stat.st_mtime,
            ))
        except OSError:
            continue
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions[:limit]


def estimate_context_usage(session: SessionInfo | None = None) -> dict:
    """Estimate context window usage from the latest debug transcript.

    Uses file size as a proxy — the debug transcript grows roughly proportional
    to context usage. We calibrate: ~4 chars per token, 200K context window.
    The estimate is intentionally conservative (shows higher than actual) so
    you get early warning before context fills up.
    """
    if session is None:
        sessions = get_recent_sessions(1)
        if not sessions:
            return {"percent": 0, "tokens_est": 0, "cost_est": 0.0, "active": False}
        session = sessions[0]

    age_hours = (time.time() - session.mtime) / 3600
    if age_hours > 1:
        return {"percent": 0, "tokens_est": 0, "cost_est": 0.0, "active": False}

    chars = session.size
    tokens_est = chars // 4
    context_limit = 200_000
    percent = min(100, int(tokens_est / context_limit * 100))
    # Cost estimate: Opus 4 pricing (~$15/M input, $75/M output, 70/30 split)
    input_tokens = int(tokens_est * 0.7)
    output_tokens = int(tokens_est * 0.3)
    cost_est = (input_tokens * 15 + output_tokens * 75) / 1_000_000

    return {
        "percent": percent,
        "tokens_est": tokens_est,
        "cost_est": cost_est,
        "active": True,
        "age_minutes": int(age_hours * 60),
    }


# ---------- Helpers ----------

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}K"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}M"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def time_ago(timestamp: float) -> str:
    delta = time.time() - timestamp
    if delta < 0:
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
