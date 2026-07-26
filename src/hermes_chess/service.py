"""Deterministic chess actions, persistence, recovery, and rendering."""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import logging
import re
import secrets
import sqlite3
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

import chess
import chess.pgn

from .config import (
    DIFFICULTIES,
    Identity,
    PluginConfig,
    current_identity,
    parse_color,
    parse_difficulty,
)
from .engine import EngineResult, EngineUnavailable, StockfishRunner
from .storage import ChessDatabase, utc_now
from .rendering import (
    render_board_png,
    get_latest_render,
    cleanup_game_renders,
    RenderingError,
)

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {
    "setup", "start", "move", "board", "status", "resume", "legal_moves",
    "hint", "analyze", "undo", "resign", "offer_draw", "accept_draw",
    "decline_draw", "set_difficulty", "get_difficulty", "set_color", "pgn",
    "list_games", "rematch", "help",
}
ALLOWED_KEYS = {
    "action", "move", "difficulty", "color", "confirm", "rematch_mode",
    "game_id", "detail",
}
MUTATING_ACTIONS = {
    "setup", "start", "move", "undo", "resign", "offer_draw", "accept_draw",
    "decline_draw", "set_difficulty", "set_color", "rematch",
}

_LOCKS_GUARD = threading.Lock()
_OWNER_LOCKS: dict[str, threading.RLock] = defaultdict(threading.RLock)


