"""
Agente do JARVIS: Sonnet com tool use e streaming de tokens.

Loop manual (não o tool runner beta) porque o orquestrador precisa de três
ganchos que acontecem *durante* o turno:
  * cada delta de texto vai para o dashboard em tempo real (`AGENT_DELTA`);
  * o texto que o modelo escreve antes de chamar ferramentas ("Vou abrir o
    VS Code...") é falado na hora (`on_interim`), dando a sensação de que o
    JARVIS narra o que está fazendo;
  * cada chamada de ferramenta publica `TOOL_CALL`/`TOOL_RESULT` (bipes e UI).

Também implementa `click_element`: print da tela → Sonnet vision → coords →
clique.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from config import settings
from core.events import EventBus, EventType
from executor.pc import ActionResult, PCController
from llm.prompts import agent_context, agent_system_prompt, vision_locate_prompt
from llm.tools import TOOLS, ToolExecutor, encode_image
from memory.store import MemoryStore

log = structlog.get_logger(__name__)

InterimFn = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AgentResult:
    """Resultado final de uma execução do agente."""

    text: str
    success: bool = True
    tools: list[str] = field(default_factory=list)
    turns: int = 0
    latency_s: float = 0.0
    error: str = ""

    @property
    def summary(self) -> str:
        return self.text


def _friendly_api_error(exc: Exception) -> str:
    """Traduz exceções do SDK para um motivo que dá para falar."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "a chave da API Anthropic é inválida"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "a chave da API não tem permissão para esse modelo"
    if isinstance(exc, anthropic.NotFoundError):
        return "o modelo configurado não existe"
    if isinstance(exc, anthropic.RateLimitError):
        return "atingi o limite de requisições da API, tente em instantes"
    if isinstance(exc, anthropic.APITimeoutError):
        return "a API demorou demais para responder"
    if isinstance(exc, anthropic.APIConnectionError):
        return "estou sem conexão com a API"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 529:
            return "os servidores da Anthropic estão instáveis"
        return f"a API recusou o pedido (erro {exc.status_code})"
    return str(exc)


