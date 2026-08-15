"""Live Hermes runtime E2E for the hermes-chess plugin.

Exercises the plugin through the *real* Hermes plugin manager, tool registry,
``/chess`` slash command, and ``pre_llm_call`` hook — the same path the live
agent uses — minus the LLM itself (deterministic, no tokens, no delivery).

Run from inside the Hermes install with the runtime venv so that
``hermes_cli`` / ``tools`` / ``gateway`` resolve against the live tree:

    cd ~/.hermes
    .venv/bin/pytest /Users/tronik/Documents/code/hermes-chess/tests/test_live_runtime.py -q

Isolation: each test uses a throwaway SQLite DB (``HERMES_CHESS_DB``) and a
deterministic fake engine, so it never touches production state or sends a
message. Set ``HERMES_CHESS_STOCKFISH=/path/to/stockfish`` to also run one move
through the real engine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("hermes_cli")
pytest.importorskip("tools")
pytest.importorskip("gateway")


PROFILE = "default"
CHAT = "local-chess-live-test-do-not-deliver"
USER = "local-chess-live-test-user"
SLASH_CHAT = "local-chess-live-slash-do-not-deliver"


def _fake_engine(board, settings, analysis):
    """Deterministic engine: always plays the first legal move."""
    from hermes_chess.engine import EngineResult

    move = next(iter(board.legal_moves))
    return EngineResult(
        move=move,
        score_cp=0,
        mate=None,
        depth=1,
        pv=[move],
        limit={"time": 0.01, "nodes": 1},
    )


def _call(args, *, message_id, chat=CHAT, user=USER, platform="photon"):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.registry import registry

    tokens = set_session_vars(
        platform=platform,
        chat_id=chat,
        user_id=user,
        user_name="Live E2E",
        session_key=f"agent:main:{platform}:dm:{chat}",
        message_id=message_id,
        profile=PROFILE,
    )
    try:
        return json.loads(registry.dispatch("chess_game", args))
    finally:
        clear_session_vars(tokens)


def _slash(raw_args, message_id):
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    tokens = set_session_vars(
        platform="local",
        chat_id=SLASH_CHAT,
        user_id="local-chess-live-slash-user",
        user_name="Live Slash E2E",
        session_key=f"agent:main:local:dm:{SLASH_CHAT}",
        message_id=message_id,
        profile=PROFILE,
    )
    try:
        return manager._plugin_commands["chess"]["handler"](raw_args)
    finally:
        clear_session_vars(tokens)


@pytest.fixture(scope="session")
def live_manager():
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    return manager


@pytest.fixture
def live_service(tmp_path, monkeypatch, live_manager):
    """A ChessService bound to a throwaway DB and a fake engine.

    Setting ``hermes_chess._service`` binds the module-level singleton, so the
    slash command and pre_llm_call hook (which call ``_get_service()``) share
    the same isolated DB + fake engine as the tool dispatch.
    """
    monkeypatch.setenv("HERMES_CHESS_DB", str(tmp_path / "chess" / "chess.sqlite3"))
    monkeypatch.setenv("HERMES_CHESS_STOCKFISH", str(tmp_path / "bin" / "stockfish"))

    import hermes_chess

    hermes_chess._service = None
    svc = hermes_chess._get_service()
    svc.engine.fake = _fake_engine
    yield svc
    hermes_chess._service = None


def test_plugin_registered_through_live_manager(live_manager):
    assert "chess_game" in live_manager._plugin_tool_names
    assert "chess" in live_manager._plugin_commands
    assert "hermes-chess" in live_manager._plugins


def test_health_ready_with_fake_engine(live_service):
    assert live_service.health()["ready"] is True


def test_start_move_board_via_live_registry(live_service):
    started = _call(
        {"action": "start", "difficulty": "easy", "color": "white"},
        message_id="live-start",
    )
    assert started["success"] and started["started"] and started["difficulty"] == "Easy"

    moved = _call({"action": "move", "move": "e4"}, message_id="live-e4")
    assert moved["success"] and moved["human_move"] == "e4" and moved["engine_move"]

    board = _call({"action": "board"}, message_id="live-board")
    assert board["game_id"] == started["game_id"]
    assert "Last move:" in board["board"] and "Turn: White" in board["board"]


def test_identity_isolation_across_chats(live_service):
    _call({"action": "start", "difficulty": "easy", "color": "white"}, message_id="iso-a")
    other = _call({"action": "status"}, message_id="iso-b", chat="some-other-chat")
    assert other["success"] is False


def test_slash_command_start_and_move(live_manager, live_service):
    started = _slash("start white easy", "slash-start")
    assert "Game started" in started
    moved = _slash("e4", "slash-e4")
    assert "Stockfish played" in moved


def test_pre_llm_call_hook_injects_context(live_manager, live_service):
    from gateway.session_context import clear_session_vars, set_session_vars

    _slash("start white easy", "hook-start")
    tokens = set_session_vars(
        platform="local",
        chat_id=SLASH_CHAT,
        user_id="local-chess-live-slash-user",
        session_key=f"agent:main:local:dm:{SLASH_CHAT}",
        message_id="slash-hook",
        profile=PROFILE,
    )
    try:
        hook = live_manager.invoke_hook(
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


def test_engine_crash_recovery_exactly_once(live_service):
    from hermes_chess.engine import EngineUnavailable

    _call({"action": "start", "difficulty": "easy", "color": "white"}, message_id="crash-start")
    _call({"action": "move", "move": "e4"}, message_id="crash-e4")

    def fail_once(board, settings, analysis):
        raise EngineUnavailable("simulated interruption")

    live_service.engine.fake = fail_once
    legal = _call({"action": "legal_moves"}, message_id="crash-legal")
    interrupted = _call(
        {"action": "move", "move": legal["legal_moves"][0]},
        message_id="crash-interrupted",
    )
    assert not interrupted["success"]
    assert interrupted["human_move_saved"] is True
    assert interrupted["pending_engine_move"] is True

    live_service.engine.fake = _fake_engine
    recovered = _call({"action": "resume"}, message_id="crash-recover")
    assert recovered["success"] and not recovered["pending_engine_move"]

    repeated = _call({"action": "resume"}, message_id="crash-recover-again")
    assert repeated["success"]


@pytest.mark.skipif(
    not os.environ.get("HERMES_CHESS_STOCKFISH"),
    reason="set HERMES_CHESS_STOCKFISH=/path/to/stockfish to run a real move",
)
def test_real_stockfish_move(live_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CHESS_DB", str(tmp_path / "real.sqlite3"))

    import hermes_chess

    hermes_chess._service = None
    svc = hermes_chess._get_service()
    assert svc.engine.probe()["ready"] is True

    started = _call(
        {"action": "start", "difficulty": "easy", "color": "white"},
        message_id="real-start",
    )
    moved = _call({"action": "move", "move": "e4"}, message_id="real-e4")
    assert started["success"] and moved["success"] and moved["engine_move"]
    hermes_chess._service = None
