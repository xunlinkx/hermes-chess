from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import chess
import pytest

from hermes_chess.config import Identity, PluginConfig, parse_difficulty
from hermes_chess.engine import EngineResult, EngineUnavailable
from hermes_chess.service import ChessService, _outcome_fields, _parse_move


def ident(
    owner: str = "owner-a",
    *,
    platform: str = "photon",
    chat: str = "dm-1",
    thread: str = "",
    user: str = "user-1",
    message: str = "",
) -> Identity:
    return Identity(
        owner_key=owner,
        profile="default",
        platform=platform,
        source="",
        session_key=f"agent:main:{platform}:dm:{chat}",
        chat_id=chat,
        thread_id=thread,
        user_id=user,
        display_name="Test Player",
        message_id=message,
    )


def fake_engine(board: chess.Board, settings: dict, analysis: bool) -> EngineResult:
    move = next(iter(board.legal_moves))
    return EngineResult(
        move=move,
        score_cp=15,
        mate=None,
        depth=8,
        pv=[move],
        limit={"time": settings.get("time", 0.1), "nodes": settings.get("nodes", 1000)},
    )


@pytest.fixture
def config(tmp_path: Path) -> PluginConfig:
    return PluginConfig(
        hermes_home=tmp_path,
        db_path=tmp_path / "data" / "chess" / "chess.sqlite3",
        stockfish_path=tmp_path / "bin" / "stockfish",
        default_difficulty="casual",
        engine_threads=1,
        hash_mb=16,
        engine_timeout=2.0,
        engine_concurrency=1,
        max_active_games=32,
    )


@pytest.fixture
def service(config: PluginConfig) -> ChessService:
    return ChessService(config, engine_runner=fake_engine)


def start(service: ChessService, identity: Identity | None = None, **kwargs):
    identity = identity or ident()
    args = {"action": "start", "difficulty": "easy", "color": "white", **kwargs}
    return service.dispatch(identity, args)


def rows(service: ChessService, sql: str, params=()):
    conn = service.db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestSetup:
    def test_start_without_choices(self, service):
        result = service.dispatch(ident(), {"action": "start"})
        assert result["success"] and set(result["missing_choices"]) == {"difficulty", "color"}

    def test_missing_difficulty_only(self, service):
        result = service.dispatch(ident(), {"action": "start", "color": "white"})
        assert result["missing_choices"] == ["difficulty"]

    def test_missing_color_only(self, service):
        result = service.dispatch(ident(), {"action": "start", "difficulty": "easy"})
        assert result["missing_choices"] == ["color"]

    @pytest.mark.parametrize("name", [
        "beginner", "easy", "casual", "intermediate", "advanced", "expert", "maximum"
    ])
    def test_named_difficulty(self, config, name):
        svc = ChessService(replace(config, db_path=config.db_path.with_name(name + ".sqlite3")),
                           engine_runner=fake_engine)
        result = svc.dispatch(ident(name), {
            "action": "start", "difficulty": name, "color": "white"
        })
        assert result["started"]

    def test_numeric_elo_and_clamping(self, service):
        result = service.dispatch(ident(), {
            "action": "start", "difficulty": 800, "color": "white"
        })
        assert result["effective_target_elo"] == 1320
        assert result["difficulty_clamped"]

    def test_random_color(self, service):
        result = service.dispatch(ident(), {
            "action": "start", "difficulty": "easy", "color": "random"
        })
        assert result["human_color"] in {"white", "black"}

    def test_default_casual_selection(self, service):
        result = service.dispatch(ident(), {
            "action": "start", "difficulty": "just choose", "color": "white"
        })
        assert result["difficulty"] == "Casual"

    def test_change_difficulty_before_first_move(self, service):
        start(service)
        result = service.dispatch(ident(message="diff-1"), {
            "action": "set_difficulty", "difficulty": "intermediate"
        })
        assert result["success"] and result["difficulty"] == "Intermediate"

    def test_change_difficulty_after_play_requires_confirmation(self, service):
        start(service)
        service.dispatch(ident(message="move-1"), {"action": "move", "move": "e4"})
        denied = service.dispatch(ident(message="diff-2"), {
            "action": "set_difficulty", "difficulty": "advanced"
        })
        assert denied["confirmation_required"]
        changed = service.dispatch(ident(message="diff-3"), {
            "action": "set_difficulty", "difficulty": "advanced", "confirm": True
        })
        assert changed["success"]

    def test_rematch_same_and_swap(self, service):
        first = start(service)
        service.dispatch(ident(), {"action": "resign"})
        same = service.dispatch(ident(message="r1"), {
            "action": "rematch", "rematch_mode": "same"
        })
        assert same["human_color"] == first["human_color"]
        service.dispatch(ident(), {"action": "resign"})
        swap = service.dispatch(ident(message="r2"), {
            "action": "rematch", "rematch_mode": "swap"
        })
        assert swap["human_color"] != same["human_color"]

    def test_human_black_gets_engine_opening(self, service):
        result = service.dispatch(ident(), {
            "action": "start", "difficulty": "easy", "color": "black"
        })
        assert result["started"] and result["last_move"] and result["human_to_move"]


