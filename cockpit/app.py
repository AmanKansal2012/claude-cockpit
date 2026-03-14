"""Claude Cockpit — Textual TUI application."""

from __future__ import annotations

import threading
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from cockpit import data


SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[int], width: int = 30) -> str:
    """Render a sparkline string from integer values."""
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    return "".join(
        SPARKLINE_CHARS[int((v - mn) / rng * (len(SPARKLINE_CHARS) - 1))]
        for v in sampled
    )


def gauge_bar(percent: int, width: int = 20) -> str:
    """Render a gauge bar like ██████░░░░."""
    filled = int(width * percent / 100)
    empty = width - filled
    if percent >= 80:
        color = "red"
    elif percent >= 50:
        color = "yellow"
    else:
        color = "green"
    return f"[{color}]{'█' * filled}{'░' * empty}[/{color}] {percent}%"


# ============================================================
# Help Screen
# ============================================================

HELP_TEXT = """\
[bold]Claude Cockpit[/bold] — X-ray vision for your Claude Code brain

[bold cyan]Navigation[/bold cyan]
  [bold]m[/bold]  Memory tab        [bold]t[/bold]  Tasks tab
  [bold]p[/bold]  Plans tab         [bold]s[/bold]  Stats tab
  [bold]h[/bold]  History tab       [bold]/[/bold]  Focus search

[bold cyan]Actions[/bold cyan]
  [bold]r[/bold]  Refresh all data from disk
  [bold]Esc[/bold]  Unfocus search input
  [bold]q[/bold]  Quit cockpit (Claude keeps running)
  [bold]?[/bold]  Toggle this help screen

[bold cyan]Memory Search[/bold cyan]
  Type in the search box to instantly search across
  all your Claude memory files. Results update live
  with 200ms debounce. Press Esc to clear focus.

[bold cyan]What this shows[/bold cyan]
  [bold]Memory[/bold]   All memory files across projects
  [bold]Tasks[/bold]    Active/pending/done from recent sessions
  [bold]Plans[/bold]    All plan files with markdown preview
  [bold]Stats[/bold]    Usage metrics, model breakdown, sparklines
  [bold]History[/bold]  Searchable command history

[bold cyan]Auto-refresh[/bold cyan]
  File changes in ~/.claude/ are detected automatically.
  The context gauge updates every 5 seconds.

[dim]Press Esc or ? to close this screen[/dim]
"""


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="help-modal"):
                yield Static(HELP_TEXT, id="help-content")

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-modal {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #help-content {
        height: auto;
    }
    """


# ============================================================
# Memory Tab
# ============================================================

class MemoryTab(TabPane):
    """Memory explorer with full-text search and debounce."""

    def __init__(self) -> None:
        super().__init__("Memory", id="tab-memory")
        self._memory_files: list[data.MemoryFile] = []
        self._selected_file: data.MemoryFile | None = None
        self._search_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="memory-container"):
            with Vertical(id="memory-sidebar"):
                with Vertical(id="memory-search"):
                    yield Input(
                        placeholder="Search memory... (/ to focus, Esc to unfocus)",
                        id="memory-search-input",
                    )
                with VerticalScroll(id="memory-tree-container"):
                    yield Tree("Memory", id="memory-tree")
            with Vertical(id="memory-preview"):
                yield Static("Select a file or search to preview", id="memory-preview-title")
                yield VerticalScroll(Markdown("", id="memory-preview-content"))
                yield VerticalScroll(id="search-results-container", classes="hidden")

    def on_mount(self) -> None:
        self._load_memory()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_memory(self) -> None:
        self._memory_files = data.get_memory_files()
        tree: Tree = self.query_one("#memory-tree", Tree)
        tree.clear()
        tree.root.expand()
        by_project: dict[str, list[data.MemoryFile]] = {}
        for mf in self._memory_files:
            by_project.setdefault(mf.project, []).append(mf)
        summary = data.memory_summary(self._memory_files)
        tree.root.set_label(
            f"Memory ({summary['files']} files, {data.format_size(summary['size'])})"
        )
        for proj, files in sorted(by_project.items()):
            proj_node = tree.root.add(
                f"📁 {escape(proj)} ({len(files)})", expand=True
            )
            for mf in files:
                icon = "📄" if mf.name == "MEMORY.md" else "📝"
                proj_node.add_leaf(
                    f"{icon} {escape(mf.name)} ({data.format_size(mf.size)})",
                    data=mf,
                )

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        if node.data and isinstance(node.data, data.MemoryFile):
            self._selected_file = node.data
            title = self.query_one("#memory-preview-title", Static)
            title.update(f" {node.data.display_name} ({node.data.lines} lines)")
            md = self.query_one("#memory-preview-content", Markdown)
            md.update(node.data.content)
            self.query_one("#memory-preview-content").parent.remove_class("hidden")
            self.query_one("#search-results-container").add_class("hidden")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "memory-search-input":
            return
        # Debounce: cancel previous timer, start new 200ms timer
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value
        self._search_timer = self.set_timer(
            0.2, lambda: self._do_search(query)
        )

    def _do_search(self, query: str) -> None:
        results_container = self.query_one("#search-results-container")
        preview_scroll = self.query_one("#memory-preview-content").parent

        if not query.strip():
            results_container.add_class("hidden")
            preview_scroll.remove_class("hidden")
            return

        results = data.search_memory(query, self._memory_files, context=1)
        results_container.remove_class("hidden")
        preview_scroll.add_class("hidden")

        title = self.query_one("#memory-preview-title", Static)
        title.update(f" Search: '{query}' — {len(results)} results")

        results_container.remove_children()
        if not results:
            results_container.mount(Static("[dim]No results found.[/dim]"))
            return
        for r in results[:50]:
            parts = [
                f"[bold cyan]{escape(r.file.display_name)}[/bold cyan]"
                f":[yellow]{r.line_num}[/yellow]"
            ]
            if r.context_before:
                parts.append(f"[dim]{escape(r.context_before)}[/dim]")
            parts.append(f"  {escape(r.line)}")
            if r.context_after:
                parts.append(f"[dim]{escape(r.context_after)}[/dim]")
            results_container.mount(
                Static("\n".join(parts), classes="search-result")
            )


# ============================================================
# Tasks Tab
# ============================================================

class TasksTab(TabPane):
    """Live task board from recent sessions."""

    def __init__(self) -> None:
        super().__init__("Tasks", id="tab-tasks")

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="tasks-container")

    def on_mount(self) -> None:
        self._load_tasks()

    def _load_tasks(self) -> None:
        container = self.query_one("#tasks-container")
        container.remove_children()

        tasks = data.get_all_recent_tasks(limit=3)
        if not tasks:
            container.mount(Static(
                "[dim]No tasks found in recent sessions.\n\n"
                "Tasks appear here when Claude Code creates them\n"
                "during your sessions (TaskCreate/TaskUpdate).[/dim]"
            ))
            return

        summary = data.task_summary(tasks)
        container.mount(Static(
            f"[bold]Tasks[/bold]  "
            f"[yellow]⏳ {summary['active']} active[/yellow]  "
            f"○ {summary['pending']} pending  "
            f"[green]✅ {summary['done']} done[/green]  "
            f"({summary['total']} total)\n"
        ))

        active = [t for t in tasks if t.status == "in_progress"]
        pending = [t for t in tasks if t.status == "pending"]
        done = [t for t in tasks if t.status == "completed"]

        if active:
            container.mount(Static("[bold yellow]⏳ In Progress[/bold yellow]",
                                   classes="task-group-title"))
            for t in active:
                container.mount(self._render_task(t, "yellow"))

        if pending:
            container.mount(Static("\n[bold]○ Pending[/bold]",
                                   classes="task-group-title"))
            for t in pending:
                container.mount(self._render_task(t, "white"))

        if done:
            container.mount(Static("\n[bold green]✅ Completed[/bold green]",
                                   classes="task-group-title"))
            for t in done:
                container.mount(self._render_task(t, "green"))

    def _render_task(self, t: data.Task, color: str) -> Static:
        lines = [f"  [bold {color}]#{t.id}[/bold {color}] {escape(t.subject)}"]
        if t.description:
            desc = t.description[:120] + ("..." if len(t.description) > 120 else "")
            lines.append(f"     [dim]{escape(desc)}[/dim]")
        if t.blocked_by:
            lines.append(f"     [red]Blocked by: #{', #'.join(t.blocked_by)}[/red]")
        if t.active_form:
            lines.append(f"     [italic]{escape(t.active_form)}[/italic]")
        return Static("\n".join(lines), classes="task-card")


# ============================================================
# Plans Tab
# ============================================================

class PlansTab(TabPane):
    """Browse all plan files."""

    def __init__(self) -> None:
        super().__init__("Plans", id="tab-plans")
        self._plans: list[data.Plan] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="plans-container"):
            yield ListView(id="plans-list")
            yield VerticalScroll(Markdown("Select a plan to preview", id="plans-preview"))

    def on_mount(self) -> None:
        self._load_plans()

    def _load_plans(self) -> None:
        self._plans = data.get_plans()
        plan_list = self.query_one("#plans-list", ListView)
        plan_list.clear()
        for p in self._plans:
            plan_list.append(
                ListItem(
                    Static(
                        f"[bold]{escape(p.name)}[/bold]\n"
                        f"[dim]{p.lines} lines · {data.format_size(p.size)} · "
                        f"{data.time_ago(p.mtime)}[/dim]"
                    ),
                    name=p.name,
                )
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and idx < len(self._plans):
            plan = self._plans[idx]
            md = self.query_one("#plans-preview", Markdown)
            md.update(plan.content)


# ============================================================
# Stats Tab
# ============================================================

class StatsTab(TabPane):
    """Usage statistics dashboard."""

    def __init__(self) -> None:
        super().__init__("Stats", id="tab-stats")

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="stats-container")

    def on_mount(self) -> None:
        self._load_stats()

    def _load_stats(self) -> None:
        container = self.query_one("#stats-container")
        container.remove_children()

        all_stats = data.get_stats()
        overview = data.get_stats_overview()

        if not all_stats and not overview:
            container.mount(Static(
                "[dim]No stats data found.\n\n"
                "Stats are collected automatically by Claude Code\n"
                "and appear here after your first session.[/dim]"
            ))
            return

        summary = data.stats_summary(all_stats)

        # Overview
        container.mount(Static("[bold]Usage Overview[/bold]\n"))
        total_msgs = overview.get("total_messages", summary["total_messages"])
        total_sess = overview.get("total_sessions", summary["total_sessions"])
        container.mount(Static(
            f"  [bold]{data.format_number(total_msgs)}[/bold] messages  |  "
            f"[bold]{data.format_number(summary['total_tools'])}[/bold] tool calls  |  "
            f"[bold]{total_sess}[/bold] sessions  |  "
            f"[bold]{summary['days']}[/bold] days tracked"
        ))
        container.mount(Static(
            f"  Avg: [bold]{data.format_number(summary['avg_daily_messages'])}[/bold] msgs/day  |  "
            f"First session: {overview.get('first_session', 'N/A')[:10]}\n"
        ))

        # Model usage
        models = overview.get("models", {})
        if models and isinstance(models, dict):
            container.mount(Static("[bold]Model Usage[/bold]"))
            for model_name, usage in models.items():
                if isinstance(usage, dict):
                    cache_read = usage.get("cacheReadInputTokens", 0)
                    cache_write = usage.get("cacheCreationInputTokens", 0)
                    inp = usage.get("inputTokens", 0)
                    out = usage.get("outputTokens", 0)
                    total_tokens = cache_read + cache_write + inp + out
                    short = model_name.replace("claude-", "").split("-2025")[0]
                    container.mount(Static(
                        f"  [cyan]{short}[/cyan]  "
                        f"in:{data.format_number(inp)}  out:{data.format_number(out)}  "
                        f"cache:{data.format_number(cache_read)}  "
                        f"total:{data.format_number(total_tokens)}"
                    ))
            container.mount(Static(""))

        # Context gauge
        ctx = data.estimate_context_usage()
        if ctx.get("active"):
            container.mount(Static(
                f"  [bold]Active Session[/bold]  "
                f"{gauge_bar(ctx['percent'])}  "
                f"~{data.format_number(ctx['tokens_est'])} tokens  "
                f"~${ctx['cost_est']:.2f}  "
                f"{ctx.get('age_minutes', 0)}m ago\n"
            ))
        else:
            container.mount(Static("  [dim]No active session detected[/dim]\n"))

        # Sparklines
        recent_30 = all_stats[-30:] if len(all_stats) >= 30 else all_stats
        msg_values = [s.messages for s in recent_30]
        tool_values = [s.tool_calls for s in recent_30]

        if msg_values:
            spark = sparkline(msg_values, width=40)
            container.mount(Static(f"  [bold]Messages (last {len(recent_30)}d)[/bold]"))
            container.mount(Static(f"  [cyan]{spark}[/cyan]"))
            container.mount(Static(
                f"  [dim]{recent_30[0].date}  {'·' * 20}  {recent_30[-1].date}[/dim]\n"
            ))

        if tool_values:
            spark = sparkline(tool_values, width=40)
            container.mount(Static(f"  [bold]Tool calls (last {len(recent_30)}d)[/bold]"))
            container.mount(Static(f"  [magenta]{spark}[/magenta]"))
            container.mount(Static(""))

        # Top 5 days
        sorted_days = sorted(all_stats, key=lambda s: s.messages, reverse=True)[:5]
        if sorted_days:
            peak = max(d.messages for d in sorted_days)
            container.mount(Static("  [bold]Top 5 Most Active Days[/bold]"))
            for s in sorted_days:
                bar_len = int(s.messages / peak * 25) if peak > 0 else 0
                container.mount(Static(
                    f"  {s.date}  [cyan]{'█' * bar_len}[/cyan] "
                    f"{data.format_number(s.messages)} msgs, "
                    f"{s.sessions} sess, {data.format_number(s.tool_calls)} tools"
                ))

        # Longest session
        longest = overview.get("longest_session", {})
        if isinstance(longest, dict) and longest.get("messageCount"):
            dur_h = longest.get("duration", 0) / 3_600_000
            container.mount(Static(
                f"\n  [bold]Longest Session[/bold]  "
                f"{longest['messageCount']} messages · {dur_h:.1f}h · "
                f"{longest.get('timestamp', 'N/A')[:10]}"
            ))


# ============================================================
# History Tab
# ============================================================

class HistoryTab(TabPane):
    """Searchable command history with debounce."""

    def __init__(self) -> None:
        super().__init__("History", id="tab-history")
        self._entries: list[data.HistoryEntry] = []
        self._filtered: list[data.HistoryEntry] = []
        self._search_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="history-container"):
            with Vertical(id="history-search"):
                yield Input(
                    placeholder="Filter history... (Esc to unfocus)",
                    id="history-search-input",
                )
            yield VerticalScroll(id="history-list")

    def on_mount(self) -> None:
        self._load_history()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_history(self) -> None:
        self._entries = data.get_history(limit=500)
        self._filtered = self._entries
        self._render_list()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "history-search-input":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value.strip().lower()
        self._search_timer = self.set_timer(
            0.15, lambda: self._filter_and_render(query)
        )

    def _filter_and_render(self, query: str) -> None:
        if query:
            self._filtered = [
                e for e in self._entries
                if query in e.display.lower() or query in e.project.lower()
            ]
        else:
            self._filtered = self._entries
        self._render_list()

    def _render_list(self) -> None:
        container = self.query_one("#history-list")
        container.remove_children()
        for entry in self._filtered[:100]:
            ts = (data.time_ago(entry.timestamp / 1000)
                  if entry.timestamp > 1e12
                  else data.time_ago(entry.timestamp))
            display = entry.display.replace("\n", " ")[:100]
            container.mount(Static(
                f"[bold]{escape(display)}[/bold]\n"
                f"[dim]{escape(entry.project)} · {ts}[/dim]",
                classes="history-entry",
            ))
        if not self._filtered:
            container.mount(Static("[dim]No matching entries.[/dim]"))


# ============================================================
# Main App
# ============================================================

class CockpitApp(App):
    """Claude Cockpit — X-ray vision for your Claude Code brain."""

    CSS_PATH = "app.tcss"
    TITLE = "Claude Cockpit"
    SUB_TITLE = "~/.claude/"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("slash", "focus_search", "Search", show=True, key_display="/"),
        Binding("question_mark", "toggle_help", "Help", show=True, key_display="?"),
        Binding("m", "switch_tab('tab-memory')", "Memory", show=True),
        Binding("t", "switch_tab('tab-tasks')", "Tasks", show=True),
        Binding("p", "switch_tab('tab-plans')", "Plans", show=True),
        Binding("s", "switch_tab('tab-stats')", "Stats", show=True),
        Binding("h", "switch_tab('tab-history')", "History", show=True),
        Binding("r", "refresh_all", "Refresh", show=True),
        Binding("escape", "unfocus", "Unfocus", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._gauge_cache: str = ""
        self._gauge_cache_tick: int = 0
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="context-gauge")
        with TabbedContent():
            yield MemoryTab()
            yield TasksTab()
            yield PlansTab()
            yield StatsTab()
            yield HistoryTab()
        yield Footer()

    def on_mount(self) -> None:
        self._update_context_gauge()
        self.set_interval(5.0, self._update_context_gauge)
        self._start_file_watcher()

    def on_unmount(self) -> None:
        self._watcher_stop.set()

    def _start_file_watcher(self) -> None:
        """Watch ~/.claude/ for changes and auto-refresh affected tabs."""
        try:
            from watchfiles import watch, Change
        except ImportError:
            return  # watchfiles not installed, skip auto-refresh

        def _watcher():
            watch_dirs = [str(p) for p in data.WATCH_PATHS if p.exists()]
            # Also watch memory dirs
            if data.PROJECTS_DIR.exists():
                for mem_dir in data.PROJECTS_DIR.glob("*/memory"):
                    watch_dirs.append(str(mem_dir))
            if not watch_dirs:
                return
            try:
                for changes in watch(
                    *watch_dirs,
                    stop_event=self._watcher_stop,
                    debounce=1500,
                    step=500,
                    rust_timeout=5000,
                ):
                    changed_paths = {Path(p) for _, p in changes}
                    # Determine which tabs need refreshing
                    refresh_memory = any(
                        "memory" in str(p) for p in changed_paths
                    )
                    refresh_tasks = any(
                        "tasks" in str(p) for p in changed_paths
                    )
                    refresh_plans = any(
                        "plans" in str(p) for p in changed_paths
                    )
                    refresh_stats = any(
                        "stats" in str(p) for p in changed_paths
                    )
                    # Schedule refreshes on the main thread
                    if refresh_tasks:
                        self.call_from_thread(self._refresh_tab, "tasks")
                    if refresh_memory:
                        self.call_from_thread(self._refresh_tab, "memory")
                    if refresh_plans:
                        self.call_from_thread(self._refresh_tab, "plans")
                    if refresh_stats:
                        self.call_from_thread(self._refresh_tab, "stats")
                    self.call_from_thread(self._invalidate_gauge_cache)
            except Exception as exc:
                import sys
                print(f"cockpit: file watcher stopped: {exc}", file=sys.stderr)

        self._watcher_thread = threading.Thread(target=_watcher, daemon=True)
        self._watcher_thread.start()

    def _refresh_tab(self, tab_name: str) -> None:
        """Refresh a specific tab's data."""
        method_map = {
            "memory": "_load_memory",
            "tasks": "_load_tasks",
            "plans": "_load_plans",
            "stats": "_load_stats",
            "history": "_load_history",
        }
        method = method_map.get(tab_name)
        if not method:
            return
        for tab in self.query(TabPane):
            if hasattr(tab, method):
                getattr(tab, method)()
                break

    def _invalidate_gauge_cache(self) -> None:
        self._gauge_cache = ""

    def _update_context_gauge(self) -> None:
        ctx = data.estimate_context_usage()
        gauge = self.query_one("#context-gauge", Static)
        if ctx.get("active"):
            bar = gauge_bar(ctx["percent"], width=15)
            gauge.update(
                f" Session: {bar}  "
                f"~{data.format_number(ctx['tokens_est'])} tokens  "
                f"~${ctx['cost_est']:.2f}  "
                f"{ctx.get('age_minutes', 0)}m ago"
            )
            self._gauge_cache = ""
        else:
            self._gauge_cache_tick += 1
            if not self._gauge_cache or self._gauge_cache_tick % 12 == 0:
                # Use lightweight stat-only calls (lazy content not loaded)
                files = data.get_memory_files()
                summary = data.memory_summary(files)
                tasks = data.get_tasks()
                ts = data.task_summary(tasks)
                self._gauge_cache = (
                    f" {summary['files']} memory files · "
                    f"{data.format_number(summary['size'])} "
                    f"|  {ts['active']} active · {ts['pending']} pending · "
                    f"{ts['done']} done  |  No active session"
                )
            gauge.update(self._gauge_cache)

    def action_focus_search(self) -> None:
        self.query_one(TabbedContent).active = "tab-memory"
        search_input = self.query_one("#memory-search-input", Input)
        search_input.focus()

    def action_unfocus(self) -> None:
        """Unfocus any focused input so keyboard shortcuts work again."""
        self.set_focus(None)

    def action_toggle_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_refresh_all(self) -> None:
        """Reload all data from disk."""
        self._gauge_cache = ""
        for tab in self.query(TabPane):
            for method_name in ("_load_memory", "_load_tasks", "_load_plans",
                                "_load_stats", "_load_history"):
                if hasattr(tab, method_name):
                    getattr(tab, method_name)()
                    break
        self._update_context_gauge()
        self.notify("Refreshed all data", timeout=2)


def main():
    app = CockpitApp()
    app.run()


if __name__ == "__main__":
    main()
