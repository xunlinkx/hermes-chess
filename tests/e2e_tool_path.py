#!/usr/bin/env python3
"""Isolated end-to-end check through Hermes plugin discovery and tool dispatch."""

from __future__ import annotations

import json
import sys

from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry


PROFILE = "default"
PLATFORM = "photon"
CHAT = "local-chess-e2e-do-not-deliver"
USER = "local-chess-e2e-user"
SESSION_KEY = "agent:main:photon:dm:local-chess-e2e-do-not-deliver"


def call(args: dict, message_id: str) -> dict:
    tokens = set_session_vars(
        platform=PLATFORM,
        chat_id=CHAT,
        user_id=USER,
        user_name="Local E2E",
        session_key=SESSION_KEY,
        message_id=message_id,
        profile=PROFILE,
    )
    try:
        raw = registry.dispatch("chess_game", args)
        return json.loads(raw)
    finally:
        clear_session_vars(tokens)


def plugin_service():
    # Reach the service through the SAME import the deployed root __init__.py
    # performs (it adds src/ to sys.path then does `from hermes_chess import *`),
    # rather than introspecting a private _get_service on the shim module.
    import hermes_chess

    return hermes_chess._get_service()


def slash(raw_args: str, message_id: str) -> str:
    tokens = set_session_vars(
        platform="local",
        chat_id="local-chess-slash-e2e",
        user_id="local-chess-slash-user",
        user_name="Local Slash E2E",
        session_key="agent:main:local:dm:local-chess-slash-e2e",
        message_id=message_id,
        profile=PROFILE,
    )
    try:
        manager = get_plugin_manager()
        return manager._plugin_commands["chess"]["handler"](raw_args)
    finally:
        clear_session_vars(tokens)


def count_moves(game_id: int, actor: str | None = None) -> int:
    service = plugin_service()
    conn = service.db.connect()
    try:
        if actor:
            return conn.execute(
                "SELECT COUNT(*) FROM moves WHERE game_id=? AND actor=? AND undone=0",
                (game_id, actor),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM moves WHERE game_id=? AND undone=0", (game_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    if "chess_game" not in manager._plugin_tool_names:
        raise RuntimeError("chess_game did not register")

    if phase == "verify":
        pgn = call({"action": "pgn", "game_id": 1}, "e2e-post-stop-pgn")
        assert pgn["success"] and "1. e4" in pgn["pgn"]
        started = slash("start white easy", "slash-start")
        assert "Game started" in started
        moved = slash("e4", "slash-e4")
        assert "Stockfish played" in moved
        tokens = set_session_vars(
            platform="local",
            chat_id="local-chess-slash-e2e",
            user_id="local-chess-slash-user",
            session_key="agent:main:local:dm:local-chess-slash-e2e",
            message_id="slash-hook",
            profile=PROFILE,
        )
        try:
            hook = manager.invoke_hook(
                "pre_llm_call",
                session_id="temporary-conversation-id",
                user_message="Nf3",
                conversation_history=[],
                is_first_turn=False,
                model="test",
            )
        finally:
            clear_session_vars(tokens)
        assert any("active persisted chess game" in str(item) for item in hook)
        ended = slash("resign", "slash-resign")
        assert "human resignation" in ended
        print(json.dumps({
            "phase": "verify",
            "post_stop_start_persistence": True,
            "slash_command": True,
            "pre_llm_hook": True,
        }))
        return 0

    if phase in {"begin", "all"}:
        started = call(
            {"action": "start", "difficulty": "easy", "color": "white"},
            "e2e-start",
        )
        assert started["success"] and started["started"] and started["difficulty"] == "Easy"
        moved = call({"action": "move", "move": "e4"}, "e2e-e4")
        assert moved["success"] and moved["engine_move"]
        board = call({"action": "board"}, "e2e-board")
        assert board["game_id"] == started["game_id"] and board["difficulty"] == "Easy"
        print(json.dumps({
            "phase": "begin",
            "game_id": started["game_id"],
            "engine_move": moved["engine_move"],
            "moves": count_moves(started["game_id"]),
        }))
        if phase == "begin":
            return 0

    resumed = call({"action": "resume"}, "e2e-resume-after-restart")
    assert resumed["success"] and resumed["difficulty"] == "Easy" and resumed["human_to_move"]
    game_id = resumed["game_id"]
    legal = call({"action": "legal_moves"}, "e2e-legal-2")
    second = call({"action": "move", "move": legal["legal_moves"][0]}, "e2e-move-2")
    assert second["success"] and second["engine_move"]

    # Simulate an engine crash after the next human move while keeping the
    # invocation on the registered chess_game path.
    service = plugin_service()
    original_fake = service.engine.fake

    def fail_once(board, settings, analysis):
        from hermes_chess.engine import EngineUnavailable

        raise EngineUnavailable("simulated interruption")

    service.engine.fake = fail_once
    legal = call({"action": "legal_moves"}, "e2e-legal-pending")
    interrupted = call(
        {"action": "move", "move": legal["legal_moves"][0]},
        "e2e-interrupted-human",
    )
    assert not interrupted["success"] and interrupted["human_move_saved"]
    engine_before = count_moves(game_id, "engine")
    service.engine.fake = original_fake
    recovered = call({"action": "resume"}, "e2e-recover")
    assert recovered["success"] and not recovered["pending_engine_move"]
    engine_after = count_moves(game_id, "engine")
    assert engine_after == engine_before + 1
    repeated = call({"action": "resume"}, "e2e-recover-again")
    assert repeated["success"]
    assert count_moves(game_id, "engine") == engine_after

    pgn = call({"action": "pgn", "game_id": game_id}, "e2e-pgn")
    assert pgn["success"] and "1. e4" in pgn["pgn"]
    ended = call({"action": "resign", "game_id": game_id}, "e2e-end")
    assert ended["success"] and ended["result"] in {"0-1", "1-0"}
    print(json.dumps({
        "phase": "resume",
        "game_id": game_id,
        "difficulty": resumed["difficulty"],
        "recovered_exactly_one": True,
        "pgn_ok": True,
        "ended": True,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