class TestRules:
    def test_san_and_uci(self):
        board = chess.Board()
        assert _parse_move(board, "e4") == chess.Move.from_uci("e2e4")
        assert _parse_move(board, "e2e4") == chess.Move.from_uci("e2e4")

    def test_illegal_and_ambiguous(self):
        with pytest.raises(ValueError, match="not legal"):
            _parse_move(chess.Board(), "e5")
        board = chess.Board("4k3/8/8/8/8/2N1N3/8/4K3 w - - 0 1")
        with pytest.raises(ValueError, match="ambiguous"):
            _parse_move(board, "Nd5")

    def test_castling(self):
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        assert _parse_move(board, "castle kingside") == chess.Move.from_uci("e1g1")
        assert _parse_move(board, "O-O-O") == chess.Move.from_uci("e1c1")

    def test_en_passant(self):
        board = chess.Board()
        for san in ["e4", "a6", "e5", "d5"]:
            board.push_san(san)
        assert _parse_move(board, "exd6") == chess.Move.from_uci("e5d6")

    def test_promotion(self):
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        assert _parse_move(board, "a8=Q") == chess.Move.from_uci("a7a8q")
        assert _parse_move(board, "promote to queen") == chess.Move.from_uci("a7a8q")

    @pytest.mark.parametrize(
        "fen,status,reason",
        [
            ("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1", "checkmate", "checkmate"),
            ("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", "stalemate", "stalemate"),
            ("7k/8/8/8/8/8/8/K7 w - - 0 1", "draw", "insufficient material"),
        ],
    )
    def test_termination_positions(self, fen, status, reason):
        result = _outcome_fields(chess.Board(fen))
        assert result["game_status"] == status
        assert reason in result["termination_reason"]

    def test_check(self):
        board = chess.Board("7k/6Q1/8/8/8/8/8/K7 b - - 0 1")
        assert board.is_check()

    def test_repetition(self):
        board = chess.Board()
        for _ in range(2):
            for move in ["Nf3", "Nf6", "Ng1", "Ng8"]:
                board.push_san(move)
        assert board.can_claim_threefold_repetition()
        assert _outcome_fields(board)["completed"]

    def test_fifty_move(self):
        board = chess.Board("7k/8/8/8/8/8/R7/K7 w - - 100 80")
        assert board.can_claim_fifty_moves()
        assert _outcome_fields(board)["completed"]


