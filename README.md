# Claude Cockpit

X-ray vision for your Claude Code brain — a TUI dashboard that runs alongside Claude Code, giving you live visibility into memory, tasks, plans, stats, and context usage.

<!-- TODO: Add screenshot here -->
<!-- ![Claude Cockpit](docs/screenshot.png) -->

## What It Shows

| Tab | What you see |
|-----|-------------|
| **Memory** | All `~/.claude/projects/*/memory/*.md` files, grouped by project, with full-text search |
| **Tasks** | Active / pending / done task board from recent Claude sessions |
| **Plans** | All plan files with markdown preview, sorted by recency |
| **Stats** | Usage metrics, model breakdown, sparklines, top active days, longest session |
| **History** | Searchable command history (last 500 entries) |

Plus a **context gauge** bar at the bottom showing estimated context window usage for your active session.

## Features

- **Read-only** — never writes to `~/.claude/`, only reads
- **Auto-refresh** — file watcher detects changes and updates affected tabs automatically
- **Search** — full-text search across all memory files with context lines (200ms debounce)
- **Lazy loading** — memory file contents loaded on demand, not all at once
- **iTerm2 status bar** — optional native status bar components (memory count, task counts, context gauge)
- **Keyboard-driven** — single-key tab switching, search focus, help screen

## Install

```bash
git clone https://github.com/amankansal-lt/claude-cockpit.git
cd claude-cockpit
./install.sh
```

The installer:
1. Creates a Python venv with dependencies
2. Adds `cockpit` alias and `cockpit-toggle` function to your shell
3. Optionally installs iTerm2 status bar components

Then open a new terminal (or `source ~/.zshrc`) and run:

```bash
cockpit              # Launch the full TUI dashboard
cockpit-toggle       # Open in iTerm2 split pane alongside Claude Code
```

### Requirements

- Python 3.10+
- macOS / Linux (iTerm2 features are macOS-only)
- Claude Code installed (`~/.claude/` must exist)

## Keybindings

| Key | Action |
|-----|--------|
| `m` | Memory tab |
| `t` | Tasks tab |
| `p` | Plans tab |
| `s` | Stats tab |
| `h` | History tab |
| `/` | Focus memory search |
| `?` | Toggle help screen |
| `r` | Refresh all data |
| `Esc` | Unfocus search input |
| `q` | Quit |

## iTerm2 Status Bar (Optional)

If you use iTerm2 with the Python Runtime installed:

1. Enable the API: iTerm2 > Settings > General > Magic > Enable Python API
2. Run `./install.sh` (it auto-detects and installs the status bar script)
3. iTerm2 > Settings > Profiles > Session > Status bar enabled
4. Click "Configure Status Bar" and drag the Claude components

Components update every 3-5 seconds:
- **Claude Memory** — file count and total size
- **Claude Tasks** — active/pending/done counts
- **Claude Context** — context window gauge bar

## How It Works

Claude Cockpit reads the `~/.claude/` directory structure:

```
~/.claude/
├── projects/*/memory/*.md   → Memory tab
├── tasks/*/N.json           → Tasks tab
├── plans/*.md               → Plans tab
├── stats-cache.json         → Stats tab
├── history.jsonl            → History tab
└── debug/*.txt              → Context gauge
```

It never writes anything. The file watcher (powered by `watchfiles` / Rust) monitors for changes and auto-refreshes.

## Development

```bash
# Run tests
.venv/bin/pytest tests/ -v

# Run directly
.venv/bin/python -m cockpit
```

## License

MIT
