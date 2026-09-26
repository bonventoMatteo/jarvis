"""
Painel ao vivo no terminal (rich Live).

Mostra:
  * estado atual da FSM com cor + LED virtual que pisca em ciano na ativação;
  * waveform ASCII do microfone com a linha do limiar de palmas;
  * o turno em andamento (transcrição, texto do agente em streaming,
    ferramentas chamadas);
  * os últimos comandos com rota, latência e resultado;
  * latências (STT, roteamento, execução, total) e avisos do sistema.

Só lê estado: tudo chega pelo `EventBus` ou por leitura de atributos do
orquestrador. Nunca bloqueia o loop — o render roda a `dashboard_fps`.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import settings
from core.events import ACTIVATION_EVENTS, Event, EventType
from core.state import STATE_COLORS, STATE_LABELS, State

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator

_BARS = " ▁▂▃▄▅▆▇█"
_TRIGGER_LABEL = {
    "CLAP_DOUBLE": "palmas",
    "WAKE_WORD": "voz",
    "HOTKEY": "atalho",
    "text": "texto",
    "schedule": "agenda",
}
_ROUTE_STYLE = {"regex": "green", "haiku": "yellow", "agent": "magenta"}


@dataclass
class _Turn:
    """Estado do turno em andamento, para o painel central."""

    trigger: str = ""
    transcript: str = ""
    agent_text: str = ""
    tools: list[tuple[str, bool | None]] = field(default_factory=list)
    speaking: str = ""


class Dashboard:
    """Painel rich Live alimentado pelo barramento de eventos."""

    def __init__(self, orchestrator: Orchestrator, console: Console | None = None) -> None:
        self.orc = orchestrator
        self.console = console or Console()
        self.started = time.monotonic()
        self.led_until = 0.0
        self.led_source = ""
        self.turn = _Turn()
        self.history: deque[dict[str, Any]] = deque(maxlen=8)
        self.notices: deque[tuple[str, str]] = deque(maxlen=6)
        self.latencies: deque[dict[str, int]] = deque(maxlen=50)
        self.waveform: list[float] = []
        self._frame = 0
        self._queue: asyncio.Queue[Event] | None = None

    # ------------------------------------------------------------------ #
    # Eventos
    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        try:
            for turn in self.orc.memory.recent_turns(8):
                self.history.append(
                    {
                        "ts": turn.ts[11:16],
                        "trigger": turn.trigger,
                        "transcript": turn.transcript,
                        "route": turn.route,
                        "intent": turn.intent,
                        "success": turn.success,
                        "latency_ms": turn.latency_ms,
                    }
                )
        except Exception:  # pragma: no cover - banco ainda não conectado
            pass

    def _handle(self, event: Event) -> None:
        match event.type:
            case t if t in ACTIVATION_EVENTS:
                self.led_until = time.monotonic() + 1.6
                self.led_source = _TRIGGER_LABEL.get(event.type.value, event.type.value)
                self.turn = _Turn(trigger=self.led_source)
            case EventType.TRANSCRIPT:
                if self.turn.transcript:  # novo turno sem ativação (texto/agenda)
                    self.turn = _Turn(trigger=_TRIGGER_LABEL.get(event.get("trigger", ""), ""))
                self.turn.transcript = event.get("text", "")
            case EventType.AGENT_DELTA:
                self.turn.agent_text = (self.turn.agent_text + event.get("text", ""))[-600:]
            case EventType.TOOL_CALL:
                self.turn.tools.append((event.get("name", "?"), None))
            case EventType.TOOL_RESULT:
                name = event.get("name", "?")
                for index in range(len(self.turn.tools) - 1, -1, -1):
                    if self.turn.tools[index][0] == name and self.turn.tools[index][1] is None:
                        self.turn.tools[index] = (name, bool(event.get("ok")))
                        break
                else:
                    self.turn.tools.append((name, bool(event.get("ok"))))
            case EventType.SPEAKING_TEXT:
                self.turn.speaking = event.get("text", "")
            case EventType.RESULT:
                self.history.append(
                    {
                        "ts": datetime.now().strftime("%H:%M"),
                        "trigger": event.get("trigger", ""),
                        "transcript": event.get("transcript", ""),
                        "route": event.get("route", ""),
                        "intent": event.get("intent", ""),
                        "success": event.get("success", True),
                        "latency_ms": event.get("latency_ms", 0),
                    }
                )
            case EventType.LATENCY:
                keys = ("stt_ms", "route_ms", "exec_ms", "total_ms")
                self.latencies.append({key: int(event.get(key, 0)) for key in keys})
            case EventType.AUDIO_LEVEL:
                self.waveform = list(event.get("waveform", []))
            case EventType.NOTICE:
                self.notices.append((datetime.now().strftime("%H:%M:%S"), str(event.get("text", ""))))
            case EventType.CALIBRATED:
                self.notices.append(
                    (
                        datetime.now().strftime("%H:%M:%S"),
                        f"calibrado: ruído {event.get('noise_floor', 0):.4f} · limiar {event.get('threshold', 0):.4f}",
                    )
                )
            case EventType.ERROR:
                self.notices.append((datetime.now().strftime("%H:%M:%S"), f"[erro] {event.get('text', '')}"))

    # ------------------------------------------------------------------ #
    # Render
    # ------------------------------------------------------------------ #
    def _header(self) -> Panel:
        state: State = self.orc.state.state
        color = STATE_COLORS[state]
        now = time.monotonic()
        blinking = now < self.led_until
        if blinking:
            led_style = "bold bright_cyan" if self._frame % 2 == 0 else "cyan"
            led = Text("◉", style=led_style)
        elif state in (State.LISTENING, State.SPEAKING, State.THINKING, State.EXECUTING):
            led = Text("◉", style=f"bold {color}")
        else:
            led = Text("○", style="bright_black" if self._frame % 12 < 10 else "cyan")

        uptime = int(now - self.started)
        title = Text.assemble(
            ("J.A.R.V.I.S", "bold bright_cyan"),
            ("  ·  ", "bright_black"),
            (STATE_LABELS[state], f"bold {color}"),
            (f"  {self.orc.state.elapsed:4.1f}s", "bright_black"),
        )
        right = Text.assemble(
            led,
            (f"  {self.led_source.upper()}" if blinking else "", "bright_cyan"),
            (f"   uptime {uptime // 3600:02d}:{uptime % 3600 // 60:02d}:{uptime % 60:02d}", "bright_black"),
            (f"   {datetime.now():%H:%M:%S}", "white"),
        )
        grid = Table.grid(expand=True)
        grid.add_column(justify="left")
        grid.add_column(justify="right")
        grid.add_row(title, right)
        return Panel(grid, border_style=color, padding=(0, 1))

    def _audio(self) -> Panel:
        clap = self.orc.clap
        mic = self.orc.mic
        wave = self.waveform or (list(mic.waveform) if mic is not None else [])
        threshold = clap.detector.threshold if clap is not None else 0.0
        scale = max(max(wave, default=0.0), threshold * 1.2, 0.02)

        spark = Text()
        for value in wave[-48:]:
            index = min(len(_BARS) - 1, int(value / scale * (len(_BARS) - 1)))
            style = "bold bright_cyan" if threshold and value >= threshold else ("cyan" if index > 2 else "blue")
            spark.append(_BARS[index], style=style)

        info = Table.grid(padding=(0, 1))
        info.add_column(style="bright_black")
        info.add_column()
        if mic is None:
            info.add_row("entrada", "modo texto")
        else:
            level = mic.level
            info.add_row("nível", f"{level:.4f}  pico {mic.peak:.3f}")
        if clap is not None:
            det = clap.detector
            info.add_row("ruído", f"{det.noise_floor:.4f}")
            info.add_row("limiar", f"{det.threshold:.4f}  (barras cianas = acima)")
            last = det.last_analysis
            if last is not None and last.peak >= det.threshold * 0.6:
                info.add_row("último", f"{last.reason} · ataque {last.attack_ms:.0f}ms · banda {last.band_ratio:.0%}")
            info.add_row("palmas", "●" * det.clap_count + "○" * max(0, det.required - det.clap_count))
        wake = self.orc.wake
        if wake is not None and wake.engine.available:
            info.add_row("wake", f"{wake.engine.last_score:.2f} / {wake.engine.threshold:.2f}")
        return Panel(Group(spark, Text(""), info), title="[cyan]áudio", border_style="blue")

    def _current(self) -> Panel:
        turn = self.turn
        body = Table.grid(padding=(0, 1))
        body.add_column(style="bright_black", no_wrap=True)
        body.add_column(overflow="fold")
        if turn.trigger:
            body.add_row("gatilho", Text(turn.trigger, style="bright_cyan"))
        body.add_row("você", Text(turn.transcript or "—", style="white"))
        if turn.agent_text:
            body.add_row("agente", Text(turn.agent_text.strip()[-300:], style="magenta"))
        if turn.tools:
            tools = Text()
            for name, ok in turn.tools[-8:]:
                mark, style = ("…", "yellow") if ok is None else (("✓", "green") if ok else ("✗", "red"))
                tools.append(f"{mark} {name}  ", style=style)
            body.add_row("ações", tools)
        if turn.speaking:
            body.add_row(settings.assistant_name.lower(), Text(turn.speaking, style="bright_green"))
        return Panel(body, title="[cyan]turno atual", border_style="blue")

    def _history(self) -> Panel:
        table = Table(expand=True, box=None, header_style="bright_black", pad_edge=False)
        table.add_column("hora", width=5)
        table.add_column("via", width=7)
        table.add_column("comando", ratio=3, overflow="ellipsis", no_wrap=True)
        table.add_column("rota", width=12)
        table.add_column("ms", justify="right", width=6)
        table.add_column("", width=1)
        for item in reversed(self.history):
            route = item.get("route", "")
            route_text = Text(route, style=_ROUTE_STYLE.get(route, "white"))
            if item.get("intent"):
                route_text.append(f":{item['intent']}"[:12 - len(route)], style="bright_black")
            table.add_row(
                item.get("ts", ""),
                _TRIGGER_LABEL.get(item.get("trigger", ""), item.get("trigger", ""))[:7],
                item.get("transcript", ""),
                route_text,
                str(item.get("latency_ms", "")),
                Text("✓", style="green") if item.get("success") else Text("✗", style="red"),
            )
        if not self.history:
            hint = Text("bata palmas duas vezes, diga o wake word ou use o atalho", style="bright_black")
            table.add_row("", "", hint, "", "", "")
        return Panel(table, title="[cyan]últimos comandos", border_style="blue")

    def _latency(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bright_black")
        table.add_column(justify="right")
        table.add_column(justify="right", style="bright_black")
        last = self.latencies[-1] if self.latencies else {}
        count = len(self.latencies) or 1
        for key, label in (("stt_ms", "stt"), ("route_ms", "rota"), ("exec_ms", "exec"), ("total_ms", "total")):
            average = sum(item.get(key, 0) for item in self.latencies) / count
            value = last.get(key, 0)
            style = "green" if value < 1200 else ("yellow" if value < 3500 else "red")
            table.add_row(label, Text(f"{value} ms", style=style), f"méd {average:.0f}")
        return Panel(table, title="[cyan]latência", border_style="blue")

    def _system(self) -> Panel:
        orc = self.orc
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bright_black")
        table.add_column(overflow="fold")
        table.add_row("ativação", ", ".join(orc.activation_methods) or "—")
        whisper = orc.whisper
        stt = f"{whisper.model_name} · {whisper.device}/{whisper.compute_type}" if whisper.ready else "carregando…"
        table.add_row("stt", stt)
        table.add_row("voz", orc.tts.backend)
        llm = f"{settings.model_fast} / {settings.model_agent}" if settings.has_api_key else "sem chave da API"
        table.add_row("llm", llm)
        pending = orc.scheduler.pending()
        if pending:
            table.add_row("agenda", "; ".join(item.describe() for item in pending[:2]))
        return Panel(table, title="[cyan]sistema", border_style="blue")

    def _notices(self) -> Panel:
        text = Text()
        for ts, message in list(self.notices)[-4:]:
            text.append(f"{ts} ", style="bright_black")
            text.append(f"{message}\n", style="white")
        return Panel(text or Text("—", style="bright_black"), title="[cyan]avisos", border_style="blue")

    def render(self) -> Layout:
        """Monta o layout completo."""
        layout = Layout()
        layout.split_column(
            Layout(self._header(), name="header", size=3),
            Layout(name="middle", ratio=3),
            Layout(self._history(), name="history", ratio=3),
            Layout(name="bottom", size=8),
        )
        layout["middle"].split_row(Layout(self._audio(), ratio=2), Layout(self._current(), ratio=3))
        layout["bottom"].split_row(
            Layout(self._latency(), ratio=2), Layout(self._system(), ratio=3), Layout(self._notices(), ratio=3)
        )
        return layout

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Consome eventos e redesenha até o cancelamento."""
        self._queue = self.orc.bus.subscribe(maxsize=512)
        self._load_history()
        interval = 1.0 / max(1, settings.dashboard_fps)
        try:
            with Live(self.render(), console=self.console, screen=True, auto_refresh=False) as live:
                while True:
                    while not self._queue.empty():
                        event = self._queue.get_nowait()
                        if event.type is EventType.SHUTDOWN:
                            return
                        self._handle(event)
                    self._frame += 1
                    live.update(self.render(), refresh=True)
                    await asyncio.sleep(interval)
        finally:
            self.orc.bus.unsubscribe(self._queue)


def splash(console: Console) -> None:
    """Banner curto exibido antes do painel (e no modo texto)."""
    console.print(
        Align.center(
            Panel(
                Text.assemble(
                    ("J.A.R.V.I.S\n", "bold bright_cyan"),
                    ("Just A Rather Very Intelligent System", "bright_black"),
                ),
                border_style="cyan",
                padding=(1, 6),
            )
        )
    )


__all__ = ["Dashboard", "splash"]