class TestEngineAndPersistence:
    def test_engine_move_legal_and_persisted(self, service):
        start(service)
        result = service.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        assert result["success"] and result["engine_move"]
        saved = rows(service, "SELECT * FROM moves WHERE game_id=? AND undone=0", (result["game_id"],))
        assert [row["actor"] for row in saved] == ["human", "engine"]
        board = chess.Board()
        for row in saved:
            move = chess.Move.from_uci(row["uci"])
            assert move in board.legal_moves
            board.push(move)

    def test_saved_difficulty_reused_after_reload(self, config):
        seen = []

        def recorder(board, settings, analysis):
            seen.append(dict(settings))
            return fake_engine(board, settings, analysis)

        svc1 = ChessService(config, engine_runner=recorder)
        start(svc1)
        svc1.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        svc2 = ChessService(config, engine_runner=recorder)
        status = svc2.dispatch(ident(message="s1"), {"action": "status"})
        assert status["difficulty"] == "Easy"
        assert seen and seen[0]["label"] == "Easy"

    @pytest.mark.parametrize("mode", ["crash", "timeout"])
    def test_engine_failure_preserves_pending(self, config, mode):
        def broken(board, settings, analysis):
            raise EngineUnavailable(mode)

        svc = ChessService(config, engine_runner=broken)
        start(svc)
        result = svc.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        assert not result["success"] and result["human_move_saved"]
        game = dict(svc.db.active_game(ident().owner_key))
        assert game["pending_engine"] == 1
        assert len(rows(svc, "SELECT * FROM moves WHERE game_id=?", (game["id"],))) == 1

    def test_missing_engine_executable(self, config):
        svc = ChessService(config)
        assert not svc.engine.probe()["ready"]

    def test_game_survives_reload_and_changed_conversation_id(self, config):
        svc1 = ChessService(config, engine_runner=fake_engine)
        first = start(svc1, ident(message="start"))
        svc1.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        # Temporary Hermes session/conversation id is intentionally absent from Identity.
        svc2 = ChessService(config, engine_runner=fake_engine)
        resumed = svc2.dispatch(ident(message="resume-new-session"), {"action": "resume"})
        assert resumed["game_id"] == first["game_id"] and resumed["difficulty"] == "Easy"

    def test_interrupted_turn_recovery_exactly_once(self, config):
        def broken(board, settings, analysis):
            raise EngineUnavailable("crash")

        svc1 = ChessService(config, engine_runner=broken)
        start(svc1)
        failed = svc1.dispatch(ident(message="delivery-1"), {"action": "move", "move": "e4"})
        assert failed["human_move_saved"]
        calls = 0
        call_lock = threading.Lock()

        def counted(board, settings, analysis):
            nonlocal calls
            with call_lock:
                calls += 1
            return fake_engine(board, settings, analysis)

        svc2 = ChessService(config, engine_runner=counted)
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    svc2.dispatch(ident(message=f"resume-{i}"), {"action": "resume"})
                )
            )
            for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        moves = rows(svc2, "SELECT actor FROM moves WHERE undone=0 ORDER BY id")
        assert calls == 1
        assert [row["actor"] for row in moves] == ["human", "engine"]

    def test_duplicate_message_id_does_not_move_twice(self, service):
        start(service)
        identity = ident(message="same-delivery")
        first = service.dispatch(identity, {"action": "move", "move": "e4"})
        second = service.dispatch(identity, {"action": "move", "move": "e4"})
        assert first["success"] and second["duplicate"]
        assert len(rows(service, "SELECT * FROM moves WHERE undone=0")) == 2

    def test_undo_complete_turn_and_pending_human(self, config):
        svc = ChessService(config, engine_runner=fake_engine)
        start(svc)
        svc.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        undone = svc.dispatch(ident(message="u1"), {"action": "undo"})
        assert len(undone["undone_moves"]) == 2
        assert len(rows(svc, "SELECT * FROM moves WHERE undone=0")) == 0

        def broken(board, settings, analysis):
            raise EngineUnavailable("crash")

        svc.engine.fake = broken
        svc.dispatch(ident(message="m2"), {"action": "move", "move": "d4"})
        pending_undo = svc.dispatch(ident(message="u2"), {"action": "undo"})
        assert pending_undo["undone_moves"] == ["d4"]

    def test_completed_history_and_pgn(self, service):
        start(service)
        played = service.dispatch(ident(message="m1"), {"action": "move", "move": "e4"})
        pgn = service.dispatch(ident(), {"action": "pgn"})
        assert "1. e4" in pgn["pgn"] and played["engine_move"] in pgn["pgn"]
        service.dispatch(ident(message="resign"), {"action": "resign"})
        history = service.dispatch(ident(), {"action": "list_games"})
        assert history["count"] == 1 and history["games"][0]["result"] == "0-1"

    def test_bounded_concurrency_setting(self, config):
        assert config.engine_concurrency == 1


class TestPvp:
    def _players(self, chat: str = "chan-pvp"):
        white = ident("owner-w", platform="discord", user="user-w", chat=chat)
        black = ident("owner-b", platform="discord", user="user-b", chat=chat)
        return white, black

    def test_pvp_start_has_no_engine_reply(self, service):
        white, _ = self._players()
        started = service.dispatch(white, {"action": "start", "difficulty": "pvp", "color": "white"})
        assert started["started"] and started["human_color"] == "white"
        assert started["engine_color"] is None and started["last_move"] is None
        game = dict(service.db.game(started["game_id"]))
        assert game["mode"] == "pvp" and game["pending_engine"] == 0

    def test_pvp_channel_sharing(self, service):
        white, black = self._players()
        started = service.dispatch(white, {"action": "start", "difficulty": "pvp", "color": "white"})
        status = service.dispatch(black, {"action": "status"})
        assert status["success"] and status["game_id"] == started["game_id"]
        outsider = ident("owner-c", platform="discord", user="user-c", chat="other-chan")
        assert service.dispatch(outsider, {"action": "status"})["success"] is False

    def test_pvp_side_claim_and_turn_enforcement(self, service):
        white, black = self._players()
        service.dispatch(white, {"action": "start", "difficulty": "pvp", "color": "white"})
        service.dispatch(white, {"action": "move", "move": "e4"})
        game = dict(service.db.game(service.dispatch(white, {"action": "status"})["game_id"]))
        assert game["white_user_id"] == white.user_id and game["black_user_id"] is None
        moved = service.dispatch(black, {"action": "move", "move": "e5"})
        assert moved["success"]
        game = dict(service.db.game(moved["game_id"]))
        assert game["black_user_id"] == black.user_id
        wrong = service.dispatch(black, {"action": "move", "move": "Nf3"})
        assert not wrong["success"] and "not your turn" in wrong["error"].lower()
        third = ident("owner-d", platform="discord", user="user-d", chat="chan-pvp")
        denied = service.dispatch(third, {"action": "move", "move": "Nf3"})
        assert not denied["success"] and "not your turn" in denied["error"].lower()