def _owner_lock(owner_key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _OWNER_LOCKS[owner_key]


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _request_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(_json_text(args).encode("utf-8")).hexdigest()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _color_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def _termination_name(outcome: chess.Outcome) -> str:
    return outcome.termination.name.lower().replace("_", " ")


def _outcome_fields(board: chess.Board) -> dict[str, Any]:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return {
            "completed": False,
            "game_status": "check" if board.is_check() else "active",
            "result": None,
            "termination_reason": None,
        }
    reason = _termination_name(outcome)
    if outcome.winner is None:
        status = "stalemate" if outcome.termination == chess.Termination.STALEMATE else "draw"
    else:
        status = "checkmate" if outcome.termination == chess.Termination.CHECKMATE else "completed"
    return {
        "completed": True,
        "game_status": status,
        "result": outcome.result(),
        "termination_reason": reason,
    }


def _normalize_move_text(text: str, board: chess.Board) -> str:
    value = " ".join(str(text or "").strip().split())
    if len(value) > 64:
        raise ValueError("move is too long")
    lower = value.lower().replace("0", "o")
    if lower in {"castle kingside", "castle king side", "kingside castle"}:
        return "O-O"
    if lower in {"castle queenside", "castle queen side", "queenside castle"}:
        return "O-O-O"
    promotion = re.fullmatch(
        r"([a-h][27][a-h][18])\s+(?:promote|promotion)\s+(?:to\s+)?"
        r"(queen|rook|bishop|knight|q|r|b|n)",
        lower,
    )
    if promotion:
        piece = {
            "queen": "q", "rook": "r", "bishop": "b", "knight": "n",
            "q": "q", "r": "r", "b": "b", "n": "n",
        }[promotion.group(2)]
        return promotion.group(1) + piece
    # If only "promote to queen" is supplied and exactly one legal queen
    # promotion exists, it is deterministic; otherwise python-chess must reject it.
    promotion_only = re.fullmatch(r"promote(?: pawn)?(?: to)? (queen|rook|bishop|knight)", lower)
    if promotion_only:
        ptype = {
            "queen": chess.QUEEN,
            "rook": chess.ROOK,
            "bishop": chess.BISHOP,
            "knight": chess.KNIGHT,
        }[promotion_only.group(1)]
        choices = [m for m in board.legal_moves if m.promotion == ptype]
        if len(choices) == 1:
            return choices[0].uci()
    return value


def _parse_move(board: chess.Board, text: str) -> chess.Move:
    value = _normalize_move_text(text, board)
    if not value:
        raise ValueError("move is required")
    try:
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", value.lower()):
            move = chess.Move.from_uci(value.lower())
            if move not in board.legal_moves:
                raise chess.IllegalMoveError(f"illegal uci: {value}")
            return move
        return board.parse_san(value)
    except chess.AmbiguousMoveError as exc:
        raise ValueError("That move is ambiguous; include the starting file/rank or use UCI.") from exc
    except (chess.InvalidMoveError, chess.IllegalMoveError, ValueError) as exc:
        raise ValueError("That move is not legal in the saved position.") from exc


def _legal_alternatives(board: chess.Board, attempted: str, limit: int = 5) -> list[str]:
    sans = [board.san(move) for move in board.legal_moves]
    close = difflib.get_close_matches(attempted, sans, n=limit, cutoff=0.25)
    return close or sans[: min(limit, len(sans))]


def _material(board: chess.Board) -> dict[str, int]:
    values = {
        chess.PAWN: 1,
        chess.KNIGHT: 3,
        chess.BISHOP: 3,
        chess.ROOK: 5,
        chess.QUEEN: 9,
    }
    white = sum(len(board.pieces(piece, chess.WHITE)) * value for piece, value in values.items())
    black = sum(len(board.pieces(piece, chess.BLACK)) * value for piece, value in values.items())
    return {"white": white, "black": black, "balance_white_minus_black": white - black}


def _pv_san(board: chess.Board, pv: list[chess.Move], maximum: int = 8) -> list[str]:
    work = board.copy(stack=True)
    out: list[str] = []
    for move in pv[:maximum]:
        if move not in work.legal_moves:
            break
        out.append(work.san(move))
        work.push(move)
    return out


def _render_board(board: chess.Board, human_color: str, game: dict[str, Any]) -> str:
    symbols = {
        "K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
        "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟",
    }
    if human_color == "black":
        ranks = range(1, 9)
        files = range(7, -1, -1)
    else:
        ranks = range(8, 0, -1)
        files = range(8)
    file_labels = [chess.FILE_NAMES[f] for f in files]
    lines = ["    " + "   ".join(file_labels)]
    for rank in ranks:
        cells: list[str] = []
        for file_index in files:
            piece = board.piece_at(chess.square(file_index, rank - 1))
            cells.append(symbols.get(piece.symbol(), piece.symbol()) if piece else "·")
        lines.append(f" {rank}  " + "   ".join(cells) + f"  {rank}")
    lines.append("    " + "   ".join(file_labels))
    turn = _color_name(board.turn).title()
    status = game.get("game_status") or "active"
    last = game.get("last_move_san") or "—"
    lines.extend([
        f"Human: {human_color.title()} | Engine: {game['engine_color'].title()}",
        f"Difficulty: {game.get('difficulty_label') or game.get('difficulty_name')}",
        f"Last move: {last} | Turn: {turn}",
        f"Status: {status}" + (f" | Result: {game.get('result')}" if game.get("result") else ""),
    ])
    return '\n'.join(lines)


class ChessService:
    def __init__(
        self,
        config: PluginConfig,
        *,
        engine_runner: Callable[[chess.Board, dict[str, Any], bool], EngineResult] | None = None,
    ) -> None:
        self.config = config
        self.db = ChessDatabase(config.db_path, config.busy_timeout_ms)
        self.engine = StockfishRunner(config, fake=engine_runner)

    def _get_current_ply(self, game_id: int) -> int:
        """Get the current ply number (completed half-moves) for a game."""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0",
                (game_id,),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    @classmethod
    def from_environment(cls) -> "ChessService":
        return cls(PluginConfig.from_environment())

    def health(self) -> dict[str, Any]:
        engine = self.engine.probe()
        integrity = self.db.integrity()
        return {
            "ready": bool(
                engine.get("ready")
                and integrity["integrity"] == "ok"
                and integrity["foreign_key_errors"] == 0
            ),
            "engine": engine,
            "database": str(self.db.path),
            "database_integrity": integrity,
        }

    def _validate_args(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        unknown = set(args) - ALLOWED_KEYS
        if unknown:
            raise ValueError(f"unsupported arguments: {', '.join(sorted(unknown))}")
        action = args.get("action")
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise ValueError("invalid chess action")
        if "move" in args and not isinstance(args["move"], str):
            raise ValueError("move must be text")
        if "move" in args and len(args["move"]) > 64:
            raise ValueError("move is too long")
        if "detail" in args and (
            not isinstance(args["detail"], str) or len(args["detail"]) > 80
        ):
            raise ValueError("detail must be short text")
        if "game_id" in args and (
            isinstance(args["game_id"], bool)
            or not isinstance(args["game_id"], int)
            or args["game_id"] < 1
        ):
            raise ValueError("game_id must be a positive integer")
        if "confirm" in args and not isinstance(args["confirm"], bool):
            raise ValueError("confirm must be true or false")
        return action, dict(args)

    def _processed(self, identity: Identity, action: str, args: dict[str, Any]):
        if not identity.message_id:
            return None
        conn = self.db.connect()
        try:
            row = conn.execute(
                """
                SELECT request_hash,response_json FROM processed_messages
                WHERE owner_key=? AND message_id=? AND action=?
                """,
                (identity.owner_key, identity.message_id, action),
            ).fetchone()
            if not row:
                return None
            if row["request_hash"] != _request_hash(args):
                return {
                    "success": False,
                    "error": "This message ID was already processed with different chess arguments.",
                    "duplicate": True,
                }
            result = json.loads(row["response_json"])
            result["duplicate"] = True
            return result
        finally:
            conn.close()

    def _record_processed(
        self, identity: Identity, action: str, args: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not identity.message_id or not result.get("success"):
            return
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages(
                    owner_key,message_id,action,request_hash,response_json,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    identity.owner_key,
                    identity.message_id,
                    action,
                    _request_hash(args),
                    _json_text(result),
                    utc_now(),
                ),
            )

    def dispatch(self, identity: Identity, args: dict[str, Any]) -> dict[str, Any]:
        try:
            action, clean = self._validate_args(args)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        with _owner_lock(identity.owner_key):
            duplicate = self._processed(identity, action, clean)
            if duplicate is not None:
                return duplicate
            try:
                method = getattr(self, f"_action_{action}")
                result = method(identity, clean)
            except ValueError as exc:
                result = {"success": False, "error": str(exc)}
            except sqlite3.Error:
                result = {
                    "success": False,
                    "error": "The chess database is temporarily busy; retry safely.",
                    "recoverable": True,
                }
            except Exception:
                result = {
                    "success": False,
                    "error": "Chess encountered an operational error; saved state was preserved.",
                    "recoverable": True,
                }
            self._record_processed(identity, action, clean, result)
            return result

    def _select_game(
        self, identity: Identity, args: dict[str, Any], *, require_active: bool = True
    ) -> dict[str, Any]:
        game_id = args.get("game_id")
        row = (
            self.db.owned_game(identity.owner_key, game_id)
            if game_id
            else self.db.active_game(identity.owner_key)
        )
        game = _row_dict(row)
        if game is None:
            raise ValueError("No chess game is available for this messaging identity.")
        if require_active and game["state"] not in {"setup", "active"}:
            raise ValueError("That game is complete. Start a new game or request a rematch.")
        return game

    def _decorate_game(self, game: dict[str, Any]) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            last = conn.execute(
                "SELECT san,uci,actor FROM moves WHERE game_id=? AND undone=0 ORDER BY id DESC LIMIT 1",
                (game["id"],),
            ).fetchone()
        finally:
            conn.close()
        game = dict(game)
        game["last_move_san"] = last["san"] if last else None
        game["last_move_uci"] = last["uci"] if last else None
        game["last_move_actor"] = last["actor"] if last else None
        settings = json.loads(game["engine_settings_json"]) if game.get("engine_settings_json") else {}
        game["difficulty_label"] = settings.get("label") or game.get("difficulty_name")
        return game

    def _payload(self, game: dict[str, Any], *, include_board: bool = True) -> dict[str, Any]:
        game = self._decorate_game(game)
        board = chess.Board(game["current_fen"])
        result = {
            "success": True,
            "game_id": game["id"],
            "state": game["state"],
            "status": game["game_status"],
            "human_color": game["human_color"],
            "engine_color": game["engine_color"],
            "difficulty": game["difficulty_label"],
            "requested_elo": game["requested_elo"],
            "effective_target_elo": game["effective_target_elo"],
            "turn": _color_name(board.turn),
            "human_to_move": game["human_color"] == _color_name(board.turn),
            "pending_engine_move": bool(game["pending_engine"]),
            "last_move": game["last_move_san"],
            "check": board.is_check(),
            "result": game["result"],
            "termination_reason": game["termination_reason"],
        }
        if include_board and game.get("human_color"):
            result["board"] = _render_board(board, game["human_color"], game)
            # Render and attach PNG
            last_move = None
            if game.get("last_move_uci"):
                try:
                    last_move = chess.Move.from_uci(game["last_move_uci"])
                except Exception:
                    pass
            human_color = chess.WHITE if game["human_color"] == "white" else chess.BLACK
            ply = self._get_current_ply(game["id"])
            try:
                image_path = render_board_png(
                    board=board,
                    human_color=human_color,
                    last_move=last_move,
                    game_id=game["id"],
                    ply=ply,
                    config=self.config,
                )
                result["image_path"] = str(image_path)
                result["image_mime_type"] = "image/png"
                result["send_image_first"] = True
            except RenderingError as e:
                logger.warning("Failed to render board PNG: %s", e)
                # Continue without image - text board is fallback
        return result

    def _create_setup(self, identity: Identity) -> dict[str, Any]:
        now = utc_now()
        today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
        board = chess.Board()
        with self.db.transaction(immediate=True) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM games WHERE active=1"
            ).fetchone()[0]
            if count >= self.config.max_active_games:
                raise ValueError("The configured maximum number of active chess games is reached.")
            cursor = conn.execute(
                """
                INSERT INTO games(
                    owner_key,active,state,profile,platform,source,session_key,
                    chat_id,thread_id,user_id,display_name,current_fen,starting_fen,
                    turn,game_status,created_at,updated_at,pgn_date
                ) VALUES(?,1,'setup',?,?,?,?,?,?,?,?,?,?,?,'setup',?,?,?)
                """,
                (
                    identity.owner_key,
                    identity.profile,
                    identity.platform,
                    identity.source,
                    identity.session_key,
                    identity.chat_id,
                    identity.thread_id,
                    identity.user_id,
                    identity.display_name,
                    board.fen(),
                    board.fen(),
                    "white",
                    now,
                    now,
                    today,
                ),
            )
            game_id = int(cursor.lastrowid)
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game_id,
                event_type="setup_created",
                message_id=identity.message_id,
            )
        return _row_dict(self.db.owned_game(identity.owner_key, game_id)) or {}

    def _archive_live_game(self, conn: sqlite3.Connection, game: dict[str, Any]) -> None:
        now = utc_now()
        conn.execute(
            """
            UPDATE games SET active=0,state='archived',game_status='archived',
                termination_reason=COALESCE(termination_reason,'superseded by new game'),
                completed_at=COALESCE(completed_at,?),updated_at=?
            WHERE id=? AND owner_key=? AND active=1
            """,
            (now, now, game["id"], game["owner_key"]),
        )
        self.db.event(
            conn,
            owner_key=game["owner_key"],
            game_id=game["id"],
            event_type="archived_for_new_game",
        )

    def _apply_setup_choices(
        self, identity: Identity, game: dict[str, Any], args: dict[str, Any]
    ) -> dict[str, Any]:
        difficulty = None
        if "difficulty" in args:
            probe = self.engine.probe()
            difficulty = parse_difficulty(
                args["difficulty"],
                default_name=self.config.default_difficulty,
                engine_elo_min=int(probe.get("uci_elo_min", 1320)),
                engine_elo_max=int(probe.get("uci_elo_max", 3190)),
            )
        color = parse_color(args["color"]) if "color" in args else None
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=? AND active=1",
                (game["id"], identity.owner_key),
            ).fetchone()
            if not current or current["state"] != "setup":
                raise ValueError("Chess setup is no longer active.")
            if difficulty:
                conn.execute(
                    """
                    UPDATE games SET difficulty_name=?,requested_elo=?,
                        effective_target_elo=?,engine_settings_json=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        difficulty["name"],
                        difficulty["requested_elo"],
                        difficulty["effective_elo"],
                        _json_text(difficulty["settings"]),
                        utc_now(),
                        game["id"],
                    ),
                )
                self.db.event(
                    conn,
                    owner_key=identity.owner_key,
                    game_id=game["id"],
                    event_type="difficulty_selected",
                    details={
                        "name": difficulty["name"],
                        "requested_elo": difficulty["requested_elo"],
                        "effective_elo": difficulty["effective_elo"],
                    },
                    message_id=identity.message_id,
                )
            if color:
                conn.execute(
                    "UPDATE games SET requested_color=?,updated_at=? WHERE id=?",
                    (color, utc_now(), game["id"]),
                )
                self.db.event(
                    conn,
                    owner_key=identity.owner_key,
                    game_id=game["id"],
                    event_type="color_selected",
                    details={"color": color},
                    message_id=identity.message_id,
                )
        updated = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or {}
        if difficulty:
            updated["_difficulty_note"] = difficulty["note"]
            updated["_difficulty_clamped"] = difficulty["clamped"]
        return updated

    def _finalize_setup(self, identity: Identity, game: dict[str, Any]) -> dict[str, Any]:
        missing = []
        if not game.get("difficulty_name"):
            missing.append("difficulty")
        if not game.get("requested_color"):
            missing.append("color")
        if missing:
            return {
                "success": True,
                "setup": True,
                "started": False,
                "game_id": game["id"],
                "missing_choices": missing,
                "difficulty": game.get("difficulty_name"),
                "color": game.get("requested_color"),
                "message": "Chess setup needs: " + " and ".join(missing) + ".",
            }
        human = game["requested_color"]
        if human == "random":
            human = secrets.choice(["white", "black"])
        engine_color = "black" if human == "white" else "white"
        settings = json.loads(game["engine_settings_json"])
        human_name = game.get("display_name") or "Human"
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=? AND active=1",
                (game["id"], identity.owner_key),
            ).fetchone()
            if not current or current["state"] != "setup":
                raise ValueError("Chess setup is no longer active.")
            conn.execute(
                """
                UPDATE games SET state='active',game_status='active',
                    human_color=?,engine_color=?,pgn_white=?,pgn_black=?,
                    pending_engine=?,updated_at=?
                WHERE id=?
                """,
                (
                    human,
                    engine_color,
                    human_name if human == "white" else f"Stockfish ({settings.get('label')})",
                    human_name if human == "black" else f"Stockfish ({settings.get('label')})",
                    1 if human == "black" else 0,
                    utc_now(),
                    game["id"],
                ),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="game_started",
                details={"human_color": human, "difficulty": game["difficulty_name"]},
                message_id=identity.message_id,
            )
        started = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or {}
        if human == "black":
            recovered = self._engine_turn(identity, started)
            if not recovered.get("success"):
                recovered.update({
                    "setup": False,
                    "started": True,
                    "human_color": human,
                    "difficulty": settings.get("label"),
                })
                return recovered
            started = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or {}
        payload = self._payload(started)
        payload.update({
            "setup": False,
            "started": True,
            "message": (
                "Game started. Stockfish has moved; it is your turn."
                if human == "black"
                else "Game started. You have White; make the first move."
            ),
            "difficulty_note": game.get("_difficulty_note"),
            "difficulty_clamped": bool(game.get("_difficulty_clamped")),
        })
        return payload

    def _setup_or_start(self, identity: Identity, args: dict[str, Any], *, action: str):
        game = _row_dict(self.db.active_game(identity.owner_key))
        supplied_complete = "difficulty" in args and "color" in args
        if game and game["state"] == "active":
            if action == "start" and supplied_complete:
                with self.db.transaction(immediate=True) as conn:
                    current = dict(conn.execute(
                        "SELECT * FROM games WHERE id=? AND owner_key=?",
                        (game["id"], identity.owner_key),
                    ).fetchone())
                    self._archive_live_game(conn, current)
                game = None
            else:
                payload = self._payload(game)
                payload.update({
                    "existing_game": True,
                    "message": "An active game already exists. Resume it or start with complete new settings.",
                })
                return payload
        if game is None:
            game = self._create_setup(identity)
        if game["state"] != "setup":
            raise ValueError("Existing chess state cannot be configured.")
        game = self._apply_setup_choices(identity, game, args)
        return self._finalize_setup(identity, game)

    def _action_setup(self, identity: Identity, args: dict[str, Any]):
        return self._setup_or_start(identity, args, action="setup")

    def _action_start(self, identity: Identity, args: dict[str, Any]):
        return self._setup_or_start(identity, args, action="start")

    def _claim_engine_turn(self, identity: Identity, game_id: int):
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=?",
                (game_id, identity.owner_key),
            ).fetchone()
            if not row:
                raise ValueError("Game not found for this identity.")
            game = dict(row)
            if game["state"] != "active" or not game["pending_engine"]:
                return None, game
            if game.get("engine_claim_token") and game.get("engine_claimed_at"):
                try:
                    claimed = datetime.fromisoformat(game["engine_claimed_at"])
                    age = (now - claimed).total_seconds()
                except (ValueError, TypeError):
                    age = self.config.engine_timeout * 3
                if age < self.config.engine_timeout * 2 + 2:
                    return "busy", game
            conn.execute(
                "UPDATE games SET engine_claim_token=?,engine_claimed_at=?,updated_at=? WHERE id=?",
                (token, now.isoformat(timespec="seconds"), utc_now(), game_id),
            )
        return token, game

    def _engine_turn(self, identity: Identity, game: dict[str, Any]) -> dict[str, Any]:
        token, claimed = self._claim_engine_turn(identity, int(game["id"]))
        if token is None:
            return {"success": True, "engine_move": None}
        if token == "busy":
            return {
                "success": False,
                "error": "Stockfish is already calculating this saved position; retry continue shortly.",
                "recoverable": True,
                "pending_engine_move": True,
            }
        board = chess.Board(claimed["current_fen"])
        engine_color = claimed["engine_color"]
        if _color_name(board.turn) != engine_color:
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    "UPDATE games SET engine_claim_token=NULL,engine_claimed_at=NULL WHERE id=? AND engine_claim_token=?",
                    (claimed["id"], token),
                )
            raise ValueError("Saved pending-engine state is inconsistent with the board turn.")
        settings = json.loads(claimed["engine_settings_json"])
        try:
            calculation = self.engine.calculate(board, settings, analysis=False)
        except EngineUnavailable:
            with self.db.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE games SET engine_claim_token=NULL,engine_claimed_at=NULL,updated_at=?
                    WHERE id=? AND owner_key=? AND engine_claim_token=?
                    """,
                    (utc_now(), claimed["id"], identity.owner_key, token),
                )
                self.db.event(
                    conn,
                    owner_key=identity.owner_key,
                    game_id=claimed["id"],
                    event_type="engine_failure",
                    details={"recoverable": True},
                    message_id=identity.message_id,
                )
            return {
                "success": False,
                "error": "Stockfish could not complete its move. The position is saved; use continue to retry.",
                "recoverable": True,
                "pending_engine_move": True,
            }
        fen_before = board.fen()
        move = calculation.move
        san = board.san(move)
        board.push(move)
        outcome = _outcome_fields(board)
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=?",
                (claimed["id"], identity.owner_key),
            ).fetchone()
            if (
                not current
                or current["engine_claim_token"] != token
                or not current["pending_engine"]
                or current["current_fen"] != fen_before
            ):
                return {
                    "success": False,
                    "error": "The saved position changed before the engine move committed; no duplicate move was written.",
                    "recoverable": True,
                }
            active_ply = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0",
                (claimed["id"],),
            ).fetchone()[0] + 1
            conn.execute(
                """
                INSERT INTO moves(
                    game_id,ply_number,actor,san,uci,fen_before,fen_after,created_at,
                    engine_evaluation_cp,engine_mate,engine_depth,engine_limit_json
                ) VALUES(?,?,'engine',?,?,?,?,?,?,?,?,?)
                """,
                (
                    claimed["id"],
                    active_ply,
                    san,
                    move.uci(),
                    fen_before,
                    board.fen(),
                    utc_now(),
                    calculation.score_cp,
                    calculation.mate,
                    calculation.depth,
                    _json_text(calculation.limit),
                ),
            )
            conn.execute(
                """
                UPDATE games SET current_fen=?,turn=?,game_status=?,result=?,
                    termination_reason=?,state=?,active=?,completed_at=?,
                    pending_engine=0,engine_claim_token=NULL,engine_claimed_at=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (
                    board.fen(),
                    _color_name(board.turn),
                    outcome["game_status"],
                    outcome["result"],
                    outcome["termination_reason"],
                    "completed" if outcome["completed"] else "active",
                    0 if outcome["completed"] else 1,
                    utc_now() if outcome["completed"] else None,
                    utc_now(),
                    claimed["id"],
                ),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=claimed["id"],
                event_type="engine_move",
                details={"san": san, "uci": move.uci()},
                message_id=identity.message_id,
            )
            if outcome["completed"]:
                self.db.event(
                    conn,
                    owner_key=identity.owner_key,
                    game_id=claimed["id"],
                    event_type="completion",
                    details={
                        "result": outcome["result"],
                        "termination": outcome["termination_reason"],
                    },
                )
        payload = {
            "success": True,
            "engine_move": san,
            "engine_move_uci": move.uci(),
            "engine_evaluation_cp": calculation.score_cp,
            "engine_mate": calculation.mate,
            "board": _render_board(board, game["human_color"], self._decorate_game(claimed)),
            "message": f"Black plays {san} in response to your move.",
        }
        # Add image to engine turn response
        try:
            last_move = move
            human_color = chess.WHITE if game["human_color"] == "white" else chess.BLACK
            ply = self._get_current_ply(game["id"])
            image_path = render_board_png(
                board=board,
                human_color=human_color,
                last_move=last_move,
                game_id=game["id"],
                ply=ply,
                config=self.config,
            )
            payload["image_path"] = str(image_path)
            payload["image_mime_type"] = "image/png"
            payload["send_image_first"] = True
        except RenderingError as e:
            logger.warning("Failed to render board PNG in engine turn: %s", e)
        return payload

    def _recover(self, identity: Identity, game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if game["state"] == "active" and game["pending_engine"]:
            result = self._engine_turn(identity, game)
            refreshed = _row_dict(self.db.owned_game(identity.owner_key, game["id"]))
            if result.get("success") and result.get("engine_move"):
                with self.db.transaction(immediate=True) as conn:
                    self.db.event(
                        conn,
                        owner_key=identity.owner_key,
                        game_id=game["id"],
                        event_type="interrupted_turn_recovered",
                        details={"engine_move": result["engine_move"]},
                        message_id=identity.message_id,
                    )
            return refreshed or game, result
        return game, None

    def _action_move(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        if game["state"] == "setup":
            raise ValueError("Finish chess setup before making a move.")
        # If this delivery already committed its human move but crashed before
        # processed_messages, recover from the durable move row instead of replaying.
        if identity.message_id:
            conn = self.db.connect()
            try:
                prior = conn.execute(
                    """
                    SELECT san FROM moves
                    WHERE game_id=? AND actor='human' AND triggering_message_id=? AND undone=0
                    ORDER BY id DESC LIMIT 1
                    """,
                    (game["id"], identity.message_id),
                ).fetchone()
            finally:
                conn.close()
            if prior:
                game, engine_result = self._recover(identity, game)
                if engine_result and not engine_result.get("success"):
                    return engine_result
                payload = self._payload(game)
                payload.update({
                    "human_move": prior["san"],
                    "engine_move": (engine_result or {}).get("engine_move"),
                    "duplicate_human_move_prevented": True,
                    "message": "The saved human move was not replayed; the pending turn was resumed.",
                })
                if engine_result and engine_result.get("board"):
                    payload["board"] = engine_result["board"]
                return payload
        game, recovery = self._recover(identity, game)
        if recovery and not recovery.get("success"):
            return recovery
        if game["state"] != "active":
            raise ValueError("The game is complete.")
        board = chess.Board(game["current_fen"])
        if _color_name(board.turn) != game["human_color"]:
            raise ValueError("It is not the human player's turn.")
        raw_move = args.get("move", "")
        try:
            move = _parse_move(board, raw_move)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "alternatives": _legal_alternatives(board, str(raw_move)),
                "turn_preserved": True,
            }
        fen_before = board.fen()
        san = board.san(move)
        board.push(move)
        outcome = _outcome_fields(board)
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=? AND active=1",
                (game["id"], identity.owner_key),
            ).fetchone()
            if not current or current["current_fen"] != fen_before or current["pending_engine"]:
                raise ValueError("The saved game changed; reload the board before moving.")
            active_ply = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0",
                (game["id"],),
            ).fetchone()[0] + 1
            conn.execute(
                """
                INSERT INTO moves(
                    game_id,ply_number,actor,san,uci,fen_before,fen_after,created_at,
                    triggering_message_id
                ) VALUES(?,?,'human',?,?,?,?,?,?)
                """,
                (
                    game["id"],
                    active_ply,
                    san,
                    move.uci(),
                    fen_before,
                    board.fen(),
                    utc_now(),
                    identity.message_id or None,
                ),
            )
            conn.execute(
                """
                UPDATE games SET current_fen=?,turn=?,game_status=?,result=?,
                    termination_reason=?,state=?,active=?,completed_at=?,
                    pending_engine=?,last_processed_message_id=?,updated_at=?
                WHERE id=?
                """,
                (
                    board.fen(),
                    _color_name(board.turn),
                    outcome["game_status"],
                    outcome["result"],
                    outcome["termination_reason"],
                    "completed" if outcome["completed"] else "active",
                    0 if outcome["completed"] else 1,
                    utc_now() if outcome["completed"] else None,
                    0 if outcome["completed"] else 1,
                    identity.message_id or None,
                    utc_now(),
                    game["id"],
                ),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="human_move",
                details={"san": san, "uci": move.uci()},
                message_id=identity.message_id,
            )
        after_human = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or game
        if outcome["completed"]:
            payload = self._payload(after_human)
            payload.update({"human_move": san, "engine_move": None, "message": "Game complete."})
            return payload
        engine_result = self._engine_turn(identity, after_human)
        if not engine_result.get("success"):
            payload = self._payload(after_human)
            payload.update(engine_result)
            payload.update({
                "human_move": san,
                "human_move_saved": True,
                "message": "Your move is saved; Stockfish's reply remains pending.",
            })
            return payload
        final_game = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or after_human
        payload = self._payload(final_game)
        payload.update({
            "human_move": san,
            "human_move_uci": move.uci(),
            "engine_move": engine_result.get("engine_move"),
            "engine_move_uci": engine_result.get("engine_move_uci"),
            "engine_evaluation_cp": engine_result.get("engine_evaluation_cp"),
            "message": f"You played {san}. Stockfish played {engine_result.get('engine_move')}.",
        })
        return payload

    def _read_with_recovery(self, identity: Identity, args: dict[str, Any], message: str):
        game = self._select_game(identity, args)
        if game["state"] == "setup":
            missing = [
                name for name, value in (
                    ("difficulty", game.get("difficulty_name")),
                    ("color", game.get("requested_color")),
                ) if not value
            ]
            return {
                "success": True,
                "setup": True,
                "started": False,
                "game_id": game["id"],
                "missing_choices": missing,
                "difficulty": game.get("difficulty_name"),
                "color": game.get("requested_color"),
                "message": "Chess setup is waiting for: " + " and ".join(missing) + ".",
            }
        game, recovery = self._recover(identity, game)
        if recovery and not recovery.get("success"):
            payload = self._payload(game)
            payload.update(recovery)
            return payload
        payload = self._payload(game)
        payload["message"] = (
            f"{message} Recovered Stockfish move {recovery['engine_move']}."
            if recovery and recovery.get("engine_move")
            else message
        )
        return payload

    def _action_board(self, identity: Identity, args: dict[str, Any]):
        return self._read_with_recovery(identity, args, "Current saved board.")

    def _action_status(self, identity: Identity, args: dict[str, Any]):
        return self._read_with_recovery(identity, args, "Current saved game status.")

    def _action_resume(self, identity: Identity, args: dict[str, Any]):
        return self._read_with_recovery(identity, args, "Resumed the saved game.")

    def _action_legal_moves(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        game, recovery = self._recover(identity, game)
        if recovery and not recovery.get("success"):
            return recovery
        board = chess.Board(game["current_fen"])
        if game["state"] != "active":
            raise ValueError("The game is not active.")
        sans = [board.san(move) for move in board.legal_moves]
        return {
            "success": True,
            "game_id": game["id"],
            "turn": _color_name(board.turn),
            "legal_moves": sans,
            "legal_moves_uci": [move.uci() for move in board.legal_moves],
            "count": len(sans),
        }

    def _analysis(self, game: dict[str, Any]) -> dict[str, Any]:
        board = chess.Board(game["current_fen"])
        settings = json.loads(game["engine_settings_json"])
        settings.update({"analysis_time": 0.45, "analysis_nodes": 60000})
        try:
            result = self.engine.calculate(board, settings, analysis=True)
        except EngineUnavailable:
            return {
                "success": False,
                "error": "Stockfish analysis is temporarily unavailable; the game is unchanged.",
                "recoverable": True,
            }
        pv = _pv_san(board, result.pv)
        return {
            "success": True,
            "evaluation_cp_for_side_to_move": result.score_cp,
            "mate_for_side_to_move": result.mate,
            "best_move": board.san(result.move),
            "best_move_uci": result.move.uci(),
            "principal_variation": pv,
            "material": _material(board),
            "depth": result.depth,
            "check": board.is_check(),
            "tactical_warning": bool(
                result.mate is not None
                or (result.score_cp is not None and abs(result.score_cp) >= 180)
            ),
        }

    def _action_analyze(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        game, recovery = self._recover(identity, game)
        if recovery and not recovery.get("success"):
            return recovery
        analysis = self._analysis(game)
        if not analysis.get("success"):
            return analysis
        conn = self.db.connect()
        try:
            last_engine = conn.execute(
                """
                SELECT san,uci,engine_evaluation_cp,engine_mate,engine_depth
                FROM moves WHERE game_id=? AND actor='engine' AND undone=0
                ORDER BY id DESC LIMIT 1
                """,
                (game["id"],),
            ).fetchone()
        finally:
            conn.close()
        analysis.update({
            "game_id": game["id"],
            "turn": _color_name(chess.Board(game["current_fen"]).turn),
            "last_engine_move": dict(last_engine) if last_engine else None,
            "focus": args.get("detail") or "position",
        })
        return analysis

    def _action_hint(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        game, recovery = self._recover(identity, game)
        if recovery and not recovery.get("success"):
            return recovery
        board = chess.Board(game["current_fen"])
        if _color_name(board.turn) != game["human_color"]:
            raise ValueError("Hints are available on the human player's turn.")
        analysis = self._analysis(game)
        if not analysis.get("success"):
            return analysis
        move = _parse_move(board, analysis["best_move"])
        piece = board.piece_at(move.from_square)
        capture = board.is_capture(move)
        gives_check = board.gives_check(move)
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT hint_count FROM games WHERE id=? AND owner_key=?",
                (game["id"], identity.owner_key),
            ).fetchone()
            count = int(current["hint_count"]) + 1
            conn.execute(
                "UPDATE games SET hint_count=?,updated_at=? WHERE id=?",
                (count, utc_now(), game["id"]),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="hint",
                details={"level": count},
                message_id=identity.message_id,
            )
        if count == 1:
            clue_parts = [
                f"Look for a move by your {chess.piece_name(piece.piece_type) if piece else 'piece'}"
            ]
            if capture:
                clue_parts.append("that wins or exchanges material")
            elif gives_check:
                clue_parts.append("that creates a forcing check")
            else:
                clue_parts.append("that improves activity or pressure")
            clue = " ".join(clue_parts) + "."
            return {"success": True, "game_id": game["id"], "hint_level": 1, "clue": clue}
        return {
            "success": True,
            "game_id": game["id"],
            "hint_level": count,
            "clue": f"A stronger hint: consider {analysis['best_move']}.",
            "best_move": analysis["best_move"],
            "principal_variation": analysis["principal_variation"][:4],
        }

    def _action_undo(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        if game["state"] != "active":
            raise ValueError("Only an active game can be undone.")
        with self.db.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM games WHERE id=? AND owner_key=? AND active=1",
                (game["id"], identity.owner_key),
            ).fetchone()
            rows = conn.execute(
                "SELECT * FROM moves WHERE game_id=? AND undone=0 ORDER BY id DESC",
                (game["id"],),
            ).fetchall()
            if not rows:
                raise ValueError("There are no moves to undo.")
            selected: list[sqlite3.Row] = []
            if current["pending_engine"] and rows[0]["actor"] == "human":
                selected = [rows[0]]
            else:
                if rows[0]["actor"] == "engine":
                    selected.append(rows[0])
                    if len(rows) > 1 and rows[1]["actor"] == "human":
                        selected.append(rows[1])
                elif rows[0]["actor"] == "human":
                    selected.append(rows[0])
            restore = selected[-1]["fen_before"]
            now = utc_now()
            conn.executemany(
                "UPDATE moves SET undone=1,undone_at=? WHERE id=?",
                [(now, row["id"]) for row in selected],
            )
            board = chess.Board(restore)
            conn.execute(
                """
                UPDATE games SET current_fen=?,turn=?,pending_engine=0,
                    engine_claim_token=NULL,engine_claimed_at=NULL,state='active',
                    active=1,game_status=?,result=NULL,termination_reason=NULL,
                    completed_at=NULL,hint_count=0,updated_at=?
                WHERE id=?
                """,
                (
                    restore,
                    _color_name(board.turn),
                    "check" if board.is_check() else "active",
                    now,
                    game["id"],
                ),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="undo",
                details={"moves": [row["san"] for row in reversed(selected)]},
                message_id=identity.message_id,
            )
        refreshed = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or game
        payload = self._payload(refreshed)
        payload.update({
            "undone_moves": [row["san"] for row in reversed(selected)],
            "message": "Undid " + ", ".join(row["san"] for row in reversed(selected)) + ".",
        })
        return payload

    def _complete_manual(
        self,
        identity: Identity,
        game: dict[str, Any],
        *,
        result: str,
        reason: str,
        event_type: str,
    ):
        with self.db.transaction(immediate=True) as conn:
            now = utc_now()
            conn.execute(
                """
                UPDATE games SET active=0,state='completed',game_status='completed',
                    result=?,termination_reason=?,completed_at=?,updated_at=?,
                    pending_engine=0,engine_claim_token=NULL,engine_claimed_at=NULL
                WHERE id=? AND owner_key=? AND active=1
                """,
                (result, reason, now, now, game["id"], identity.owner_key),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type=event_type,
                details={"result": result, "reason": reason},
                message_id=identity.message_id,
            )
        completed = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or game
        payload = self._payload(completed)
        payload["message"] = f"Game ended by {reason}. Result: {result}."
        return payload

    def _action_resign(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        result = "0-1" if game["human_color"] == "white" else "1-0"
        return self._complete_manual(
            identity, game, result=result, reason="human resignation", event_type="resignation"
        )

    def _action_offer_draw(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        analysis = self._analysis(game)
        if not analysis.get("success"):
            return analysis
        conn = self.db.connect()
        try:
            ply = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0", (game["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        score = analysis.get("evaluation_cp_for_side_to_move")
        accept = ply >= 20 and score is not None and abs(score) <= 40
        with self.db.transaction(immediate=True) as conn:
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="draw_offer",
                details={"accepted": accept},
                message_id=identity.message_id,
            )
            conn.execute(
                "UPDATE games SET draw_offer_by=?,updated_at=? WHERE id=?",
                (None if accept else None, utc_now(), game["id"]),
            )
        if accept:
            return self._complete_manual(
                identity, game, result="1/2-1/2", reason="draw agreement",
                event_type="draw_accepted",
            )
        return {
            "success": True,
            "game_id": game["id"],
            "accepted": False,
            "message": "Stockfish declines the draw offer.",
        }

    def _action_accept_draw(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        if game.get("draw_offer_by") != "engine":
            raise ValueError("There is no engine draw offer to accept.")
        return self._complete_manual(
            identity, game, result="1/2-1/2", reason="draw agreement",
            event_type="draw_accepted",
        )

    def _action_decline_draw(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        if game.get("draw_offer_by") != "engine":
            raise ValueError("There is no engine draw offer to decline.")
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE games SET draw_offer_by=NULL,updated_at=? WHERE id=?",
                (utc_now(), game["id"]),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="draw_declined",
                message_id=identity.message_id,
            )
        return {"success": True, "game_id": game["id"], "message": "Draw offer declined."}

    def _difficulty_update(
        self, identity: Identity, game: dict[str, Any], args: dict[str, Any]
    ):
        if "difficulty" not in args:
            raise ValueError("difficulty is required")
        probe = self.engine.probe()
        parsed = parse_difficulty(
            args["difficulty"],
            default_name=self.config.default_difficulty,
            engine_elo_min=int(probe.get("uci_elo_min", 1320)),
            engine_elo_max=int(probe.get("uci_elo_max", 3190)),
        )
        conn = self.db.connect()
        try:
            move_count = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0", (game["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        if move_count and not args.get("confirm"):
            return {
                "success": False,
                "confirmation_required": True,
                "error": (
                    "Difficulty cannot change silently after play begins. "
                    "Confirm the change or start a new game."
                ),
                "current_difficulty": self._decorate_game(game)["difficulty_label"],
                "requested_difficulty": parsed["label"],
            }
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE games SET difficulty_name=?,requested_elo=?,
                    effective_target_elo=?,engine_settings_json=?,updated_at=?
                WHERE id=? AND owner_key=?
                """,
                (
                    parsed["name"],
                    parsed["requested_elo"],
                    parsed["effective_elo"],
                    _json_text(parsed["settings"]),
                    utc_now(),
                    game["id"],
                    identity.owner_key,
                ),
            )
            self.db.event(
                conn,
                owner_key=identity.owner_key,
                game_id=game["id"],
                event_type="difficulty_changed",
                details={
                    "name": parsed["name"],
                    "effective_elo": parsed["effective_elo"],
                    "midgame": bool(move_count),
                },
                message_id=identity.message_id,
            )
        return {
            "success": True,
            "game_id": game["id"],
            "difficulty": parsed["label"],
            "effective_target_elo": parsed["effective_elo"],
            "clamped": parsed["clamped"],
            "note": parsed["note"],
            "message": f"Difficulty set to {parsed['label']}.",
        }

    def _action_set_difficulty(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        return self._difficulty_update(identity, game, args)

    def _action_get_difficulty(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args, require_active=False)
        decorated = self._decorate_game(game)
        return {
            "success": True,
            "game_id": game["id"],
            "difficulty": decorated["difficulty_label"],
            "difficulty_name": game["difficulty_name"],
            "requested_elo": game["requested_elo"],
            "effective_target_elo": game["effective_target_elo"],
            "engine_settings": json.loads(game["engine_settings_json"] or "{}"),
            "message": f"Saved difficulty: {decorated['difficulty_label']}.",
        }

    def _action_set_color(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args)
        if "color" not in args:
            raise ValueError("color is required")
        color = parse_color(args["color"])
        conn = self.db.connect()
        try:
            moves = conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0", (game["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        if game["state"] != "setup" and moves:
            raise ValueError("Color cannot change after play begins; start a new game.")
        if game["state"] == "setup":
            updated = self._apply_setup_choices(identity, game, {"color": color})
            return self._finalize_setup(identity, updated)
        human = secrets.choice(["white", "black"]) if color == "random" else color
        engine = "black" if human == "white" else "white"
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE games SET requested_color=?,human_color=?,engine_color=?,
                    pending_engine=?,updated_at=? WHERE id=?
                """,
                (color, human, engine, 1 if human == "black" else 0, utc_now(), game["id"]),
            )
        refreshed = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or game
        if human == "black":
            engine_result = self._engine_turn(identity, refreshed)
            if not engine_result.get("success"):
                return engine_result
            refreshed = _row_dict(self.db.owned_game(identity.owner_key, game["id"])) or refreshed
        return self._payload(refreshed)

    def _build_pgn(self, game: dict[str, Any]) -> str:
        pgn = chess.pgn.Game()
        pgn.headers["Event"] = game["pgn_event"]
        pgn.headers["Site"] = game["pgn_site"]
        pgn.headers["Date"] = game["pgn_date"]
        pgn.headers["Round"] = game["pgn_round"]
        pgn.headers["White"] = game["pgn_white"] or "White"
        pgn.headers["Black"] = game["pgn_black"] or "Black"
        pgn.headers["Result"] = game["result"] or "*"
        pgn.headers["Difficulty"] = self._decorate_game(game)["difficulty_label"] or "Unknown"
        if game.get("termination_reason"):
            pgn.headers["Termination"] = game["termination_reason"]
        if game["starting_fen"] != chess.STARTING_FEN:
            pgn.headers["SetUp"] = "1"
            pgn.headers["FEN"] = game["starting_fen"]
        board = chess.Board(game["starting_fen"])
        node = pgn
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT uci FROM moves WHERE game_id=? AND undone=0 ORDER BY id",
                (game["id"],),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            move = chess.Move.from_uci(row["uci"])
            if move not in board.legal_moves:
                raise ValueError("Saved move history failed PGN consistency validation.")
            node = node.add_variation(move)
            board.push(move)
        exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
        return pgn.accept(exporter)

    def _action_pgn(self, identity: Identity, args: dict[str, Any]):
        game = self._select_game(identity, args, require_active=False)
        return {
            "success": True,
            "game_id": game["id"],
            "pgn": self._build_pgn(game),
            "result": game["result"] or "*",
        }

    def _action_list_games(self, identity: Identity, args: dict[str, Any]):
        conn = self.db.connect()
        try:
            rows = conn.execute(
                """
                SELECT id,state,human_color,difficulty_name,requested_elo,
                    effective_target_elo,result,termination_reason,created_at,
                    updated_at,completed_at
                FROM games WHERE owner_key=? ORDER BY id DESC LIMIT 25
                """,
                (identity.owner_key,),
            ).fetchall()
        finally:
            conn.close()
        return {"success": True, "games": [dict(row) for row in rows], "count": len(rows)}

    def _latest_game(self, identity: Identity):
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM games WHERE owner_key=? ORDER BY id DESC LIMIT 1",
                (identity.owner_key,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _action_rematch(self, identity: Identity, args: dict[str, Any]):
        previous = self._latest_game(identity)
        if not previous or not previous.get("human_color") or not previous.get("difficulty_name"):
            raise ValueError("No prior configured game is available for a rematch.")
        mode = args.get("rematch_mode", "same")
        if mode not in {"same", "swap"}:
            raise ValueError("rematch_mode must be same or swap")
        color = previous["human_color"]
        if mode == "swap":
            color = "black" if color == "white" else "white"
        difficulty: Any
        if "difficulty" in args:
            difficulty = args["difficulty"]
        elif previous["difficulty_name"] == "custom":
            difficulty = previous["requested_elo"]
        else:
            difficulty = previous["difficulty_name"]
        active = _row_dict(self.db.active_game(identity.owner_key))
        if active:
            with self.db.transaction(immediate=True) as conn:
                current = dict(conn.execute(
                    "SELECT * FROM games WHERE id=? AND owner_key=?",
                    (active["id"], identity.owner_key),
                ).fetchone())
                self._archive_live_game(conn, current)
        game = self._create_setup(identity)
        game = self._apply_setup_choices(
            identity, game, {"difficulty": difficulty, "color": color}
        )
        result = self._finalize_setup(identity, game)
        result["rematch"] = True
        result["rematch_mode"] = mode
        return result

    def _action_help(self, identity: Identity, args: dict[str, Any]):
        return {
            "success": True,
            "actions": sorted(ALLOWED_ACTIONS),
            "difficulties": [data["label"] for data in DIFFICULTIES.values()],
            "examples": [
                "Let's play chess at easy difficulty. I'll be White.",
                "e4",
                "Show me the board.",
                "Continue our chess game.",
                "Why did you play that?",
                "Rematch, but switch colors.",
            ],
            "message": "Use normal chess conversation or /chess help; saved state is local and persistent.",
        }

    def lightweight_context(self, identity: Identity) -> str | None:
        game = _row_dict(self.db.active_game(identity.owner_key))
        if not game:
            return None
        if game["state"] == "setup":
            missing = []
            if not game.get("difficulty_name"):
                missing.append("difficulty")
            if not game.get("requested_color"):
                missing.append("color")
            chosen = []
            if game.get("difficulty_name"):
                chosen.append(f"difficulty={self._decorate_game(game)['difficulty_label']}")
            if game.get("requested_color"):
                chosen.append(f"color={game['requested_color']}")
            return (
                "Chess setup is in progress for this messaging identity. "
                + (", ".join(chosen) + ". " if chosen else "")
                + "Still required: "
                + ", ".join(missing)
                + ". Use chess_game; its database is authoritative."
            )
        board = chess.Board(game["current_fen"])
        turn = _color_name(board.turn)
        return (
            "This messaging identity has an active persisted chess game. "
            f"Human={game['human_color']}; difficulty={self._decorate_game(game)['difficulty_label']}; "
            f"turn={turn}; pending_engine={bool(game['pending_engine'])}. "
            "Treat plausible chess notation as a move and use chess_game. "
            "The plugin database is authoritative."
        )


__all__ = [
    "ChessService",
    "Identity",
    "current_identity",
    "_outcome_fields",
    "_parse_move",
]
