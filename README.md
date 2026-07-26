# Hermes Chess

**Persistent local human-versus-Stockfish chess** with PNG rendering, slash commands, identity isolation, and full error recovery.

Author: [xunlinkx](https://github.com/xunlinkx)

## Features

- Play chess against Stockfish at 8 difficulty levels (beginner → maximum)
- PNG board images rendered via CairoSVG — works in iMessage, Telegram, Discord
- Per-conversation identity isolation — multiple games, different opponents
- Deterministic rules engine (castling, en passant, promotion, repetition, 50-move)
- SQLite persistence — games survive restarts, engines crashes, network failures
- Idempotent move handling — duplicate messages don't double-play
- Remote skill integration — agents can start and play games via `/chess` commands

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

### As a Hermes plugin (standard)

```bash
# Clone into your Hermes plugins directory
git clone https://github.com/xunlinkx/hermes-chess.git \
  ~/.hermes/plugins/chess

# Install Python deps into your Hermes venv
~/.hermes/.venv/bin/pip install python-chess CairoSVG

# Restart gateway
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

> On Fedora/Bean, CairoSVG finds libcairo via the system linker automatically
> after `dnf install cairo-devel` — no env var needed.

## Usage

### Slash commands (in any supported Hermes chat — iMessage, Telegram, Discord, etc.)

| Command | Description |
|---|---|
| `/chess` | Start a new game (interactive prompts for difficulty and color) |
| `/chess <difficulty>` | Start at a specific level (beginner / easy / casual / intermediate / advanced / expert / maximum) |
| `/chess <difficulty> <color>` | Full start: `/chess beginner black`, `/chess casual white` |
| `e2e4`, `Nf3`, `O-O` | Make a move (standard SAN notation) |
| `/undo` | Undo last move pair |
| `/resign` | Resign |
| `/board` | Show current board state |
| `/hint` | Get Stockfish's suggested move |
| `/analyze` | Deep analysis of current position |
| `/pgn` | Export game in PGN format |

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

### Agent / LLM instructions

When adding this plugin to a Hermes profile, include a skill instruction like:

```
You have the Chess plugin available. To start a game, use:
/chess beginner black   (or any difficulty + color)
Reply with your move in standard algebraic notation (e.g. e2e4, Nf3, O-O).
```

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
│   ├── test_chess_service.py    # 53 unit tests
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
