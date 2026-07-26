"""Configuration, identity, and difficulty mapping for the chess plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gateway.session_context import get_session_env

DIFFICULTIES: dict[str, dict[str, Any]] = {
    "beginner": {
        "label": "Beginner",
        "target_elo": 700,
        "skill": 0,
        "limit_strength": False,
        "time": 0.025,
        "nodes": 350,
    },
    "easy": {
        "label": "Easy",
        "target_elo": 1000,
        "skill": 2,
        "limit_strength": False,
        "time": 0.04,
        "nodes": 700,
    },
    "casual": {
        "label": "Casual",
        "target_elo": 1300,
        "skill": 5,
        "limit_strength": False,
        "time": 0.07,
        "nodes": 1400,
    },
    "intermediate": {
        "label": "Intermediate",
        "target_elo": 1600,
        "skill": 10,
        "limit_strength": True,
        "uci_elo": 1600,
        "time": 0.12,
        "nodes": 5000,
    },
    "advanced": {
        "label": "Advanced",
        "target_elo": 1900,
        "skill": 14,
        "limit_strength": True,
        "uci_elo": 1900,
        "time": 0.20,
        "nodes": 15000,
    },
    "expert": {
        "label": "Expert",
        "target_elo": 2200,
        "skill": 18,
        "limit_strength": True,
        "uci_elo": 2200,
        "time": 0.35,
        "nodes": 40000,
    },
    "maximum": {
        "label": "Maximum",
        "target_elo": None,
        "skill": 20,
        "limit_strength": False,
        "time": 0.60,
        "nodes": 120000,
    },
}

DIFFICULTY_ALIASES = {
    "beg": "beginner",
    "novice": "beginner",
    "normal": "casual",
    "medium": "intermediate",
    "hard": "advanced",
    "max": "maximum",
    "unlimited": "maximum",
    "unrestricted": "maximum",
}


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


@dataclass(frozen=True)
class PluginConfig:
    hermes_home: Path
    db_path: Path
    stockfish_path: Path
    default_difficulty: str
    engine_threads: int
    hash_mb: int
    engine_timeout: float
    engine_concurrency: int
    max_active_games: int
    busy_timeout_ms: int = 5000

    @classmethod
    def from_environment(cls) -> "PluginConfig":
        home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
        default = os.getenv("HERMES_CHESS_DEFAULT_DIFFICULTY", "casual").strip().lower()
        default = DIFFICULTY_ALIASES.get(default, default)
        if default not in DIFFICULTIES:
            default = "casual"
        return cls(
            hermes_home=home,
            db_path=Path(
                os.getenv("HERMES_CHESS_DB", str(home / "data" / "chess" / "chess.sqlite3"))
            ).expanduser(),
            stockfish_path=Path(
                os.getenv("HERMES_CHESS_STOCKFISH", str(home / "bin" / "stockfish"))
            ).expanduser(),
            default_difficulty=default,
            engine_threads=_bounded_int("HERMES_CHESS_ENGINE_THREADS", 1, 1, 4),
            hash_mb=_bounded_int("HERMES_CHESS_HASH_MB", 32, 8, 256),
            engine_timeout=_bounded_float("HERMES_CHESS_ENGINE_TIMEOUT", 8.0, 1.0, 30.0),
            engine_concurrency=_bounded_int("HERMES_CHESS_ENGINE_CONCURRENCY", 1, 1, 4),
            max_active_games=_bounded_int("HERMES_CHESS_MAX_ACTIVE_GAMES", 128, 1, 4096),
        )


@dataclass(frozen=True)
class Identity:
    owner_key: str
    profile: str
    platform: str
    source: str
    session_key: str
    chat_id: str
    thread_id: str
    user_id: str
    display_name: str
    message_id: str

    def with_message_id(self, message_id: str) -> "Identity":
        return Identity(
            owner_key=self.owner_key,
            profile=self.profile,
            platform=self.platform,
            source=self.source,
            session_key=self.session_key,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
            user_id=self.user_id,
            display_name=self.display_name,
            message_id=message_id,
        )


def _safe_identity_part(value: str, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if len(value) > maximum:
        value = value[:maximum]
    return value


def current_identity() -> Identity:
    profile = _safe_identity_part(get_session_env("HERMES_SESSION_PROFILE") or "default", 64)
    platform = _safe_identity_part(get_session_env("HERMES_SESSION_PLATFORM") or "local", 64).lower()
    source = _safe_identity_part(get_session_env("HERMES_SESSION_SOURCE"), 64)
    session_key = _safe_identity_part(get_session_env("HERMES_SESSION_KEY"), 512)
    chat_id = _safe_identity_part(get_session_env("HERMES_SESSION_CHAT_ID"), 512)
    thread_id = _safe_identity_part(get_session_env("HERMES_SESSION_THREAD_ID"), 512)
    user_id = _safe_identity_part(get_session_env("HERMES_SESSION_USER_ID"), 512)
    display_name = _safe_identity_part(get_session_env("HERMES_SESSION_USER_NAME"), 256)
    message_id = _safe_identity_part(get_session_env("HERMES_SESSION_MESSAGE_ID"), 512)

    # CLI sessions may not bind gateway identity ContextVars. Prefer a stable
    # local owner, but allow fallback to the profile name so the skill can start
    # from a fresh CLI session with no existing messaging identity.
    if not any((session_key, chat_id, thread_id, user_id)):
        try:
            import getpass

            user_id = getpass.getuser()
        except Exception:
            user_id = str(os.getuid())
        if not profile:
            profile = user_id or "default"

    canonical = {
        "profile": profile,
        "platform": platform,
        "session_key": session_key,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "user_id": user_id,
    }
    if not any((session_key, chat_id, thread_id, user_id, profile)):
        raise ValueError("No stable Hermes session identity is available.")
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Identity(
        owner_key=f"v1:{digest}",
        profile=profile,
        platform=platform,
        source=source,
        session_key=session_key,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        display_name=display_name,
        message_id=message_id,
    )


def parse_difficulty(
    value: Any,
    *,
    default_name: str = "casual",
    engine_elo_min: int = 1320,
    engine_elo_max: int = 3190,
) -> dict[str, Any]:
    """Return a persisted difficulty descriptor with no user-controlled options."""
    if value is None or value == "":
        raise ValueError("difficulty is required")
    if isinstance(value, bool):
        raise ValueError("difficulty must be a preset or Elo")
    if isinstance(value, int):
        requested = value
    else:
        text = str(value).strip().lower()
        if len(text) > 40:
            raise ValueError("difficulty is too long")
        if text in {"choose", "just choose", "default", "whatever", "you choose"}:
            text = default_name
        text = DIFFICULTY_ALIASES.get(text, text)
        if text in DIFFICULTIES:
            preset = dict(DIFFICULTIES[text])
            return {
                "name": text,
                "label": preset["label"],
                "requested_elo": preset["target_elo"],
                "effective_elo": preset.get("uci_elo") or preset["target_elo"],
                "clamped": False,
                "settings": preset,
                "note": (
                    "Approximate strength; time, nodes, and Skill Level are used "
                    "for presets below this Stockfish build's UCI_Elo minimum."
                    if text in {"beginner", "easy", "casual"}
                    else "Stockfish limited strength is approximate, not a human rating guarantee."
                ),
            }
        match = re.fullmatch(r"\s*(\d{2,4})\s*(?:elo)?\s*", text)
        if not match:
            raise ValueError(
                "Choose Beginner, Easy, Casual, Intermediate, Advanced, Expert, "
                "Maximum, or a numeric approximate Elo."
            )
        requested = int(match.group(1))
    if requested < 100 or requested > 5000:
        raise ValueError("Requested Elo must be between 100 and 5000.")
    effective = max(engine_elo_min, min(engine_elo_max, requested))
    settings = {
        "label": f"{requested} Elo",
        "target_elo": requested,
        "uci_elo": effective,
        "skill": 20,
        "limit_strength": True,
        "time": 0.25,
        "nodes": 25000,
    }
    clamped = effective != requested
    return {
        "name": "custom",
        "label": f"{requested} Elo",
        "requested_elo": requested,
        "effective_elo": effective,
        "clamped": clamped,
        "settings": settings,
        "note": (
            f"Requested {requested} Elo was clamped to Stockfish's supported "
            f"UCI_Elo range ({engine_elo_min}–{engine_elo_max})."
            if clamped
            else "Stockfish limited strength is approximate, not a human rating guarantee."
        ),
    }


def parse_color(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "w": "white",
        "white pieces": "white",
        "b": "black",
        "black pieces": "black",
        "random colors": "random",
        "random colour": "random",
        "random colours": "random",
        "either": "random",
    }
    text = aliases.get(text, text)
    if text not in {"white", "black", "random"}:
        raise ValueError("color must be White, Black, or random")
    return text
