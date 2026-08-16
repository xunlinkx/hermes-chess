# Hermes Chess

**Persistent local human-versus-Stockfish chess** with PNG rendering, slash commands, identity isolation, and full error recovery. I originally wrote this with the help of my hermes agent's Lucca and Bean, so that my wife could play chess with her bot through imessage. Planning some more features...but just wanted to get this out there for more playtesting.

Author: [xunlinkx](https://github.com/xunlinkx)

## Features

- Play chess against Stockfish at 8 difficulty levels (beginner → maximum)
- **Human vs human (PvP)** — two people in the same chat/channel play each other; the engine never intervenes
- PNG board images rendered via CairoSVG — works in iMessage, Telegram, Discord
- Per-conversation identity isolation — multiple games, different opponents
- Deterministic rules engine (castling, en passant, promotion, repetition, 50-move)
- SQLite persistence — games survive restarts, engines crashes, network failures
- Idempotent move handling — duplicate messages don't double-play
- Remote skill integration — agents can start and play games via `/chess` commands
- **`CHESS:` prefix** — every /chess output is prefixed so it stands out in a multi-agent message stream
- **Rich status** — `/chess status` shows game ID, difficulty, colors, ply count, last move, timer state
- **Per-game timer** — `/chess timer [on|off|status]` toggles best-effort timing (accuracy disclaimer included)

## Dependencies

### System packages

**macOS (Homebrew):**
```bash
brew install stockfish cairo
```

**Linux (Fedora / dnf-based, e.g. Bean):**
```bash
sudo dnf install stockfish cairo cairo-devel python3-devel
```

On other distros (apt): `sudo apt install stockfish libcairo2-dev`

### Python (in your Hermes venv or standalone venv)
```bash
pip install python-chess CairoSVG
```

## Installation

### Recommended: from GitHub (native Hermes plugin manager)

```bash
hermes plugins install xunlinkx/hermes-chess
hermes gateway restart
```

This clones the repo into `~/.hermes/plugins/hermes-chess/` as a regular directory and
the gateway discovers it automatically via `plugin.yaml`.

To update later:

```bash
hermes plugins update hermes-chess
hermes gateway restart         # only needed for handler/plugin.yaml changes
```

### Manual: clone into plugins directory

```bash
git clone https://github.com/xunlinkx/hermes-chess.git \
  ~/.hermes/plugins/hermes-chess
pip install python-chess CairoSVG    # into your Hermes venv
hermes gateway restart
```

### As a standalone Python package (pip install)

```bash
git clone https://github.com/xunlinkx/hermes-chess.git
cd hermes-chess
pip install .
```

### For development

```bash
git clone https://github.com/xunlinkx/hermes-chess.git
cd hermes-chess
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Development workflow (updating the live plugin)

This repo is the **canonical source**. Edit code in `src/hermes_chess/`, test it, then
push changes to the live Hermes plugin:

```bash
# 1. Make changes and verify
cd hermes-chess   # your local clone
pytest tests/

# 2. Sync to the Hermes plugin directory
rsync -a --delete src/hermes_chess/ ~/.hermes/plugins/hermes-chess/

# 3. Restart gateway to pick up handler changes
hermes gateway restart
```

> If you use a symlink (`ln -s src/hermes_chess ~/.hermes/plugins/hermes-chess`),
> edits take effect immediately — only restart the gateway for plugin metadata
> or handler registration changes.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HERMES_CHESS_STOCKFISH` | `$HERMES_HOME/bin/stockfish` | Path to Stockfish binary |
| `HERMES_CHESS_DB` | `$HERMES_HOME/data/chess/chess.sqlite3` | SQLite database path |
| `HERMES_CHESS_DEFAULT_DIFFICULTY` | `casual` | Starting difficulty |
| `ENGINE_THREADS` | `1` | Stockfish thread count |
| `HASH_MB` | `32` | Stockfish hash table size (MB) |
| `ENGINE_TIMEOUT` | `8.0` | Max seconds per engine move |

> **macOS only:** CairoSVG needs `libcairo.2.dylib` on the library load path.
> Set `DYLD_LIBRARY_PATH=/opt/homebrew/lib` in your gateway startup script
> (or use `brew --prefix cairo` to find the right path).
>
> **Note:** The defaults assume `$HERMES_HOME` is set (typically `~/.hermes`).
> If running standalone without `HERMES_HOME`, the config falls back to
> `~/.hermes` — create that directory or explicitly set all `HERMES_CHESS_*`
> variables.

> On Fedora/Bean, CairoSVG finds libcairo via the system linker automatically
> after `dnf install cairo-devel` — no env var needed.

### Common pitfalls

| Issue | Symptom | Fix |
|---|---|---|
| **Stockfish not found** | `EngineUnavailable: Stockfish executable is missing` | `brew install stockfish` or set `HERMES_CHESS_STOCKFISH=/opt/homebrew/bin/stockfish` in your gateway env |
| **Cairo library not loaded** | `OSError: no library called "cairo-2" was found` (macOS only) | Set `DYLD_LIBRARY_PATH=/opt/homebrew/lib` before starting the gateway |
| **Gateway ignores plugin after update** | Old commands still work unchanged | You must restart the gateway: `hermes gateway restart` |
| **Two profiles, one shares the same DB** | Moves from one profile appear in another | Each profile needs its own `HERMES_CHESS_DB` path, or use different `owner_key` values |
| **Stockfish path vs brew location** | Plugin works in terminal but not in gateway | The gateway process may not have the same `PATH`. Set `HERMES_CHESS_STOCKFISH` explicitly to the absolute binary path |
| **Plugin not registered** | `/chess` returns "unknown command" | Ensure `~/.hermes/plugins/hermes-chess/plugin.yaml` exists and gateway has been restarted. Verify with `hermes plugins list` |

## Usage

### Slash commands (in any supported Hermes chat — iMessage, Telegram, Discord, etc.)

| Command | Description |
|---|---|---|
| `/chess` | Start a new game (interactive prompts for difficulty and color) |
| `/chess <difficulty>` | Start at a specific level (beginner / easy / casual / intermediate / advanced / expert / maximum) |
| `/chess <difficulty> <color>` | Full start: `/chess beginner black`, `/chess casual white` |
| `/chess pvp <color>` | Start a human-vs-human game (`multiplayer`, `friend`, and `human` are aliases) |
| `e2e4`, `Nf3`, `O-O` | Make a move (standard SAN notation) |
| `/chess status` | Show rich game status: game ID, difficulty, colors, ply count, last move, timer state + board |
| `/chess timer [on\|off\|status]` | Toggle per-game best-effort timing or query current state |
| `/chess undo` | Undo last move pair |
| `/chess resign` | Resign |
| `/chess board` | Show current board state |
| `/chess hint` | Get Stockfish's suggested move |
| `/chess analyze` | Deep analysis of current position |
| `/chess pgn` | Export game in PGN format |

### Difficulty levels

| Name | Approx Elo | Description |
|---|---|---|
| beginner | 600 | Makes obvious blunders |
| easy | 1000 | Weak but recognisable play |
| casual | 1400 | Typical club player |
| intermediate | 1700 | Strong club player |
| advanced | 2000 | Expert level |
| expert | 2300 | Master level |
| maximum | 3000 | Full Stockfish strength |
| pvp | — | Human vs human in a shared chat (no engine) |

### Agent / LLM instructions

When adding this plugin to a Hermes profile, include a skill instruction like:

```
You have the Chess plugin available. To start a game, use:
/chess beginner black   (or any difficulty + color)
Reply with your move in standard algebraic notation (e.g. e2e4, Nf3, O-O).
```

> **Important:** the agent must never *play chess itself*. The agent is not the
> opponent and must not invent moves, positions, FEN, or PGN from prose. Every
> move — the human's and the engine's — is recorded and validated by the
> authoritative `chess_game` tool. The agent only routes messages to that tool
> and relays its results (PNG board + concise text). If the human is playing
> PvP against another person, the agent's only job is to move the board along.

## Project Structure

```
hermes-chess/
├── src/
│   └── hermes_chess/
│       ├── __init__.py          # Plugin entrypoint + slash handler
│       ├── config.py            # Difficulty mapping, env vars
│       ├── engine.py            # Stockfish UCI runner
│       ├── gateway/
│       │   ├── __init__.py
│       │   └── session_context.py  # Hermes gateway shim
│       ├── plugin.yaml          # Hermes plugin metadata
│       ├── rendering.py         # SVG → PNG board renderer
│       ├── service.py           # ChessService — game logic + persistence
│       └── storage.py           # SQLite schema + migrations
├── tests/
│   ├── conftest.py
│   ├── test_chess_service.py    # 54 unit tests
│   └── e2e_tool_path.py         # Hermes integration test
├── pyproject.toml
├── LICENSE
└── README.md
```

## Running Tests

**macOS:**
```bash
DYLD_LIBRARY_PATH=/opt/homebrew/lib pytest tests/
```

**Linux (Fedora/Bean):**
```bash
pytest tests/
```

One test (`test_real_stockfish_legal_move`) is skipped unless Stockfish is on PATH — expected outside the gateway context.

## License

MIT — see [LICENSE](LICENSE). Contributions welcome via pull request.
