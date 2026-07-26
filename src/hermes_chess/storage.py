"""Owner-only SQLite storage with explicit schema versioning."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChessDatabase:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(250, min(30000, int(busy_timeout_ms)))
        self._prepare_path()
        self.initialize()

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return conn

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 0
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Chess database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                self._migrate_v1(conn)
            if version < 2:
                self._migrate_v2(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate_v1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                state TEXT NOT NULL CHECK(state IN ('setup','active','completed','archived')),
                profile TEXT NOT NULL,
                platform TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                session_key TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                human_color TEXT CHECK(human_color IN ('white','black') OR human_color IS NULL),
                engine_color TEXT CHECK(engine_color IN ('white','black') OR engine_color IS NULL),
                requested_color TEXT CHECK(requested_color IN ('white','black','random') OR requested_color IS NULL),
                difficulty_name TEXT,
                requested_elo INTEGER,
                effective_target_elo INTEGER,
                engine_settings_json TEXT,
                current_fen TEXT NOT NULL,
                starting_fen TEXT NOT NULL,
                turn TEXT NOT NULL CHECK(turn IN ('white','black')),
                game_status TEXT NOT NULL DEFAULT 'setup',
                result TEXT,
                termination_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                last_processed_message_id TEXT,
                pending_engine INTEGER NOT NULL DEFAULT 0 CHECK(pending_engine IN (0,1)),
                engine_claim_token TEXT,
                engine_claimed_at TEXT,
                pgn_event TEXT NOT NULL DEFAULT 'Hermes Local Chess',
                pgn_site TEXT NOT NULL DEFAULT 'Luna',
                pgn_date TEXT NOT NULL,
                pgn_round TEXT NOT NULL DEFAULT '-',
                pgn_white TEXT,
                pgn_black TEXT,
                hint_count INTEGER NOT NULL DEFAULT 0,
                draw_offer_by TEXT CHECK(draw_offer_by IN ('human','engine') OR draw_offer_by IS NULL)
            );

            CREATE UNIQUE INDEX one_live_game_per_owner
                ON games(owner_key) WHERE active=1;
            CREATE INDEX games_owner_updated ON games(owner_key, updated_at DESC);
            CREATE INDEX games_state_pending ON games(state, pending_engine);

            CREATE TABLE moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE RESTRICT,
                ply_number INTEGER NOT NULL,
                actor TEXT NOT NULL CHECK(actor IN ('human','engine')),
                san TEXT NOT NULL,
                uci TEXT NOT NULL,
                fen_before TEXT NOT NULL,
                fen_after TEXT NOT NULL,
                created_at TEXT NOT NULL,
                triggering_message_id TEXT,
                engine_evaluation_cp INTEGER,
                engine_mate INTEGER,
                engine_depth INTEGER,
                engine_limit_json TEXT,
                undone INTEGER NOT NULL DEFAULT 0 CHECK(undone IN (0,1)),
                undone_at TEXT
            );
            CREATE INDEX moves_game_active ON moves(game_id, undone, id);
            CREATE UNIQUE INDEX moves_human_message_once
                ON moves(game_id, triggering_message_id, actor)
                WHERE triggering_message_id IS NOT NULL AND actor='human' AND undone=0;

            CREATE TABLE processed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                action TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(owner_key, message_id, action)
            );

            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER REFERENCES games(id) ON DELETE RESTRICT,
                owner_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                message_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX events_game_time ON events(game_id, id);

            INSERT OR REPLACE INTO schema_meta(key,value)
                VALUES('schema_version','1');
            COMMIT;
            """
        )

    @staticmethod
    def _migrate_v2(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE games ADD COLUMN timer_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(timer_enabled IN (0,1));
            INSERT OR REPLACE INTO schema_meta(key,value)
                VALUES('schema_version','2');
            COMMIT;
            """
        )

    def active_game(self, owner_key: str, conn: sqlite3.Connection | None = None):
        owned = conn is None
        conn = conn or self.connect()
        try:
            return conn.execute(
                "SELECT * FROM games WHERE owner_key=? AND active=1 ORDER BY id DESC LIMIT 1",
                (owner_key,),
            ).fetchone()
        finally:
            if owned:
                conn.close()

    def owned_game(
        self, owner_key: str, game_id: int, conn: sqlite3.Connection | None = None
    ):
        owned = conn is None
        conn = conn or self.connect()
        try:
            return conn.execute(
                "SELECT * FROM games WHERE owner_key=? AND id=?",
                (owner_key, game_id),
            ).fetchone()
        finally:
            if owned:
                conn.close()

    def event(
        self,
        conn: sqlite3.Connection,
        *,
        owner_key: str,
        event_type: str,
        game_id: int | None,
        details: dict[str, Any] | None = None,
        message_id: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT INTO events(game_id,owner_key,event_type,details_json,message_id,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                game_id,
                owner_key,
                event_type[:64],
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                message_id or None,
                utc_now(),
            ),
        )

    def integrity(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            return {
                "integrity": integrity,
                "foreign_key_errors": len(foreign_keys),
                "schema_version": int(version[0]) if version else 0,
            }
        finally:
            conn.close()
