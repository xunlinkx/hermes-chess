"""Bounded short-lived local Stockfish operations through python-chess."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import chess
import chess.engine

from .config import PluginConfig


class EngineUnavailable(RuntimeError):
    pass


@dataclass
class EngineResult:
    move: chess.Move
    score_cp: int | None
    mate: int | None
    depth: int | None
    pv: list[chess.Move]
    limit: dict[str, Any]


class StockfishRunner:
    def __init__(
        self,
        config: PluginConfig,
        fake: Callable[[chess.Board, dict[str, Any], bool], EngineResult] | None = None,
    ) -> None:
        self.config = config
        self.fake = fake
        self._semaphore = threading.BoundedSemaphore(config.engine_concurrency)
        self._probe_lock = threading.Lock()
        self._probe_cache: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self.config.stockfish_path

    def probe(self, *, refresh: bool = False) -> dict[str, Any]:
        if self.fake is not None:
            return {
                "ready": True,
                "name": "test-engine",
                "path": "<injected>",
                "uci_elo_min": 1320,
                "uci_elo_max": 3190,
                "options": ["Skill Level", "UCI_Elo", "UCI_LimitStrength"],
            }
        with self._probe_lock:
            if self._probe_cache is not None and not refresh:
                return dict(self._probe_cache)
            path = self.path
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                self._probe_cache = {
                    "ready": False,
                    "error": "Stockfish executable is missing or not executable.",
                    "path": str(path),
                }
                return dict(self._probe_cache)
            engine = None
            try:
                engine = chess.engine.SimpleEngine.popen_uci(
                    [str(path)], timeout=self.config.engine_timeout
                )
                options = engine.options
                elo = options.get("UCI_Elo")
                self._probe_cache = {
                    "ready": True,
                    "name": engine.id.get("name", "Stockfish"),
                    "author": engine.id.get("author", ""),
                    "path": str(path),
                    "uci_elo_min": int(getattr(elo, "min", 1320) or 1320),
                    "uci_elo_max": int(getattr(elo, "max", 3190) or 3190),
                    "options": sorted(options),
                }
            except Exception:
                self._probe_cache = {
                    "ready": False,
                    "error": "Stockfish failed its local UCI startup check.",
                    "path": str(path),
                }
            finally:
                self._cleanup(engine)
            return dict(self._probe_cache)

    @staticmethod
    def _cleanup(engine: chess.engine.SimpleEngine | None) -> None:
        if engine is None:
            return
        try:
            engine.quit()
        except Exception:
            try:
                engine.close()
            except Exception:
                pass

    @staticmethod
    def _score(info: dict[str, Any], board: chess.Board) -> tuple[int | None, int | None]:
        score = info.get("score")
        if score is None:
            return None, None
        pov = score.pov(board.turn)
        if pov.is_mate():
            return None, pov.mate()
        return pov.score(mate_score=100000), None

    def _options(self, engine: chess.engine.SimpleEngine, settings: dict[str, Any]) -> dict[str, Any]:
        supported = engine.options
        options: dict[str, Any] = {}
        if "Threads" in supported:
            options["Threads"] = self.config.engine_threads
        if "Hash" in supported:
            options["Hash"] = self.config.hash_mb
        limited = bool(settings.get("limit_strength"))
        if limited and "UCI_LimitStrength" in supported and "UCI_Elo" in supported:
            elo_opt = supported["UCI_Elo"]
            low = int(getattr(elo_opt, "min", 1320) or 1320)
            high = int(getattr(elo_opt, "max", 3190) or 3190)
            options["UCI_LimitStrength"] = True
            options["UCI_Elo"] = max(low, min(high, int(settings.get("uci_elo", low))))
        else:
            if "UCI_LimitStrength" in supported:
                options["UCI_LimitStrength"] = False
            if "Skill Level" in supported:
                options["Skill Level"] = max(0, min(20, int(settings.get("skill", 20))))
        return options

    @staticmethod
    def _limit(settings: dict[str, Any], *, analysis: bool) -> tuple[chess.engine.Limit, dict[str, Any]]:
        if analysis:
            time_limit = max(0.10, min(1.0, float(settings.get("analysis_time", 0.45))))
            nodes = max(1000, min(150000, int(settings.get("analysis_nodes", 60000))))
        else:
            time_limit = max(0.01, min(1.5, float(settings.get("time", 0.15))))
            nodes = max(50, min(250000, int(settings.get("nodes", 10000))))
        data = {"time": time_limit, "nodes": nodes}
        return chess.engine.Limit(time=time_limit, nodes=nodes), data

    def calculate(
        self,
        board: chess.Board,
        settings: dict[str, Any],
        *,
        analysis: bool = False,
    ) -> EngineResult:
        if self.fake is not None:
            return self.fake(board.copy(stack=True), dict(settings), analysis)
        if not self._semaphore.acquire(timeout=self.config.engine_timeout):
            raise EngineUnavailable("engine concurrency limit is busy")
        engine = None
        # Hard wall-clock deadline: if the engine call itself exceeds
        # 2x the configured timeout, we force-kill the subprocess.
        # This guards against pathological positions where the UCI
        # Limit is ignored by the engine.
        hard_deadline = self.config.engine_timeout * 2
        try:
            health = self.probe()
            if not health.get("ready"):
                raise EngineUnavailable("Stockfish is unavailable")
            engine = chess.engine.SimpleEngine.popen_uci(
                [str(self.path)], timeout=self.config.engine_timeout
            )
            engine.timeout = self.config.engine_timeout
            options = self._options(engine, settings)
            limit, limit_data = self._limit(settings, analysis=analysis)
            # Set a hard wall-clock watchdog that kills the engine
            # process if the call exceeds the deadline.
            _watchdog = threading.Timer(hard_deadline, lambda: self._cleanup(engine))
            _watchdog.daemon = True
            _watchdog.start()
            try:
                if analysis:
                    info = engine.analyse(
                        board,
                        limit,
                        multipv=1,
                        options=options,
                        info=chess.engine.INFO_ALL,
                    )
                    if isinstance(info, list):
                        info = info[0]
                    pv = list(info.get("pv") or [])
                    if not pv:
                        raise EngineUnavailable("Stockfish returned no principal variation")
                    move = pv[0]
                else:
                    result = engine.play(
                        board,
                        limit,
                        options=options,
                        info=chess.engine.INFO_ALL,
                    )
                    move = result.move
                    info = result.info or {}
                    pv = list(info.get("pv") or [move])
            finally:
                _watchdog.cancel()
            if move not in board.legal_moves:
                raise EngineUnavailable("Stockfish returned an illegal move")
            cp, mate = self._score(info, board)
            depth = info.get("depth")
            return EngineResult(
                move=move,
                score_cp=cp,
                mate=mate,
                depth=int(depth) if isinstance(depth, int) else None,
                pv=pv,
                limit=limit_data,
            )
        except EngineUnavailable:
            raise
        except (chess.engine.EngineError, chess.engine.EngineTerminatedError, TimeoutError):
            raise EngineUnavailable("Stockfish calculation failed or timed out") from None
        except Exception:
            raise EngineUnavailable("Stockfish calculation failed") from None
        finally:
            self._cleanup(engine)
            self._semaphore.release()
