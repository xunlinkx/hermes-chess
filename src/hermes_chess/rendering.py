"""Board image rendering for the chess plugin.

Generates PNG board images from authoritative game state using python-chess
and CairoSVG. Handles file management, atomic writes, and cleanup.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import chess
import chess.svg

CAIROSVG_AVAILABLE = False
_cairosvg = None
try:
    import cairosvg
    _cairosvg = cairosvg
    CAIROSVG_AVAILABLE = True
except (ImportError, OSError):
    # cairosvg pulls in cairocffi, which raises OSError (not ImportError) when
    # the cairo native library is absent from the loader path (e.g. the Hermes
    # gateway venv without DYLD_LIBRARY_PATH). Treat that like the package
    # being missing: rendering is optional and degrades to the ASCII board.
    CAIROSVG_AVAILABLE = False
    _cairosvg = None

from .config import PluginConfig

logger = logging.getLogger(__name__)

RENDER_DIR_NAME = "rendered"
MAX_RENDERED_PER_GAME = 20
PNG_SIZE = 1024


class RenderingError(Exception):
    """Raised when board rendering fails."""
    pass


def _get_render_dir(config: PluginConfig) -> Path:
    """Get the rendered images directory, creating it if needed."""
    # Use the same directory as the database
    data_dir = Path(config.db_path).parent
    render_dir = data_dir / RENDER_DIR_NAME
    render_dir.mkdir(parents=True, exist_ok=True)
    return render_dir


def _render_filename(game_id: int, ply: int) -> str:
    """Generate a safe filename for a rendered board."""
    return f"game-{game_id}-ply-{ply}.png"


def _verify_png(path: Path) -> bool:
    """Verify a file is a valid, non-empty PNG."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        # Check PNG signature
        with path.open("rb") as f:
            header = f.read(8)
        return header == b"\x89PNG\r\n\x1a\n"
    except Exception:
        return False


def render_board_png(
    board: chess.Board,
    human_color: chess.Color,
    last_move: Optional[chess.Move],
    game_id: int,
    ply: int,
    config: PluginConfig,
) -> Path:
    """Render a board position to a PNG file.

    Args:
        board: The authoritative board position from persisted state.
        human_color: The human player's color (WHITE or BLACK).
        last_move: The most recent move for highlighting, or None.
        game_id: The game database ID for filename.
        ply: The current ply number for filename.
        config: Plugin configuration for data directory.

    Returns:
        Path to the generated PNG file.

    Raises:
        RenderingError: If rendering or file operations fail.
    """
    if not CAIROSVG_AVAILABLE or _cairosvg is None:
        raise RenderingError("CairoSVG not available; cannot render board images")

    render_dir = _get_render_dir(config)
    filename = _render_filename(game_id, ply)
    final_path = render_dir / filename

    # Generate SVG using python-chess
    flipped = human_color == chess.BLACK
    check_square = board.king(board.turn) if board.is_check() else None

    svg_bytes = chess.svg.board(
        board=board,
        flipped=flipped,
        coordinates=True,
        lastmove=last_move,
        check=check_square,
        size=PNG_SIZE,
    ).encode("utf-8")

    # Convert SVG to PNG using CairoSVG
    # Use atomic write: temp file -> verify -> rename
    with tempfile.NamedTemporaryFile(
        dir=render_dir, suffix=".png", delete=False
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        _cairosvg.svg2png(bytestring=svg_bytes, write_to=str(tmp_path))

        if not _verify_png(tmp_path):
            raise RenderingError("Generated file is not a valid PNG")

        # Atomic rename
        tmp_path.replace(final_path)

        # Cleanup old renders for this game
        _cleanup_old_renders(render_dir, game_id)

        return final_path

    except Exception as e:
        # Clean up temp file on failure
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise RenderingError(f"Failed to render board PNG: {e}") from e


def _cleanup_old_renders(render_dir: Path, game_id: int) -> None:
    """Keep only the most recent MAX_RENDERED_PER_GAME images for a game."""
    pattern = f"game-{game_id}-ply-*.png"
    files = sorted(
        render_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_file in files[MAX_RENDERED_PER_GAME:]:
        try:
            old_file.unlink()
        except Exception:
            logger.warning("Failed to clean up old render: %s", old_file)


def get_latest_render(game_id: int, config: PluginConfig) -> Optional[Path]:
    """Get the most recent rendered image for a game, if any."""
    render_dir = _get_render_dir(config)
    pattern = f"game-{game_id}-ply-*.png"
    files = sorted(
        render_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def cleanup_game_renders(game_id: int, config: PluginConfig) -> None:
    """Remove all rendered images for a game (e.g., on rematch/archive)."""
    render_dir = _get_render_dir(config)
    pattern = f"game-{game_id}-ply-*.png"
    for file in render_dir.glob(pattern):
        try:
            file.unlink()
        except Exception:
            logger.warning("Failed to delete render during cleanup: %s", file)