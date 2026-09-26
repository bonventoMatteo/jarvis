"""
Barramento de eventos interno do JARVIS.

Um único `EventBus` assíncrono com fan-out: cada assinante recebe sua própria
cópia de cada evento numa `asyncio.Queue`. Produtores que rodam em threads
(callbacks do sounddevice, hotkey global) usam `publish_threadsafe`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

import structlog

log = structlog.get_logger(__name__)


class EventType(str, Enum):
    """Todos os tipos de evento que trafegam no barramento."""

    # Ativação
    CLAP_DOUBLE = "CLAP_DOUBLE"
    WAKE_WORD = "WAKE_WORD"
    HOTKEY = "HOTKEY"
    ACTIVATE = "ACTIVATE"

    # Ciclo de vida do turno
    STATE_CHANGED = "STATE_CHANGED"
    TRANSCRIPT = "TRANSCRIPT"
    AGENT_DELTA = "AGENT_DELTA"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    RESULT = "RESULT"
    ERROR = "ERROR"

    # Telemetria / UI
    AUDIO_LEVEL = "AUDIO_LEVEL"
    CALIBRATED = "CALIBRATED"
    LATENCY = "LATENCY"
    NOTICE = "NOTICE"
    SPEAKING_TEXT = "SPEAKING_TEXT"

    # Controle
    SHUTDOWN = "SHUTDOWN"


#: Eventos que disparam um turno de conversa.
ACTIVATION_EVENTS: frozenset[EventType] = frozenset(
    {EventType.CLAP_DOUBLE, EventType.WAKE_WORD, EventType.HOTKEY, EventType.ACTIVATE}
)


@dataclass(slots=True)
class Event:
    """Uma mensagem no barramento."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    source: str = "system"

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class EventBus:
    """Barramento pub/sub com fan-out para múltiplos assinantes."""

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Registra o event loop usado por `publish_threadsafe`."""
        self._loop = loop or asyncio.get_running_loop()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue[Event]:
        """Cria uma fila nova já inscrita no barramento."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    # ------------------------------------------------------------------ #
    def publish(self, event: Event) -> None:
        """Publica um evento (chamar de dentro do event loop)."""
        if self._closed:
            return
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Descarta o item mais antigo para não travar o produtor.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    log.warning("event_bus.drop", type=event.type.value)

    def emit(self, type_: EventType, source: str = "system", **payload: Any) -> None:
        """Atalho para `publish(Event(...))`."""
        self.publish(Event(type=type_, payload=payload, source=source))

    # ------------------------------------------------------------------ #
    def publish_threadsafe(self, event: Event) -> None:
        """Publica a partir de outra thread (callbacks de áudio, hotkey)."""
        if self._closed:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:  # pragma: no cover - loop fechando
            pass

    def emit_threadsafe(self, type_: EventType, source: str = "system", **payload: Any) -> None:
        self.publish_threadsafe(Event(type=type_, payload=payload, source=source))

    # ------------------------------------------------------------------ #
    async def stream(self, queue: asyncio.Queue[Event] | None = None) -> AsyncIterator[Event]:
        """Itera indefinidamente sobre os eventos de uma assinatura."""
        own = queue is None
        queue = queue or self.subscribe()
        try:
            while not self._closed:
                event = await queue.get()
                yield event
                if event.type is EventType.SHUTDOWN:
                    break
        finally:
            if own:
                self.unsubscribe(queue)

    def close(self) -> None:
        """Encerra o barramento e acorda todos os assinantes."""
        if self._closed:
            return
        self.publish(Event(type=EventType.SHUTDOWN))
        self._closed = True
        self._subscribers.clear()


__all__ = ["ACTIVATION_EVENTS", "Event", "EventBus", "EventType"]