class TestIdentityAndSecurity:
    @pytest.mark.parametrize(
        "left,right",
        [
            (ident("imsg-a", user="a", chat="dm-a"), ident("imsg-b", user="b", chat="dm-b")),
            (ident("disc-a", platform="discord", user="a", chat="chan-1"), ident("disc-b", platform="discord", user="b", chat="chan-1")),
            (ident("thread-a", platform="discord", thread="one"), ident("thread-b", platform="discord", thread="two")),
            (ident("imsg", platform="photon", chat="c1"), ident("disc", platform="discord", chat="c2")),
        ],
    )
    def test_identity_isolation(self, config, left, right):
        svc = ChessService(config, engine_runner=fake_engine)
        start(svc, left)
        assert svc.dispatch(right, {"action": "status"})["success"] is False

    def test_new_does_not_lose_game(self, service):
        first = start(service)
        # /new rotates Hermes session_id, which is not part of owner identity.
        resumed = service.dispatch(ident(message="after-new"), {"action": "resume"})
        assert resumed["game_id"] == first["game_id"]

    @pytest.mark.parametrize(
        "args",
        [
            {"action": "move", "move": "x" * 65},
            {"action": "move", "move": {"bad": "type"}},
            {"action": "move", "move": "e4", "engine_options": {"Threads": 99}},
            {"action": "start", "difficulty": "x" * 41, "color": "white"},
            {"action": "move", "move": "e4", "fen": chess.STARTING_FEN},
        ],
    )
    def test_malformed_or_forbidden_arguments(self, service, args):
        result = service.dispatch(ident(), args)
        assert not result["success"]

    def test_sql_injection_like_move(self, service):
        start(service)
        result = service.dispatch(ident(), {
            "action": "move", "move": "e4'); DROP TABLE games; --"
        })
        assert not result["success"]
        assert rows(service, "SELECT name FROM sqlite_master WHERE name='games'")

    def test_secret_safe_engine_error(self, config):
        def leaking(board, settings, analysis):
            raise EngineUnavailable("API_KEY=super-secret")

        svc = ChessService(config, engine_runner=leaking)
        start(svc)
        result = svc.dispatch(ident(), {"action": "move", "move": "e4"})
        assert "super-secret" not in json.dumps(result)

    def test_database_migration_initialization_and_permissions(self, service):
        integrity = service.db.integrity()
        assert integrity == {
            "integrity": "ok", "foreign_key_errors": 0, "schema_version": 3
        }
        assert os.stat(service.db.path).st_mode & 0o077 == 0

    def test_difficulty_mapping(self):
        easy = parse_difficulty("easy")
        intermediate = parse_difficulty("intermediate")
        maximum = parse_difficulty("maximum")
        assert easy["settings"]["skill"] < intermediate["settings"]["skill"]
        assert maximum["settings"]["limit_strength"] is False


def test_real_stockfish_legal_move(tmp_path):
    path = Path(os.environ.get("HERMES_CHESS_STOCKFISH", ""))
    if not path.is_file():
        pytest.skip("real Stockfish path not supplied")
    cfg = PluginConfig(
        hermes_home=tmp_path,
        db_path=tmp_path / "real.sqlite3",
        stockfish_path=path,
        default_difficulty="casual",
        engine_threads=1,
        hash_mb=16,
        engine_timeout=8,
        engine_concurrency=1,
        max_active_games=4,
    )
    svc = ChessService(cfg)
    result = start(svc)
    moved = svc.dispatch(ident(message="real-e4"), {"action": "move", "move": "e4"})
    assert result["started"] and moved["success"] and moved["engine_move"]