class JarvisAgent:
    """Agente com ferramentas (Sonnet)."""

    def __init__(self, bus: EventBus, memory: MemoryStore, pc: PCController) -> None:
        self.bus = bus
        self.memory = memory
        self.pc = pc
        self.tools: ToolExecutor | None = None
        self._client = None

    # ------------------------------------------------------------------ #
    def bind_tools(self, tools: ToolExecutor) -> None:
        """Injeta o executor de ferramentas (criado pelo orquestrador)."""
        self.tools = tools

    @property
    def client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=2, timeout=90.0)
        return self._client

    def _system(self) -> list[dict[str, Any]]:
        """System em dois blocos: estável (com cache) + contexto volátil."""
        facts: dict[str, str] = {}
        try:
            facts = self.memory.all_facts()
        except Exception as exc:  # pragma: no cover - sqlite corrompido
            log.warning("agent.facts_failed", error=str(exc))
        return [
            {"type": "text", "text": agent_system_prompt(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": agent_context(facts)},
        ]

    # ------------------------------------------------------------------ #
    async def run(self, text: str, on_interim: InterimFn | None = None) -> AgentResult:
        """
        Executa o pedido com tool use até o modelo encerrar o turno.

        Args:
            text: transcrição do usuário.
            on_interim: chamado com o texto que o modelo escreve antes de
                usar ferramentas (para ser falado imediatamente).
        """
        started = time.monotonic()
        if not settings.has_api_key:
            return AgentResult(
                "", success=False, error="a chave da API Anthropic não está configurada no arquivo .env"
            )
        if self.tools is None:
            return AgentResult("", success=False, error="ferramentas do agente não inicializadas")

        messages: list[dict[str, Any]] = [*self.memory.conversation_context(), {"role": "user", "content": text}]
        used_tools: list[str] = []
        final_text = ""

        for turn in range(1, settings.agent_max_turns + 1):
            try:
                message = await self._stream_turn(messages)
            except Exception as exc:
                reason = _friendly_api_error(exc)
                log.error("agent.api_error", error=str(exc), turn=turn)
                return AgentResult(
                    final_text, success=False, tools=used_tools, turns=turn,
                    latency_s=time.monotonic() - started, error=reason,
                )

            messages.append({"role": "assistant", "content": message.content})
            texts = [block.text for block in message.content if block.type == "text"]
            tool_uses = [block for block in message.content if block.type == "tool_use"]

            if message.stop_reason == "refusal":
                return AgentResult(
                    "", success=False, tools=used_tools, turns=turn,
                    latency_s=time.monotonic() - started, error="o modelo recusou esse pedido",
                )

            if message.stop_reason == "tool_use" and tool_uses:
                interim = " ".join(part.strip() for part in texts if part.strip())
                if interim and on_interim is not None:
                    await on_interim(interim)
                results = []
                for block in tool_uses:
                    used_tools.append(block.name)
                    results.append(await self._run_tool(block.id, block.name, block.input))
                messages.append({"role": "user", "content": results})
                continue

            final_text = " ".join(part.strip() for part in texts if part.strip())
            if message.stop_reason == "max_tokens" and tool_uses:
                return AgentResult(
                    final_text, success=False, tools=used_tools, turns=turn,
                    latency_s=time.monotonic() - started, error="a resposta foi cortada no meio",
                )
            return AgentResult(
                final_text, success=True, tools=used_tools, turns=turn, latency_s=time.monotonic() - started
            )

        return AgentResult(
            final_text, success=False, tools=used_tools, turns=settings.agent_max_turns,
            latency_s=time.monotonic() - started, error="a tarefa excedeu o número máximo de passos",
        )

    async def _stream_turn(self, messages: list[dict[str, Any]]):
        """Uma chamada à API com streaming; devolve a `Message` final."""
        async with self.client.messages.stream(
            model=settings.model_agent,
            max_tokens=settings.max_tokens_agent,
            system=self._system(),
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    self.bus.emit(EventType.AGENT_DELTA, source="agent", text=event.text)
            return await stream.get_final_message()

    async def _run_tool(self, tool_id: str, name: str, args: Any) -> dict[str, Any]:
        """Executa uma ferramenta e monta o bloco `tool_result`."""
        assert self.tools is not None
        params = args if isinstance(args, dict) else {}
        self.bus.emit(EventType.TOOL_CALL, source="agent", name=name, args=params)
        started = time.monotonic()
        content, is_error = await self.tools.execute(name, params)
        elapsed = time.monotonic() - started
        preview = content if isinstance(content, str) else json.dumps(content[0], ensure_ascii=False)
        log.info("agent.tool", tool=name, error=is_error, seconds=round(elapsed, 2))
        self.bus.emit(
            EventType.TOOL_RESULT, source="agent", name=name, ok=not is_error,
            preview=preview[:240], seconds=elapsed,
        )
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        if is_error:
            block["is_error"] = True
        return block

    # ------------------------------------------------------------------ #
    # Visão: click_element
    # ------------------------------------------------------------------ #
    async def locate_on_screen(self, description: str) -> ActionResult:
        """Encontra um elemento na tela e devolve suas coordenadas reais."""
        if not settings.has_api_key:
            return ActionResult.fail("a chave da API Anthropic não está configurada")
        shot = await asyncio.to_thread(self.pc.screenshot)
        if not shot.ok:
            return shot

        from PIL import Image

        with Image.open(shot.data["path"]) as image:
            shot_w, shot_h = image.size
            data, scale = await asyncio.to_thread(encode_image, image.copy())
        img_w, img_h = int(shot_w / scale), int(shot_h / scale)

        try:
            response = await self.client.messages.create(
                model=settings.model_vision,
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                            {"type": "text", "text": vision_locate_prompt(description, img_w, img_h)},
                        ],
                    }
                ],
            )
        except Exception as exc:
            return ActionResult.fail(_friendly_api_error(exc))

        raw = "".join(block.text for block in response.content if block.type == "text")
        found = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not found:
            return ActionResult.fail("a análise da tela não devolveu coordenadas")
        try:
            answer = json.loads(found.group(0))
        except json.JSONDecodeError:
            return ActionResult.fail("a análise da tela devolveu uma resposta inválida")
        if not answer.get("found"):
            return ActionResult.fail(f"não encontrei na tela: {answer.get('reason') or description}")

        # imagem reduzida -> captura -> coordenadas lógicas do pyautogui
        screen_w, screen_h = self.pc.screen_size()
        x = float(answer["x"]) * scale * (screen_w / shot_w)
        y = float(answer["y"]) * scale * (screen_h / shot_h)
        x = int(min(max(0, x), screen_w - 1))
        y = int(min(max(0, y), screen_h - 1))
        return ActionResult(
            True,
            f"encontrei {answer.get('label') or description}",
            {"x": x, "y": y, "confidence": answer.get("confidence")},
        )

    async def click_element(self, description: str, button: str = "left", double: bool = False) -> ActionResult:
        """Localiza visualmente e clica."""
        located = await self.locate_on_screen(description)
        if not located.ok:
            return located
        x, y = located.data["x"], located.data["y"]
        clicked = await asyncio.to_thread(self.pc.click, x, y, button, 2 if double else 1)
        if not clicked.ok:
            return clicked
        return ActionResult(True, f"cliquei em {located.message.removeprefix('encontrei ')}", located.data)

    async def close(self) -> None:
        """Fecha o cliente HTTP."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None


__all__ = ["AgentResult", "JarvisAgent"]
