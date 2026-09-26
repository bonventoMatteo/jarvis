"""
Memória do JARVIS: histórico em SQLite + preferências em JSON.

O histórico alimenta o contexto do agente (últimos N turnos) e o dashboard.
As preferências guardam coisas simples e persistentes (como o usuário quer
ser tratado, cidade padrão para o clima, apps favoritos).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from config import settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    trigger      TEXT    NOT NULL DEFAULT '',
    transcript   TEXT    NOT NULL DEFAULT '',
    route        TEXT    NOT NULL DEFAULT '',
    intent       TEXT    NOT NULL DEFAULT '',
    response     TEXT    NOT NULL DEFAULT '',
    success      INTEGER NOT NULL DEFAULT 1,
    latency_ms   INTEGER NOT NULL DEFAULT 0,
    tools        TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts DESC);

CREATE TABLE IF NOT EXISTS facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_PREFS: dict[str, Any] = {
    "user_title": settings.user_title,
    "default_city": "",
    "tts_enabled": True,
    "confirm_destructive": True,
}


@dataclass(slots=True)
class Turn:
    """Um turno de conversa registrado."""

    id: int
    ts: str
    trigger: str
    transcript: str
    route: str
    intent: str
    response: str
    success: bool
    latency_ms: int
    tools: list[str]


class MemoryStore:
    """Persistência do histórico e das preferências."""

    def __init__(self, db_path: Path | None = None, prefs_path: Path | None = None) -> None:
        self.db_path = db_path or settings.db_path
        self.prefs_path = prefs_path or settings.prefs_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._prefs: dict[str, Any] = dict(DEFAULT_PREFS)

    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        """Abre o banco, cria o schema e carrega as preferências."""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._load_prefs()
        log.info("memory.connected", db=str(self.db_path))

    def close(self) -> None:
        """Fecha a conexão."""
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # Histórico
    # ------------------------------------------------------------------ #
    def add_turn(
        self,
        transcript: str,
        response: str,
        *,
        trigger: str = "",
        route: str = "",
        intent: str = "",
        success: bool = True,
        latency_ms: int = 0,
        tools: list[str] | None = None,
    ) -> int:
        """Registra um turno e devolve o id."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO turns (ts, trigger, transcript, route, intent, response, success, latency_ms, tools)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    trigger,
                    transcript,
                    route,
                    intent,
                    response,
                    int(success),
                    int(latency_ms),
                    json.dumps(tools or [], ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    async def add_turn_async(self, *args: Any, **kwargs: Any) -> int:
        """Versão assíncrona de `add_turn` (não bloqueia o loop)."""
        return await asyncio.to_thread(self.add_turn, *args, **kwargs)

    def recent_turns(self, limit: int | None = None) -> list[Turn]:
        """Últimos turnos, do mais antigo para o mais recente."""
        self.connect()
        assert self._conn is not None
        count = limit if limit is not None else settings.history_context_turns
        if count <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turns ORDER BY id DESC LIMIT ?", (count,)
            ).fetchall()
        turns = [
            Turn(
                id=row["id"],
                ts=row["ts"],
                trigger=row["trigger"],
                transcript=row["transcript"],
                route=row["route"],
                intent=row["intent"],
                response=row["response"],
                success=bool(row["success"]),
                latency_ms=row["latency_ms"],
                tools=json.loads(row["tools"] or "[]"),
            )
            for row in rows
        ]
        return list(reversed(turns))

    def conversation_context(self, limit: int | None = None) -> list[dict[str, str]]:
        """Histórico no formato de mensagens da API Anthropic."""
        messages: list[dict[str, str]] = []
        for turn in self.recent_turns(limit):
            if turn.transcript:
                messages.append({"role": "user", "content": turn.transcript})
            if turn.response:
                messages.append({"role": "assistant", "content": turn.response})
        return messages

    def stats(self) -> dict[str, Any]:
        """Contagens agregadas para o dashboard."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok, "
                "AVG(latency_ms) AS avg_latency FROM turns"
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "ok": row["ok"] or 0,
            "avg_latency_ms": round(row["avg_latency"] or 0),
        }

    def clear_history(self) -> int:
        """Apaga todo o histórico. Devolve quantos turnos foram removidos."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            self._conn.execute("DELETE FROM turns")
            self._conn.commit()
        return int(count)

    # ------------------------------------------------------------------ #
    # Fatos (memória de longo prazo chave/valor)
    # ------------------------------------------------------------------ #
    def remember(self, key: str, value: str) -> None:
        """Grava um fato durável."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def recall(self, key: str, default: str = "") -> str:
        """Lê um fato gravado."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            row = self._conn.execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def all_facts(self) -> dict[str, str]:
        """Todos os fatos gravados."""
        self.connect()
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM facts").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ------------------------------------------------------------------ #
    # Preferências (JSON)
    # ------------------------------------------------------------------ #
    def _load_prefs(self) -> None:
        if not self.prefs_path.exists():
            self._save_prefs()
            return
        try:
            loaded = json.loads(self.prefs_path.read_text(encoding="utf-8"))
            self._prefs = {**DEFAULT_PREFS, **loaded}
        except Exception as exc:
            log.warning("memory.prefs_load_failed", error=str(exc))
            self._prefs = dict(DEFAULT_PREFS)

    def _save_prefs(self) -> None:
        try:
            self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self.prefs_path.write_text(
                json.dumps(self._prefs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("memory.prefs_save_failed", error=str(exc))

    def get_pref(self, key: str, default: Any = None) -> Any:
        """Lê uma preferência."""
        return self._prefs.get(key, default)

    def set_pref(self, key: str, value: Any) -> None:
        """Grava uma preferência e persiste no disco."""
        self._prefs[key] = value
        self._save_prefs()

    @property
    def prefs(self) -> dict[str, Any]:
        return dict(self._prefs)


__all__ = ["DEFAULT_PREFS", "MemoryStore", "Turn"]
