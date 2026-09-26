"""
Máquina de estados do JARVIS.

IDLE → LISTENING → THINKING → EXECUTING → SPEAKING → IDLE
Qualquer estado pode ir para ERROR, que volta sozinho para IDLE.
"""
from __future__ import annotations

import time
from enum import Enum

import structlog

from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


class State(str, Enum):
    """Estados possíveis do assistente."""

    BOOTING = "BOOTING"
    CALIBRATING = "CALIBRATING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"
    SHUTDOWN = "SHUTDOWN"


#: Cor (rich) associada a cada estado, usada pelo dashboard.
STATE_COLORS: dict[State, str] = {
    State.BOOTING: "bright_black",
    State.CALIBRATING: "yellow",
    State.IDLE: "cyan",
    State.LISTENING: "bright_cyan",
    State.THINKING: "magenta",
    State.EXECUTING: "bright_yellow",
    State.SPEAKING: "bright_green",
    State.ERROR: "bright_red",
    State.SHUTDOWN: "red",
}

#: Rótulo curto em pt-BR para cada estado.
STATE_LABELS: dict[State, str] = {
    State.BOOTING: "INICIANDO",
    State.CALIBRATING: "CALIBRANDO",
    State.IDLE: "EM ESPERA",
    State.LISTENING: "OUVINDO",
    State.THINKING: "PROCESSANDO",
    State.EXECUTING: "EXECUTANDO",
    State.SPEAKING: "FALANDO",
    State.ERROR: "ERRO",
    State.SHUTDOWN: "DESLIGANDO",
}

#: Transições válidas. ERROR e SHUTDOWN são alcançáveis de qualquer lugar.
_ALLOWED: dict[State, set[State]] = {
    State.BOOTING: {State.CALIBRATING, State.IDLE},
    State.CALIBRATING: {State.IDLE},
    State.IDLE: {State.LISTENING, State.THINKING, State.SPEAKING, State.EXECUTING},
    State.LISTENING: {State.THINKING, State.IDLE, State.SPEAKING},
    State.THINKING: {State.EXECUTING, State.SPEAKING, State.IDLE},
    State.EXECUTING: {State.SPEAKING, State.THINKING, State.IDLE},
    State.SPEAKING: {State.IDLE, State.LISTENING, State.EXECUTING, State.THINKING},
    State.ERROR: {State.IDLE, State.SPEAKING},
    State.SHUTDOWN: set(),
}


class StateMachine:
    """FSM observável; publica `STATE_CHANGED` a cada transição."""

    def __init__(self, bus: EventBus, initial: State = State.BOOTING) -> None:
        self._bus = bus
        self._state = initial
        self._since = time.monotonic()
        self._history: list[tuple[State, float]] = [(initial, self._since)]

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> State:
        return self._state

    @property
    def elapsed(self) -> float:
        """Segundos no estado atual."""
        return time.monotonic() - self._since

    @property
    def is_busy(self) -> bool:
        """True quando um turno está em andamento."""
        return self._state in {
            State.LISTENING,
            State.THINKING,
            State.EXECUTING,
            State.SPEAKING,
        }

    # ------------------------------------------------------------------ #
    def can(self, target: State) -> bool:
        """Indica se a transição para `target` é permitida."""
        if target in (State.ERROR, State.SHUTDOWN):
            return True
        return target in _ALLOWED.get(self._state, set())

    def set(self, target: State, *, force: bool = False, detail: str = "") -> bool:
        """
        Transiciona para `target`.

        Retorna False (sem alterar nada) se a transição for inválida e
        `force` for False.
        """
        if target is self._state:
            return True
        if not force and not self.can(target):
            log.warning("state.invalid_transition", **{"from": self._state.value, "to": target.value})
            return False

        previous, held = self._state, self.elapsed
        self._state = target
        self._since = time.monotonic()
        self._history.append((target, self._since))
        del self._history[:-200]

        log.info("state.change", **{"from": previous.value, "to": target.value, "held_s": round(held, 3)})
        self._bus.emit(
            EventType.STATE_CHANGED,
            source="state",
            previous=previous.value,
            state=target.value,
            label=STATE_LABELS[target],
            color=STATE_COLORS[target],
            held_s=held,
            detail=detail,
        )
        return True

    def reset(self) -> None:
        """Volta para IDLE independentemente do estado atual."""
        self.set(State.IDLE, force=True)


__all__ = ["STATE_COLORS", "STATE_LABELS", "State", "StateMachine"]
