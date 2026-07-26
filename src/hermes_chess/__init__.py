"""Profile-local persistent chess plugin for Hermes Agent."""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any, Dict

from .service import ChessService, current_identity

logger = logging.getLogger(__name__)

CHESS_GAME_SCHEMA: Dict[str, Any] = {
    "name": "chess_game",
    "description": (
        "Authoritative persistent local chess. Use for setup, moves, board, "
        "resume, analysis, undo, draws, resignation, PGN, history, and rematches. "
        "The SQLite game is authoritative; never infer a position from chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "setup", "start", "move", "board", "status", "resume",
                    "legal_moves", "hint", "analyze", "undo", "resign",
                    "offer_draw", "accept_draw", "decline_draw",
                    "set_difficulty", "get_difficulty", "set_color", "pgn",
                    "list_games", "rematch", "help", "timer",
                ],
            },
            "move": {
                "type": "string",
                "maxLength": 64,
                "description": "Human move in SAN, UCI, or supported castling/promotion phrase.",
            },
            "difficulty": {
                "oneOf": [
                    {"type": "string", "maxLength": 40},
                    {"type": "integer", "minimum": 1, "maximum": 10000},
                ],
                "description": "Named preset or approximate Elo.",
            },
            "color": {"type": "string", "enum": ["white", "black", "random"]},
            "confirm": {
                "type": "boolean",
                "description": "Explicit confirmation for a mid-game difficulty change.",
            },
            "rematch_mode": {"type": "string", "enum": ["same", "swap"]},
            "game_id": {"type": "integer", "minimum": 1},
            "detail": {
                "type": "string",
                "maxLength": 80,
                "description": "Optional analysis focus, such as last_move.",
            },
            "timer": {
                "oneOf": [
                    {"type": "boolean"},
                    {"type": "string", "enum": ["on", "off", "status"]},
                ],
                "description": "Toggle per-game timing on/off or query status. Timer is best-effort only: network, cellular, agent availability, and interruptions mean actual elapsed time may be much longer than clock time. Disabled by default.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_service: ChessService | None = None


def _get_service() -> ChessService:
    global _service
    if _service is None:
        _service = ChessService.from_environment()
    return _service


def handle_chess_game(args: Dict[str, Any], **_kwargs: Any) -> str:
    """Hermes tool handler. Operational failures are returned, never raised."""
    try:
        if not isinstance(args, dict):
            return json.dumps({"success": False, "error": "arguments must be an object"})
        return json.dumps(
            _get_service().dispatch(current_identity(), args),
            ensure_ascii=False,
        )
    except Exception:
        logger.exception("chess_game failed")
        return json.dumps({
            "success": False,
            "error": "Chess is temporarily unavailable; the saved game was not discarded.",
            "recoverable": True,
        })


def _slash_handler(raw_args: str) -> str:
    """Route /chess through the same tool and database as natural language."""
    try:
        parts = shlex.split((raw_args or "")[:256])
    except ValueError:
        return "CHESS: Invalid /chess arguments. Try /chess help."
    if not parts:
        return (
            "CHESS: Chess commands: /chess start [difficulty] [white|black|random], "
            "/chess move <move>, /chess board, /chess status, /chess resume, "
            "/chess legal_moves, /chess hint, /chess analyze, /chess undo, "
            "/chess resign, /chess offer_draw, /chess accept_draw, "
            "/chess decline_draw, /chess pgn, /chess rematch, /chess timer [on|off|status], "
            "/chess help"
            " — or just /chess <difficulty> <color> to start directly "
            "(e.g. /chess beginner white)"
        )
    raw = parts.pop(0).lower()
    aliases = {"continue": "resume", "difficulty": "get_difficulty"}
    action = aliases.get(raw, raw)
    args: Dict[str, Any] = {"action": action}
    # Difficulty names that imply /chess start
    _DIFF_NAMES = {"beginner", "easy", "casual", "intermediate",
                   "advanced", "expert", "maximum"}
    is_start_cmd = action == "start" or action in _DIFF_NAMES
    if is_start_cmd:
        # Before trying to start, check for an existing active game.
        # If one exists, show status instead of trying to override.
        existing = _get_service().active_game_status(current_identity())
        if existing:
            return (
                f"CHESS: A game is already in progress. Here is the current status:\n\n"
                f"{existing}\n\n"
                f"If you want to start a new game, end the current one first "
                f"with /chess resign or wait for it to finish. "
                f"Type /chess help anytime for more options."
            )
    if action == "move":
        # In CLI sessions, the latest human move should be applied and the
        # response should include the updated board in the same turn.
        args["move"] = " ".join(parts)
    elif is_start_cmd:
        if action in _DIFF_NAMES:
            args["action"] = "start"
            args["difficulty"] = action
        for token in parts:
            low = token.lower()
            if low in {"white", "black", "random"}:
                args["color"] = low
            elif low.isdigit():
                args["difficulty"] = int(low)
            else:
                args["difficulty"] = low
    elif action == "rematch":
        for token in parts:
            low = token.lower()
            if low in {"same", "swap"}:
                args["rematch_mode"] = low
            elif low.isdigit():
                args["difficulty"] = int(low)
            else:
                args["difficulty"] = low
    elif action == "get_difficulty" and parts:
        args["action"] = "set_difficulty"
        args["difficulty"] = int(parts[0]) if parts[0].isdigit() else parts[0]
    elif action not in {
        "board", "status", "resume", "legal_moves", "hint", "analyze",
        "undo", "resign", "offer_draw", "accept_draw", "decline_draw",
        "get_difficulty", "pgn", "list_games", "help", "timer",
    }:
        args = {"action": "move", "move": " ".join([action, *parts])}
    if args.get("action") == "timer":
        # /chess timer [on|off|status]
        val = parts[0].lower() if parts else "status"
        if val == "on":
            args["timer"] = True
        elif val == "off":
            args["timer"] = False
        else:  # status
            args["timer"] = "status"
    if args.get("action") == "help":
        return (
            "CHESS: Chess commands: /chess start [difficulty] [white|black|random], "
            "/chess move <move>, /chess board, /chess status, /chess resume, "
            "/chess legal_moves, /chess hint, /chess analyze, /chess undo, "
            "/chess resign, /chess offer_draw, /chess accept_draw, "
            "/chess decline_draw, /chess pgn, /chess rematch, /chess timer [on|off|status], "
            "/chess help"
            " — or just /chess <difficulty> <color> to start directly "
            "(e.g. /chess beginner white)"
        )
    result = json.loads(handle_chess_game(args))
    if not result.get("success"):
        return f"CHESS: {result.get('error') or 'Chess command failed.'}"
    summary = result.get("message") or result.get("status") or ""
    board = result.get("board")
    image_path = result.get("image_path")
    media_tag = f"MEDIA:{image_path}" if image_path else ""
    if image_path:
        # PNG delivers the visual board natively — skip redundant ASCII board
        msg = summary
        if media_tag:
            msg += f"\n\n{media_tag}" if msg else media_tag
        return f"CHESS: {msg.strip() or '.'}"
    if board:
        if summary:
            msg = f"{summary}\n\n{board}".strip()
        else:
            msg = str(board)
        if media_tag:
            msg += f"\n\n{media_tag}"
        return f"CHESS: {msg}"
    msg = str(summary or json.dumps(result, ensure_ascii=False))
    if media_tag:
        msg = f"{media_tag}\n\n{msg}"
    return f"CHESS: {msg}"


def _pre_llm_context(**_kwargs: Any) -> Dict[str, str] | None:
    """Inject only lightweight owner-scoped metadata into the current turn."""
    try:
        note = _get_service().lightweight_context(current_identity())
        return {"context": note} if note else None
    except Exception:
        logger.debug("chess metadata hook skipped", exc_info=True)
        return None


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="chess_game",
        toolset="chess",
        schema=CHESS_GAME_SCHEMA,
        handler=handle_chess_game,
        check_fn=lambda: _get_service().health()["ready"],
        emoji="♟️",
    )
    ctx.register_command(
        name="chess",
        handler=_slash_handler,
        description="Play or resume a persistent local Stockfish chess game",
        args_hint="[start|move|board|status|continue|hint|analyze|undo|resign|pgn|rematch]",
    )
    ctx.register_hook("pre_llm_call", _pre_llm_context)
