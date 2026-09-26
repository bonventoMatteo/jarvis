"""
Agendador do JARVIS: timers, alarmes, lembretes e tarefas agendadas.

Tudo roda num único loop asyncio que acorda a cada 0,5 s. As entradas são
persistidas em `data/schedule.json`, então alarmes e lembretes sobrevivem a
um reinício do assistente (timers também — se o horário já passou durante o
desligamento, disparam assim que o JARVIS volta).

Tipos de entrada:
  * ``timer``    — "timer de 5 minutos"
  * ``alarm``    — "alarme às 7 e meia"
  * ``reminder`` — "me lembre de ligar pro João em 20 minutos"
  * ``command``  — tarefa agendada que executa um comando falado no horário
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog

from config import DATA_DIR

log = structlog.get_logger(__name__)

Kind = Literal["timer", "alarm", "reminder", "command"]

_SCHEDULE_FILE = DATA_DIR / "schedule.json"

_NUMBER_WORDS: dict[str, int] = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "três": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
    "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "vinte": 20, "vinte e cinco": 25, "trinta": 30,
    "quarenta": 40, "quarenta e cinco": 45, "cinquenta": 50, "sessenta": 60,
    "noventa": 90, "meia": 30,
}


def parse_number(token: str) -> float | None:
    """Converte '5', '2,5', 'cinco', 'meia' em número."""
    token = token.strip().lower()
    if not token:
        return None
    try:
        return float(token.replace(",", "."))
    except ValueError:
        pass
    return float(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else None


_UNIT_SECONDS: dict[str, int] = {
    "s": 1, "seg": 1, "segundo": 1, "segundos": 1,
    "min": 60, "minuto": 60, "minutos": 60,
    "h": 3600, "hora": 3600, "horas": 3600,
}


def duration_seconds(amount: str, unit: str) -> float | None:
    """'5' + 'minutos' -> 300.0"""
    value = parse_number(amount)
    factor = _UNIT_SECONDS.get(unit.strip().lower())
    if value is None or factor is None:
        return None
    return value * factor


_CLOCK_RE = re.compile(
    r"(?P<h>\d{1,2}|[a-zç]+?)(?:\s*(?:h|:|horas?)\s*(?P<m>\d{1,2})?|\s+e\s+(?P<mw>meia|\d{1,2}|[a-z]+))?"
    r"(?:\s*(?:da|de)\s+(?P<period>manha|manhã|tarde|noite|madrugada))?$"
)


def parse_clock(text: str, now: datetime | None = None) -> datetime | None:
    """
    Converte uma hora falada no próximo instante correspondente.

    Aceita '7:30', '7h30', '19 horas', '7 e meia', 'sete e quinze da noite'.
    Se o horário já passou hoje, devolve o de amanhã.
    """
    now = now or datetime.now()
    raw = text.strip().lower().replace("às", "").replace("as ", " ").strip()
    match = _CLOCK_RE.match(raw)
    if not match:
        return None
    hour = parse_number(match.group("h"))
    if hour is None:
        return None
    minute_token = match.group("m") or match.group("mw") or "0"
    minute = parse_number(minute_token)
    if minute is None:
        return None
    hour_i, minute_i = int(hour), int(minute)
    period = (match.group("period") or "").replace("ã", "a")
    if period in {"tarde", "noite"} and hour_i < 12:
        hour_i += 12
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return None
    target = now.replace(hour=hour_i, minute=minute_i, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


@dataclass(slots=True)
class ScheduledItem:
    """Uma entrada do agendador."""

    id: str
    kind: Kind
    due_iso: str
    text: str
    created_iso: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def due(self) -> datetime:
        return datetime.fromisoformat(self.due_iso)

    @property
    def remaining_s(self) -> float:
        return max(0.0, (self.due - datetime.now()).total_seconds())

    def describe(self) -> str:
        """Descrição curta em pt-BR para falar/mostrar."""
        remaining = self.remaining_s
        if remaining >= 3600:
            left = f"{remaining / 3600:.1f} horas"
        elif remaining >= 60:
            left = f"{remaining / 60:.0f} minutos"
        else:
            left = f"{remaining:.0f} segundos"
        label = {"timer": "timer", "alarm": "alarme", "reminder": "lembrete", "command": "tarefa"}[self.kind]
        suffix = f" ({self.text})" if self.text else ""
        return f"{label}{suffix} em {left}"


DueCallback = Callable[[ScheduledItem], Awaitable[None]]


class Scheduler:
    """Agendador assíncrono com persistência em JSON."""

    def __init__(self, on_due: DueCallback | None = None, path: Path | None = None) -> None:
        self.path = path or _SCHEDULE_FILE
        self.on_due = on_due
        self._items: dict[str, ScheduledItem] = {}
        self._task: asyncio.Task[None] | None = None
        self._load()

    # ------------------------------------------------------------------ #
    # Persistência
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            stale_limit = datetime.now() - timedelta(minutes=5)
            for entry in raw:
                item = ScheduledItem(**entry)
                # Timer que venceu com o JARVIS desligado já perdeu o sentido;
                # alarmes e lembretes ainda são avisados (atrasados).
                if item.kind == "timer" and item.due < stale_limit:
                    continue
                self._items[item.id] = item
            log.info("scheduler.loaded", count=len(self._items))
        except Exception as exc:
            log.warning("scheduler.load_failed", error=str(exc))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = [asdict(item) for item in self._items.values()]
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("scheduler.save_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    def add(self, kind: Kind, due: datetime, text: str = "") -> ScheduledItem:
        """Agenda uma entrada nova."""
        item = ScheduledItem(
            id=uuid.uuid4().hex[:8],
            kind=kind,
            due_iso=due.isoformat(timespec="seconds"),
            text=text.strip(),
        )
        self._items[item.id] = item
        self._save()
        log.info("scheduler.added", kind=kind, due=item.due_iso, text=item.text[:60])
        return item

    def add_in(self, kind: Kind, seconds: float, text: str = "") -> ScheduledItem:
        """Agenda para daqui a `seconds` segundos."""
        return self.add(kind, datetime.now() + timedelta(seconds=seconds), text)

    def cancel(self, kind: Kind | None = None, item_id: str | None = None) -> int:
        """Cancela por id, por tipo, ou tudo. Devolve quantos foram removidos."""
        if item_id:
            removed = 1 if self._items.pop(item_id, None) else 0
        else:
            targets = [key for key, item in self._items.items() if kind is None or item.kind == kind]
            for key in targets:
                self._items.pop(key, None)
            removed = len(targets)
        if removed:
            self._save()
        return removed

    def pending(self, kind: Kind | None = None) -> list[ScheduledItem]:
        """Entradas pendentes ordenadas pelo horário."""
        items = [item for item in self._items.values() if kind is None or item.kind == kind]
        return sorted(items, key=lambda item: item.due_iso)

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = datetime.now()
            due = [item for item in self._items.values() if item.due <= now]
            if not due:
                continue
            for item in due:
                self._items.pop(item.id, None)
            self._save()
            for item in sorted(due, key=lambda entry: entry.due_iso):
                log.info("scheduler.fire", kind=item.kind, text=item.text[:60])
                if self.on_due is None:
                    continue
                try:
                    await self.on_due(item)
                except Exception as exc:
                    log.error("scheduler.callback_failed", error=str(exc), kind=item.kind)

    def start(self) -> None:
        """Inicia o loop do agendador."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        """Encerra o loop (as entradas continuam salvas em disco)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


__all__ = [
    "Kind",
    "ScheduledItem",
    "Scheduler",
    "duration_seconds",
    "parse_clock",
    "parse_number",
]
