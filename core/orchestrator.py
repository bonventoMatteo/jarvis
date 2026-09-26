"""
Orquestrador do JARVIS — coordena tudo via asyncio.

Fluxo de um turno de voz:

    palma dupla / wake word / Ctrl+Alt+J
      → LISTENING: som "bwoom" + "Sim?" + gravação até o silêncio (silero)
      → THINKING : hum eletrônico em loop + transcrição (faster-whisper)
      → roteador : regex (instantâneo) → Haiku → Sonnet com ferramentas
      → EXECUTING: bipe + fala do que está fazendo + execução real
      → SPEAKING : resultado em uma frase (piper + pedalboard)
      → IDLE

Cada turno recebe um `trace_id` (structlog contextvars) que aparece em todos
os logs daquele comando. O loop principal nunca bloqueia: o turno roda numa
task própria enquanto o loop continua consumindo eventos (bipes de
ferramenta, barge-in pelo hotkey, lembretes do agendador).
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from config import settings
from core.events import ACTIVATION_EVENTS, Event, EventBus, EventType
from core.ipc import TriggerServer
from core.scheduler import ScheduledItem, Scheduler
from core.state import State, StateMachine
from executor.browser import BrowserController
from executor.commands import FastCommandExecutor
from executor.pc import ActionResult, PCController
from llm.agent import AgentResult, JarvisAgent
from llm.router import Intent, Router, fold
from llm.tools import ToolExecutor
from memory.store import MemoryStore
from tts import voice_lines as lines

log = structlog.get_logger(__name__)

_YES = re.compile(
    r"\b(sim|confirm\w*|pode|claro|positivo|afirmativo|isso|manda|prossig\w*|ok|okay|beleza|vai"
    r"|faca|faz|execut\w*|com certeza|certo|yes)\b"
)
_NO = re.compile(r"\b(nao|cancel\w*|negativo|para|pare|espera|nunca|jamais|aborta\w*|no|esquece)\b")


#: Frases que o Whisper "ouve" em silêncio ou ruído (alucinações conhecidas em pt).
_HALLUCINATIONS = re.compile(
    r"^(?:obrigad[oa]|tchau|legendas? (?:pela|por)|inscreva-se|e ai|ate a proxima|"
    r"ate mais|musica|aplausos|risos|\W*)[\s.!,]*$|amara\.org|legendado por|transcri(?:cao|to) por"
)


def is_noise_transcript(text: str, avg_logprob: float = 0.0) -> bool:
    """True para transcrições que quase certamente não são um comando real."""
    folded = fold(text).strip()
    if len(folded.replace(" ", "")) < 3:
        return True
    if avg_logprob < -1.0:
        return True
    return bool(_HALLUCINATIONS.search(folded))


def parse_yes_no(text: str) -> bool | None:
    """'sim, pode' -> True; 'não' -> False; ambíguo -> None. 'não' vence."""
    folded = fold(text)
    if _NO.search(folded):
        return False
    if _YES.search(folded):
        return True
    return None


@dataclass(slots=True)
class TurnMetrics:
    """Latências de um turno (ms)."""

    started: float = field(default_factory=time.monotonic)
    stt_ms: int = 0
    route_ms: int = 0
    exec_ms: int = 0
    ready_ms: int = 0  # até a resposta ficar pronta (sem contar a fala)

    @property
    def total_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class Orchestrator:
    """Liga microfone, detectores, STT, roteador, agente, executor, TTS e UI."""

    def __init__(
        self,
        *,
        text_mode: bool = False,
        enable_clap: bool | None = None,
        enable_wake: bool | None = None,
        enable_hotkey: bool | None = None,
        skip_calibration: bool = False,
        mute_voice: bool = False,
    ) -> None:
        self.text_mode = text_mode
        self.skip_calibration = skip_calibration
        self.mute_voice = mute_voice

        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.memory = MemoryStore()
        self.pc = PCController()
        self.browser = BrowserController()
        self.scheduler = Scheduler(on_due=self._on_scheduled)
        self.ipc = TriggerServer(self.bus)
        self.router = Router()
        self.commands = FastCommandExecutor(self.pc, self.scheduler, self.memory)
        self.agent = JarvisAgent(self.bus, self.memory, self.pc)
        self.agent.bind_tools(
            ToolExecutor(
                pc=self.pc,
                browser=self.browser,
                scheduler=self.scheduler,
                memory=self.memory,
                speak=self._tool_speak,
                confirm=self.ask_confirmation,
                click_element=self.agent.click_element,
            )
        )

        # Áudio (importes preguiçosos: permitem `--text` sem sounddevice).
        from audio.player import AudioOutput
        from stt.whisper_engine import WhisperEngine
        from tts.piper_engine import PiperEngine

        self.sfx = AudioOutput()
        self.tts = PiperEngine()
        self.whisper = WhisperEngine()

        self.mic = None
        self.recorder = None
        self.clap = None
        self.wake = None
        self.hotkey = None
        if not text_mode:
            from audio.clap import ClapListener
            from audio.hotkey import HotkeyListener
            from audio.mic import Microphone
            from audio.vad import SpeechRecorder
            from audio.wake import WakeWordListener

            self.mic = Microphone(self.bus)
            self.recorder = SpeechRecorder(self.bus, self.mic)
            self.clap = ClapListener(self.bus, self.mic)
            self.wake = WakeWordListener(self.bus, self.mic)
            self.hotkey = HotkeyListener(self.bus)
            if enable_clap is not None:
                self.clap.enabled = enable_clap
            if enable_wake is not None:
                self.wake.enabled = enable_wake
            self._hotkey_enabled = settings.hotkey_enabled if enable_hotkey is None else enable_hotkey
        else:
            self._hotkey_enabled = False

        self._turn_task: asyncio.Task[None] | None = None
        self._speech_lock = asyncio.Lock()
        self._mic_mute_depth = 0
        self._stop = asyncio.Event()
        self._background: set[asyncio.Task[Any]] = set()
        self.last_response = ""
        self.noise_floor = 0.0
        self.activation_methods: list[str] = []

    # ------------------------------------------------------------------ #
    # Utilidades
    # ------------------------------------------------------------------ #
    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        """Cria uma task de fundo mantendo referência (evita GC) e logando erros."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._background.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("orchestrator.task_failed", task=name, error=str(finished.exception()))

        task.add_done_callback(_done)
        return task

    def _set(self, target: State, **kwargs: Any) -> None:
        """Transição tolerante: força se a FSM recusar (nunca trava um turno)."""
        if not self.state.set(target, **kwargs):
            self.state.set(target, force=True)

    def _mute_mic(self) -> None:
        self._mic_mute_depth += 1
        if self.mic is not None:
            self.mic.pause()

    def _unmute_mic(self) -> None:
        self._mic_mute_depth = max(0, self._mic_mute_depth - 1)
        if self._mic_mute_depth == 0 and self.mic is not None:
            self.mic.resume()

    @property
    def busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    # ------------------------------------------------------------------ #
    # Voz
    # ------------------------------------------------------------------ #
    async def speak(
        self,
        text: str,
        mood: str = "neutral",
        *,
        restore: State | None = None,
        set_state: bool = True,
    ) -> None:
        """
        Fala um texto (serializado: nunca duas falas ao mesmo tempo).

        O microfone é pausado durante a fala para o JARVIS não ouvir a si
        mesmo (nem disparar o detector de palmas com consoantes plosivas).
        """
        text = (text or "").strip()
        if not text:
            return
        text = text[:1].upper() + text[1:]
        async with self._speech_lock:
            self.sfx.stop_ambient()
            if set_state and self.state.state not in (State.BOOTING, State.CALIBRATING, State.SHUTDOWN):
                self._set(State.SPEAKING)
            self.bus.emit(EventType.SPEAKING_TEXT, source="tts", text=text)
            self._mute_mic()
            try:
                if not self.mute_voice and self.memory.get_pref("tts_enabled", True):
                    await self.tts.speak(text, mood)
            finally:
                # Deixa o reverb da sala morrer antes de voltar a ouvir.
                if self.mic is not None:
                    await asyncio.sleep(0.15)
                self._unmute_mic()
            if restore is not None and self.state.state is State.SPEAKING:
                self._set(restore)

    def say(self, text: str, mood: str = "neutral", restore: State | None = None) -> asyncio.Task[None]:
        """Fala sem bloquear quem chamou."""
        return self._spawn(self.speak(text, mood, restore=restore), "say")

    async def _tool_speak(self, text: str, emotion: str = "neutral") -> None:
        await self.speak(text, emotion, restore=State.EXECUTING)
        self.sfx.start_ambient("thinking")

    async def _interim(self, text: str) -> None:
        """Fala o que o agente disse antes de usar ferramentas."""
        await self.speak(text, "confirm", restore=State.EXECUTING)
        self.sfx.start_ambient("thinking")

    # ------------------------------------------------------------------ #
    # Escuta
    # ------------------------------------------------------------------ #
    async def listen(self, max_seconds: float | None = None, initial_timeout: float = 5.0) -> str:
        """Grava até o silêncio e transcreve. Devolve '' se nada foi dito."""
        if self.text_mode:
            prompt = "você> "
            try:
                return (await asyncio.wait_for(asyncio.to_thread(input, prompt), timeout=120.0)).strip()
            except (TimeoutError, EOFError):
                return ""
        assert self.recorder is not None
        self._set(State.LISTENING)
        recording = await self.recorder.record_until_silence(
            max_seconds=max_seconds, initial_timeout=initial_timeout, prespeech=False
        )
        if not recording.is_usable:
            return ""
        self._set(State.THINKING)
        transcription = await self.whisper.transcribe(recording.audio)
        return transcription.text

    async def ask_confirmation(self, question: str, double: bool = False) -> bool:
        """
        Pergunta sim/não em voz alta e escuta a resposta.

        Com `double=True` exige uma segunda confirmação ("confirmo") —
        usado para desligar/reiniciar.
        """
        previous = self.state.state
        for attempt in range(2):
            await self.speak(question if attempt == 0 else lines.pick(lines.CONFIRM_REPEAT), "confirm")
            answer = await self.listen(max_seconds=6.0, initial_timeout=settings.confirm_timeout_s)
            verdict = parse_yes_no(answer) if answer else None
            log.info("confirm.answer", question=question[:60], answer=answer, verdict=verdict)
            if verdict is not None:
                break
        else:
            verdict = False

        if verdict and double:
            await self.speak(lines.pick(lines.DOUBLE_CONFIRM), "urgent")
            answer = await self.listen(max_seconds=6.0, initial_timeout=settings.confirm_timeout_s)
            verdict = parse_yes_no(answer) is True

        if previous in (State.THINKING, State.EXECUTING):
            self._set(previous)
        return bool(verdict)

    # ------------------------------------------------------------------ #
    # Boot
    # ------------------------------------------------------------------ #
    async def boot_sequence(self) -> None:
        """Som de boot, carga dos modelos, calibração e "Sistemas online"."""
        self.bus.bind_loop()
        self._set(State.BOOTING)
        await asyncio.to_thread(self.memory.connect)
        self.sfx.start()
        if settings.play_boot_sound:
            self.sfx.play("boot")
        boot_started = time.monotonic()

        # STT carrega em segundo plano (pode baixar o modelo na 1ª vez).
        stt_task = self._spawn(self.whisper.load(), "stt-load")
        stt_task.add_done_callback(
            lambda task: self.bus.emit(
                EventType.NOTICE, source="stt",
                text=(f"STT pronto ({self.whisper.model_name} em {self.whisper.device})"
                      if not task.cancelled() and task.exception() is None
                      else f"STT falhou: {task.exception() if not task.cancelled() else 'cancelado'}"),
            )
        )

        loaders: list[Any] = [self.tts.load()]
        if self.wake is not None and self.wake.enabled:
            loaders.append(self.wake.load())
        await asyncio.gather(*loaders, return_exceptions=True)
        self.bus.emit(EventType.NOTICE, source="tts", text=f"voz: {self.tts.backend}")

        # Espera o som de boot respirar antes da primeira fala.
        remaining = 1.6 - (time.monotonic() - boot_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        await self.speak(lines.pick(lines.BOOT), "neutral", set_state=False)
        self._spawn(self.tts.preload(lines.all_fixed_phrases()), "tts-preload")

        if self.mic is not None:
            await self.mic.start()
            await self.calibrate_noise()
            assert self.recorder is not None
            await asyncio.to_thread(self.recorder.load, max(self.noise_floor, 0.005))
            self._start_listeners()
        self.scheduler.start()
        if await self.ipc.start():
            self.activation_methods.append("externo")
        self._set(State.IDLE)
        methods = ", ".join(self.activation_methods) or "texto"
        self.bus.emit(EventType.NOTICE, source="boot", text=f"ativação: {methods}")
        log.info("boot.done", activation=methods, stt=self.whisper.model_name)

    async def calibrate_noise(self) -> None:
        """Escuta o ambiente para ajustar o limiar das palmas."""
        if self.clap is None:
            return
        self._set(State.CALIBRATING)
        if self.skip_calibration:
            self.noise_floor = self.clap.detector.noise_floor
            self.clap.detector.calibrated = True
            return
        self.noise_floor = await self.clap.calibrate()

    def _start_listeners(self) -> None:
        self.activation_methods.clear()
        if self.clap is not None and self.clap.enabled:
            self.clap.start()
            self.activation_methods.append("palmas")
        if self.wake is not None and self.wake.enabled and self.wake.engine.available:
            self.wake.start()
            self.activation_methods.append("wake word")
        if self.hotkey is not None and self._hotkey_enabled and self.hotkey.start():
            self.activation_methods.append(settings.hotkey)
        if settings.always_listen and self.recorder is not None:
            self._spawn(self._listen_loop(), "listen-loop")
            self.activation_methods.insert(0, "escuta contínua")

    # ------------------------------------------------------------------ #
    # Loop principal
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Boot + loop de eventos até `stop()`."""
        queue = self.bus.subscribe()
        try:
            await self.boot_sequence()
            if self.text_mode:
                self._spawn(self._text_loop(), "text-loop")
            event_task = asyncio.create_task(self._event_loop(queue), name="event-loop")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop-wait")
            await asyncio.wait({event_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in (event_task, stop_task):
                task.cancel()
        finally:
            self.bus.unsubscribe(queue)
            await self.shutdown()

    async def _event_loop(self, queue: asyncio.Queue[Event]) -> None:
        async for event in self.bus.stream(queue):
            if event.type in ACTIVATION_EVENTS:
                self._on_activation(event)
            elif event.type is EventType.TRANSCRIPT_REQUEST:
                if not self.busy:
                    self._turn_task = asyncio.create_task(
                        self.process_text(str(event.get("text", "")), "ipc"), name="turn"
                    )
            elif event.type is EventType.TOOL_CALL:
                if self.state.state is State.THINKING:
                    self._set(State.EXECUTING)
                self.sfx.play("confirm", gain=settings.sfx_volume * 0.6)
            elif event.type is EventType.SHUTDOWN:
                self._stop.set()
                return

    def _on_activation(self, event: Event) -> None:
        """Palma, wake word ou hotkey. Interrompe a fala (barge-in) se preciso."""
        if self.state.state in (State.BOOTING, State.CALIBRATING, State.SHUTDOWN):
            return
        if settings.always_listen and self.mic is not None:
            # Na escuta contínua a própria frase já é o comando: a palma só
            # interrompe a fala ou confirma que ele está ouvindo.
            if self.state.state is State.SPEAKING:
                self.tts.interrupt()
            elif not self.busy:
                self.sfx.play("activate")
            return
        if self.busy:
            if self.state.state is State.SPEAKING:
                log.info("activation.barge_in", trigger=event.type.value)
                self.tts.interrupt()
                assert self._turn_task is not None
                self._turn_task.cancel()
            else:
                log.info("activation.ignored_busy", trigger=event.type.value, state=self.state.state.value)
                return
        self._turn_task = asyncio.create_task(self._voice_turn(event), name="turn")

    async def _listen_loop(self) -> None:
        """
        Escuta contínua: grava cada frase (silero-vad), transcreve e executa.

        Ignora ruído, alucinações do Whisper e — se configurado — frases sem
        o nome do assistente. Nunca roda enquanto um turno está em andamento
        nem enquanto o JARVIS fala (o microfone fica pausado).
        """
        assert self.recorder is not None
        name = fold(settings.assistant_name)
        while not self._stop.is_set():
            if self.busy or self.state.state is not State.IDLE:
                await asyncio.sleep(0.2)
                continue
            try:
                recording = await self.recorder.record_until_silence(initial_timeout=30.0, prespeech=True)
                if not recording.is_usable or self.busy:
                    continue
                metrics = TurnMetrics()
                transcription = await self.whisper.transcribe(recording.audio)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("listen.failed", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            metrics.stt_ms = int((time.monotonic() - metrics.started) * 1000)

            text = transcription.text.strip()
            if not text or is_noise_transcript(text, transcription.avg_logprob):
                log.debug("listen.ignored_noise", text=text, logprob=round(transcription.avg_logprob, 2))
                continue
            if settings.always_listen_require_name and name not in fold(text):
                log.debug("listen.ignored_no_name", text=text)
                continue
            quick = self.router.match(text)
            if quick is not None and quick.name == "cancel":
                continue  # "obrigado", "nada"... sem resposta na escuta contínua
            if self.busy:
                continue

            structlog.contextvars.bind_contextvars(trace_id=uuid.uuid4().hex[:8])
            self.sfx.play("activate", gain=settings.sfx_volume * 0.5)
            self._turn_task = asyncio.create_task(
                self.process_text(text, "continuo", metrics=metrics), name="turn"
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
            finally:
                structlog.contextvars.unbind_contextvars("trace_id")

    async def _text_loop(self) -> None:
        """Modo texto: cada linha digitada é um comando."""
        while not self._stop.is_set():
            try:
                line = await asyncio.to_thread(input, "você> ")
            except (EOFError, KeyboardInterrupt):
                self.stop()
                return
            line = line.strip()
            if not line:
                continue
            if line.lower() in {"sair", "exit", "quit"}:
                await self.speak(lines.pick(lines.GOODBYE))
                self.stop()
                return
            self._turn_task = asyncio.create_task(self.process_text(line, "text"), name="turn")
            try:
                await self._turn_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # Turnos
    # ------------------------------------------------------------------ #
    async def _voice_turn(self, event: Event) -> None:
        """
        Turno de voz. Depois de responder, continua ouvindo por
        `followup_seconds` (modo conversa) — sem precisar de nova palma.
        "Obrigado", "cancela" ou silêncio encerram.
        """
        trigger = event.type.value
        trace_id = uuid.uuid4().hex[:8]
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        first = True
        try:
            assert self.recorder is not None
            if self.clap is not None:
                self.clap.detector.reset()
            if self.wake is not None:
                self.wake.engine.reset()

            while not self._stop.is_set():
                self._set(State.LISTENING, detail=trigger if first else "conversa")
                if first:
                    self.sfx.play("activate")
                    await self.speak(lines.pick(lines.ACKNOWLEDGE), "confirm", restore=State.LISTENING)
                    recording = await self.recorder.record_until_silence(prespeech=False)
                else:
                    self.sfx.play("confirm", gain=settings.sfx_volume * 0.4)
                    recording = await self.recorder.record_until_silence(
                        initial_timeout=settings.followup_seconds, prespeech=False
                    )

                if not recording.speech_detected:
                    if first:
                        self.sfx.play("error", gain=settings.sfx_volume * 0.35)
                    self._set(State.IDLE)
                    return
                if not recording.is_usable:
                    if first:
                        await self.speak(lines.pick(lines.NOT_UNDERSTOOD))
                    self._set(State.IDLE)
                    return

                metrics = TurnMetrics()  # latência conta a partir do fim da sua fala
                self._set(State.THINKING)
                self.sfx.start_ambient("thinking")
                transcription = await self.whisper.transcribe(recording.audio)
                metrics.stt_ms = int((time.monotonic() - metrics.started) * 1000)
                if transcription.is_empty:
                    self.sfx.stop_ambient()
                    if first:
                        await self.speak(lines.pick(lines.NOT_UNDERSTOOD))
                    self._set(State.IDLE)
                    return

                intent = await self.process_text(
                    transcription.text, trigger if first else "conversa", metrics=metrics, trace_id=trace_id
                )
                if settings.followup_seconds <= 0 or intent in ("cancel", "quit"):
                    return
                first = False
        except asyncio.CancelledError:
            self.sfx.stop_ambient()
            raise
        except Exception as exc:
            log.exception("turn.failed", error=str(exc))
            self.sfx.stop_ambient()
            self.sfx.play("error")
            await self.speak(lines.error_line(str(exc)[:120]), "calm")
            self._set(State.IDLE)
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")

    async def process_text(
        self,
        text: str,
        trigger: str,
        *,
        metrics: TurnMetrics | None = None,
        trace_id: str | None = None,
    ) -> str:
        """Roteia, executa e responde um comando já transcrito. Devolve o nome do intent."""
        metrics = metrics or TurnMetrics()
        own_trace = trace_id is None
        if own_trace:
            structlog.contextvars.bind_contextvars(trace_id=uuid.uuid4().hex[:8])
        self.bus.emit(EventType.TRANSCRIPT, source="stt", text=text, trigger=trigger)
        log.info("turn.transcript", text=text, trigger=trigger)
        if self.state.state is not State.THINKING:
            self._set(State.THINKING)
        if not self.sfx.ambient_active:
            self.sfx.start_ambient("thinking")

        route = "agent"
        intent_name = ""
        tools: list[str] = []
        success = True
        response = ""
        try:
            route_started = time.monotonic()
            intent = await self.router.route(text)
            metrics.route_ms = int((time.monotonic() - route_started) * 1000)

            exec_started = time.monotonic()
            if intent is not None:
                route, intent_name = intent.source, intent.name
                stop_after = False
                success, response, stop_after = await self._handle_intent(intent)
            else:
                agent_result = await self._run_agent(text)
                tools = agent_result.tools
                success = agent_result.success
                response = self._agent_reply(agent_result)
                stop_after = False
            metrics.exec_ms = int((time.monotonic() - exec_started) * 1000)
            metrics.ready_ms = metrics.total_ms

            self.sfx.stop_ambient()
            if response:
                self.sfx.play("success" if success else "error", gain=settings.sfx_volume * 0.7)
                await self.speak(response, "neutral" if success else "calm")
                self.last_response = response
            elif success:
                self.sfx.play("success", gain=settings.sfx_volume * 0.7)
            else:
                self.sfx.play("error")

            if stop_after:
                self.stop()
        except asyncio.CancelledError:
            self.sfx.stop_ambient()
            raise
        except Exception as exc:
            log.exception("turn.process_failed", error=str(exc))
            success = False
            response = lines.error_line(str(exc)[:120])
            self.sfx.stop_ambient()
            self.sfx.play("error")
            await self.speak(response, "calm")
        finally:
            latency = metrics.ready_ms or metrics.total_ms
            self.bus.emit(
                EventType.RESULT,
                source="orchestrator",
                transcript=text,
                route=route,
                intent=intent_name,
                response=response,
                success=success,
                latency_ms=latency,
                trigger=trigger,
                tools=tools,
            )
            self.bus.emit(
                EventType.LATENCY,
                source="orchestrator",
                stt_ms=metrics.stt_ms,
                route_ms=metrics.route_ms,
                exec_ms=metrics.exec_ms,
                total_ms=latency,
            )
            self._spawn(
                self.memory.add_turn_async(
                    text, response, trigger=trigger, route=route, intent=intent_name,
                    success=success, latency_ms=latency, tools=tools,
                ),
                "memory-write",
            )
            if self.state.state is not State.SHUTDOWN:
                self._set(State.IDLE)
            if own_trace:
                structlog.contextvars.unbind_contextvars("trace_id")
        return intent_name

    async def _handle_intent(self, intent: Intent) -> tuple[bool, str, bool]:
        """
        Executa um comando rápido.

        Returns:
            `(sucesso, fala, encerrar_jarvis)`
        """
        # --- meta-comandos --------------------------------------------- #
        if intent.name == "cancel":
            return True, lines.pick(lines.CANCELLED), False
        if intent.name == "repeat":
            return True, self.last_response or "Ainda não disse nada, senhor.", False
        if intent.name == "quit":
            if await self.ask_confirmation(intent.confirm):
                return True, lines.pick(lines.GOODBYE), True
            return True, lines.pick(lines.CANCELLED), False

        # --- confirmação de ações destrutivas -------------------------- #
        if intent.confirm and not settings.allow_destructive:
            self.sfx.stop_ambient()
            approved = await self.ask_confirmation(intent.confirm, double=intent.double_confirm)
            if not approved:
                return True, lines.pick(lines.CANCELLED), False

        self._set(State.EXECUTING, detail=intent.name)
        if intent.say_before:
            self.say(intent.say_before, "confirm", restore=State.EXECUTING)
        result: ActionResult = await self.commands.execute(intent)
        self.bus.emit(
            EventType.TOOL_RESULT, source="commands", name=intent.name, ok=result.ok,
            preview=result.message[:240], seconds=0.0,
        )

        if not result.ok:
            return False, lines.error_line(result.message), False
        if intent.speak_result:
            return True, result.message, False
        # Ação simples: se já narramos antes ("Abrindo o Chrome."), o bipe basta.
        return True, "" if intent.say_before else lines.pick(lines.DONE), False

    async def _run_agent(self, text: str) -> AgentResult:
        """Delegação ao Sonnet com ferramentas."""
        if not settings.has_api_key:
            return AgentResult("", success=False, error="NO_API")
        self._set(State.THINKING)
        return await self.agent.run(text, on_interim=self._interim)

    @staticmethod
    def _agent_reply(result: AgentResult) -> str:
        if result.error == "NO_API":
            return lines.pick(lines.NO_API)
        if result.success:
            return result.text or lines.pick(lines.DONE)
        if result.text:
            return f"{result.text} {lines.error_line(result.error)}"
        return lines.error_line(result.error)

    # ------------------------------------------------------------------ #
    # Agendador
    # ------------------------------------------------------------------ #
    async def _wait_idle(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            await asyncio.sleep(0.3)

    async def _on_scheduled(self, item: ScheduledItem) -> None:
        """Timer/alarme/lembrete disparado."""
        await self._wait_idle()
        self.bus.emit(EventType.NOTICE, source="scheduler", text=f"{item.kind}: {item.text or item.due_iso}")
        if item.kind == "command":
            self._turn_task = asyncio.create_task(self.process_text(item.text, "schedule"), name="turn")
            return

        done = self.sfx.play("alarm", gain=settings.sfx_volume * 1.1)
        if done is not None:
            await asyncio.to_thread(done.wait, 4.0)
        if item.kind == "timer":
            phrase = lines.pick(lines.TIMER_DONE, what=item.text or "agora")
        elif item.kind == "alarm":
            now = datetime.now()
            phrase = lines.pick(lines.ALARM, time=f"{now.hour} e {now.minute:02d}")
        else:
            phrase = lines.pick(lines.REMINDER, what=item.text)
        await self.speak(phrase, "urgent")
        if self.state.state is State.SPEAKING:
            self._set(State.IDLE)

    # ------------------------------------------------------------------ #
    # Encerramento
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Pede o encerramento (seguro de chamar de qualquer task)."""
        self._stop.set()

    async def shutdown(self) -> None:
        """Libera todos os recursos na ordem inversa do boot."""
        if self.state.state is State.SHUTDOWN:
            return
        log.info("shutdown.start")
        self._set(State.SHUTDOWN)
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        for task in list(self._background):
            task.cancel()
        await self.scheduler.stop()
        await self.ipc.stop()
        if self.clap is not None:
            await self.clap.stop()
        if self.wake is not None:
            await self.wake.stop()
        if self.hotkey is not None:
            self.hotkey.stop()
        if self.mic is not None:
            self.mic.stop()
        await self.browser.stop()
        await self.agent.close()
        self.sfx.stop()
        self.memory.close()
        self.bus.close()
        log.info("shutdown.done")


__all__ = ["Orchestrator", "TurnMetrics", "parse_yes_no"]
