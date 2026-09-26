"""Event bus, FSM, agendador e parser de sim/não."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.events import EventBus, EventType
from core.scheduler import ScheduledItem, Scheduler, duration_seconds, parse_clock, parse_number
from core.state import State, StateMachine


def _parse_yes_no():  # import tardio: o orquestrador importa módulos de áudio preguiçosamente
    from core.orchestrator import parse_yes_no

    return parse_yes_no


async def test_event_bus_fan_out() -> None:
    bus = EventBus()
    bus.bind_loop()
    first, second = bus.subscribe(), bus.subscribe()
    bus.emit(EventType.NOTICE, text="oi")
    assert (await first.get()).get("text") == "oi"
    assert (await second.get()).get("text") == "oi"


async def test_event_bus_threadsafe() -> None:
    bus = EventBus()
    bus.bind_loop()
    queue = bus.subscribe()
    await asyncio.to_thread(bus.emit_threadsafe, EventType.HOTKEY, "hotkey")
    event = await asyncio.wait_for(queue.get(), 1.0)
    assert event.type is EventType.HOTKEY


async def test_state_machine_transitions() -> None:
    bus = EventBus()
    bus.bind_loop()
    queue = bus.subscribe()
    fsm = StateMachine(bus, State.IDLE)
    assert fsm.set(State.LISTENING)
    assert fsm.set(State.THINKING)
    assert fsm.set(State.EXECUTING)
    assert fsm.set(State.SPEAKING)
    assert fsm.set(State.IDLE)
    assert not fsm.set(State.CALIBRATING)  # inválida
    assert fsm.state is State.IDLE
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [event.get("state") for event in events] == ["LISTENING", "THINKING", "EXECUTING", "SPEAKING", "IDLE"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sim", True),
        ("Sim, pode desligar.", True),
        ("confirmo", True),
        ("não", False),
        ("nao, cancela", False),
        ("sim... não, espera", False),
        ("hmm talvez", None),
        ("", None),
    ],
)
def test_parse_yes_no(text: str, expected: bool | None) -> None:
    assert _parse_yes_no()(text) is expected


@pytest.mark.parametrize(
    ("token", "value"),
    [("5", 5.0), ("2,5", 2.5), ("cinco", 5.0), ("meia", 30.0), ("vinte e cinco", 25.0), ("xyz", None)],
)
def test_parse_number(token: str, value: float | None) -> None:
    assert parse_number(token) == value


def test_duration_seconds() -> None:
    assert duration_seconds("5", "minutos") == 300
    assert duration_seconds("dez", "segundos") == 10
    assert duration_seconds("1", "hora") == 3600
    assert duration_seconds("x", "minutos") is None


@pytest.mark.parametrize(
    ("spoken", "hour", "minute"),
    [
        ("7:30", 7, 30),
        ("7h30", 7, 30),
        ("22h", 22, 0),
        ("19 horas", 19, 0),
        ("6 e meia", 6, 30),
        ("sete e quinze da noite", 19, 15),
        ("às 8 da manhã", 8, 0),
    ],
)
def test_parse_clock(spoken: str, hour: int, minute: int) -> None:
    now = datetime(2026, 9, 26, 5, 0)
    result = parse_clock(spoken, now)
    assert result is not None, spoken
    assert (result.hour, result.minute) == (hour, minute)
    assert result > now


def test_parse_clock_rolls_to_tomorrow() -> None:
    now = datetime(2026, 9, 26, 23, 0)
    result = parse_clock("7:00", now)
    assert result is not None and result.date() == (now + timedelta(days=1)).date()


def test_parse_clock_invalid() -> None:
    assert parse_clock("banana") is None
    assert parse_clock("25:99") is None


async def test_scheduler_fires_and_persists(tmp_path: Path) -> None:
    fired: list[ScheduledItem] = []

    async def on_due(item: ScheduledItem) -> None:
        fired.append(item)

    path = tmp_path / "schedule.json"
    scheduler = Scheduler(on_due=on_due, path=path)
    scheduler.add_in("timer", 0.2, "teste")
    scheduler.add_in("reminder", 3600, "depois")
    assert path.exists()

    # Outra instância lê o mesmo arquivo.
    assert len(Scheduler(path=path).pending()) == 2

    scheduler.start()
    await asyncio.sleep(1.2)
    await scheduler.stop()
    assert [item.text for item in fired] == ["teste"]
    assert [item.kind for item in scheduler.pending()] == ["reminder"]
    assert scheduler.cancel("reminder") == 1
    assert Scheduler(path=path).pending() == []
