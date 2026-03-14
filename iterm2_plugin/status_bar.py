#!/usr/bin/env python3
"""
Claude Cockpit — iTerm2 Status Bar Components.

Registers 3 status bar components in iTerm2:
  1. Memory   — file count + total lines across all Claude memory files
  2. Tasks    — pending/active/done counts from recent sessions
  3. Context  — estimated context window usage for active sessions

Also provides hotkey toggle for the cockpit TUI in a split pane.

Installation:
  This script is placed in ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/
  and runs automatically when iTerm2 starts.

Requires: iTerm2 Python Runtime (install from iTerm2 > Scripts > Manage > Install Python Runtime)
"""

import json
import sys
import time
from pathlib import Path

import iterm2

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TASKS_DIR = CLAUDE_DIR / "tasks"
DEBUG_DIR = CLAUDE_DIR / "debug"

# Path to the cockpit TUI
COCKPIT_VENV_PYTHON = Path.home() / "claude-cockpit" / ".venv" / "bin" / "python"


def _format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def get_memory_status() -> str:
    if not PROJECTS_DIR.exists():
        return "🧠 0 files"
    total_files = 0
    total_size = 0
    try:
        for memory_dir in PROJECTS_DIR.glob("*/memory"):
            for md_file in memory_dir.glob("*.md"):
                total_files += 1
                total_size += md_file.stat().st_size
    except OSError:
        pass
    size_str = _format_number(total_size) + "B" if total_size < 1024 else f"{total_size / 1024:.1f}K"
    return f"🧠 {total_files} · {size_str}"


def get_tasks_status() -> str:
    if not TASKS_DIR.exists():
        return "📋 —"
    # Find the most recent task dir
    best_dir = None
    best_mtime = 0.0
    try:
        for d in TASKS_DIR.iterdir():
            if not d.is_dir():
                continue
            json_files = list(d.glob("*.json"))
            if json_files:
                dir_mtime = max(f.stat().st_mtime for f in json_files)
                if dir_mtime > best_mtime:
                    best_mtime = dir_mtime
                    best_dir = d
    except OSError:
        return "📋 —"
    if not best_dir:
        return "📋 —"
    pending = active = done = 0
    for jf in best_dir.glob("*.json"):
        if jf.name.startswith("."):
            continue
        try:
            st = json.loads(jf.read_text()).get("status", "pending")
            if st == "pending":
                pending += 1
            elif st == "in_progress":
                active += 1
            elif st == "completed":
                done += 1
        except (json.JSONDecodeError, OSError):
            continue
    parts = []
    if active:
        parts.append(f"⏳{active}")
    if pending:
        parts.append(f"○{pending}")
    if done:
        parts.append(f"✅{done}")
    return f"📋 {' '.join(parts)}" if parts else "📋 —"


def get_context_status() -> str:
    if not DEBUG_DIR.exists():
        return ""
    best_mtime = 0.0
    best_size = 0
    try:
        for txt in DEBUG_DIR.glob("*.txt"):
            stat = txt.stat()
            if stat.st_mtime > best_mtime:
                best_mtime = stat.st_mtime
                best_size = stat.st_size
    except OSError:
        return ""
    if not best_mtime:
        return ""
    age_hours = (time.time() - best_mtime) / 3600
    if age_hours > 1:
        return "💤 idle"
    tokens_est = best_size // 4
    percent = min(100, int(tokens_est / 200_000 * 100))
    filled = int(10 * percent / 100)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{bar} {percent}%"


_cockpit_session_id: str | None = None


async def main(connection):
    global _cockpit_session_id

    # ---- Status Bar Component: Memory ----
    mem_component = iterm2.StatusBarComponent(
        short_description="Claude Memory",
        detailed_description="Claude Code memory file count and total lines",
        knobs=[],
        exemplar="🧠 14 · 2.6K lines",
        update_cadence=5,
        identifier="com.claude.cockpit.memory",
    )

    @iterm2.StatusBarRPC
    async def memory_coro(knobs):
        return get_memory_status()

    await mem_component.async_register(connection, memory_coro)

    # ---- Status Bar Component: Tasks ----
    tasks_component = iterm2.StatusBarComponent(
        short_description="Claude Tasks",
        detailed_description="Active/pending/done tasks from Claude Code sessions",
        knobs=[],
        exemplar="📋 ⏳1 ○2 ✅3",
        update_cadence=3,
        identifier="com.claude.cockpit.tasks",
    )

    @iterm2.StatusBarRPC
    async def tasks_coro(knobs):
        return get_tasks_status()

    await tasks_component.async_register(connection, tasks_coro)

    # ---- Status Bar Component: Context ----
    ctx_component = iterm2.StatusBarComponent(
        short_description="Claude Context",
        detailed_description="Estimated context window usage for active Claude session",
        knobs=[],
        exemplar="██████░░░░ 60%",
        update_cadence=5,
        identifier="com.claude.cockpit.context",
    )

    @iterm2.StatusBarRPC
    async def context_coro(knobs):
        return get_context_status()

    await ctx_component.async_register(connection, context_coro)

    # Keep running
    await connection.async_dispatch_until_future(iterm2.util.async_wait_forever())


iterm2.run_forever(main)
