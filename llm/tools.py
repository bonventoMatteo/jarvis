"""
Ferramentas do agente (Sonnet): schemas JSON + execução.

`TOOLS` é a lista enviada à API. `ToolExecutor.execute()` recebe o nome e o
input de um bloco `tool_use` e devolve `(conteúdo, is_error)` pronto para um
bloco `tool_result`. Toda falha vira `is_error=True` com o motivo real — o
agente é instruído a repassar esse motivo ao usuário, nunca a inventar.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog

from config import settings
from core.scheduler import Scheduler
from executor import files as files_mod
from executor import media as media_mod
from executor.browser import BrowserController
from executor.pc import ActionResult, PCController
from memory.store import MemoryStore

log = structlog.get_logger(__name__)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "execute_shell",
        "description": (
            "Executa um comando no shell do Windows (cmd) e devolve stdout/stderr/código. "
            "Para PowerShell use `powershell -NoProfile -Command \"...\"`. Com admin=true o comando "
            "abre elevado (UAC) e a saída NÃO é capturada. Prefira ferramentas específicas quando existirem."
        ),
        "input_schema": _schema(
            {
                "cmd": {"type": "string", "description": "Comando completo."},
                "admin": {"type": "boolean", "description": "Executar elevado (UAC).", "default": False},
                "cwd": {"type": "string", "description": "Diretório de trabalho (opcional).", "default": ""},
            },
            ["cmd"],
        ),
    },
    {
        "name": "open_application",
        "description": "Abre um aplicativo pelo nome falado (chrome, word, spotify, vscode...) ou caminho completo de executável/arquivo.",
        "input_schema": _schema({"name_or_path": {"type": "string"}}, ["name_or_path"]),
    },
    {
        "name": "close_application",
        "description": "Fecha todos os processos de um aplicativo pelo nome.",
        "input_schema": _schema(
            {
                "name": {"type": "string"},
                "force": {"type": "boolean", "description": "Matar à força (perde trabalho não salvo).", "default": False},
            },
            ["name"],
        ),
    },
    {
        "name": "control_window",
        "description": "Controla uma janela pelo trecho do título. Sem target_title age na janela em foco.",
        "input_schema": _schema(
            {
                "action": {"type": "string", "enum": ["minimize", "maximize", "close", "focus", "restore"]},
                "target_title": {"type": "string", "default": ""},
            },
            ["action"],
        ),
    },
    {
        "name": "list_windows",
        "description": "Lista os títulos das janelas visíveis abertas.",
        "input_schema": _schema({}),
    },
    {
        "name": "type_text",
        "description": "Digita um texto na janela em foco, como se fosse o teclado.",
        "input_schema": _schema(
            {
                "text": {"type": "string"},
                "interval_ms": {"type": "integer", "minimum": 0, "maximum": 200, "default": 12},
            },
            ["text"],
        ),
    },
    {
        "name": "send_hotkey",
        "description": "Pressiona uma combinação de teclas. Ex.: [\"ctrl\",\"shift\",\"esc\"], [\"win\",\"r\"], [\"enter\"].",
        "input_schema": _schema(
            {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
            ["keys"],
        ),
    },
    {
        "name": "click_at",
        "description": "Clica numa coordenada absoluta da tela (pixels).",
        "input_schema": _schema(
            {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
            },
            ["x", "y"],
        ),
    },
    {
        "name": "click_element",
        "description": (
            "Tira um print da tela, localiza visualmente o elemento descrito (botão, link, ícone, campo) "
            "e clica nele. Use descrições concretas: 'botão azul Enviar no canto inferior direito'."
        ),
        "input_schema": _schema(
            {
                "description": {"type": "string"},
                "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
                "double": {"type": "boolean", "default": False},
            },
            ["description"],
        ),
    },
    {
        "name": "read_file",
        "description": "Lê um arquivo de texto do disco.",
        "input_schema": _schema(
            {"path": {"type": "string"}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 300}},
            ["path"],
        ),
    },
    {
        "name": "write_file",
        "description": "Grava (write) ou acrescenta (append) texto num arquivo. Cria as pastas necessárias.",
        "input_schema": _schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["write", "append"], "default": "write"},
            },
            ["path", "content"],
        ),
    },
    {
        "name": "search_files",
        "description": "Busca arquivos pelo nome. root pode ser 'downloads', 'documentos', 'desktop' ou um caminho; vazio usa o índice do Windows.",
        "input_schema": _schema(
            {
                "query": {"type": "string"},
                "root": {"type": "string", "default": ""},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 15},
            },
            ["query"],
        ),
    },
    {
        "name": "list_folder",
        "description": "Lista o conteúdo de uma pasta (nome conhecido ou caminho).",
        "input_schema": _schema({"folder": {"type": "string"}}, ["folder"]),
    },
    {
        "name": "browser_navigate",
        "description": "Abre uma URL no navegador automatizado (Chromium controlado pelo Jarvis).",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    {
        "name": "browser_search_and_extract",
        "description": (
            "Pesquisa na web, abre o primeiro resultado relevante e devolve o texto da página. "
            "extract_prompt descreve o que você procura (para seu próprio uso ao ler o resultado)."
        ),
        "input_schema": _schema(
            {"query": {"type": "string"}, "extract_prompt": {"type": "string", "default": ""}},
            ["query"],
        ),
    },
    {
        "name": "browser_read_page",
        "description": "Lê o texto visível da página atual do navegador automatizado (ou de um seletor CSS).",
        "input_schema": _schema({"selector": {"type": "string", "default": "body"}}),
    },
    {
        "name": "browser_action",
        "description": "Interage com a página atual: click, fill, press, select, scroll, wait, back, reload, screenshot.",
        "input_schema": _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "press", "select", "scroll", "wait", "back", "reload", "screenshot"],
                },
                "selector": {"type": "string", "default": ""},
                "value": {"type": "string", "default": ""},
            },
            ["action"],
        ),
    },
    {
        "name": "take_screenshot",
        "description": "Captura a tela (ou região [x, y, largura, altura]) e devolve a imagem para você analisar.",
        "input_schema": _schema(
            {
                "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
                "save_path": {"type": "string", "default": ""},
            }
        ),
    },
    {
        "name": "get_system_info",
        "description": "CPU, memória, disco, rede, bateria e uptime do computador.",
        "input_schema": _schema({}),
    },
    {
        "name": "media_control",
        "description": "Controla mídia e hardware: play_pause, next, previous, stop, volume (0-100), mute, unmute, brightness (0-100).",
        "input_schema": _schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["play_pause", "next", "previous", "stop", "volume", "mute", "unmute", "brightness"],
                },
                "value": {"type": "number", "description": "Nível 0-100 para volume/brightness."},
            },
            ["action"],
        ),
    },
    {
        "name": "schedule_task",
        "description": (
            "Agenda algo para um horário. mode='remind' fala o texto no horário; mode='execute' executa o "
            "texto como se o usuário tivesse falado o comando naquele momento (ex.: 'abrir o spotify')."
        ),
        "input_schema": _schema(
            {
                "when_iso": {"type": "string", "description": "Data/hora local ISO 8601, ex. 2026-09-26T18:30:00"},
                "what": {"type": "string"},
                "mode": {"type": "string", "enum": ["remind", "execute"], "default": "remind"},
            },
            ["when_iso", "what"],
        ),
    },
    {
        "name": "remember_fact",
        "description": "Guarda um fato durável sobre o usuário (preferências, nomes, caminhos de projetos).",
        "input_schema": _schema({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    },
    {
        "name": "speak",
        "description": "Fala uma frase curta AGORA, no meio de uma tarefa longa. Não use para a resposta final.",
        "input_schema": _schema(
            {
                "text": {"type": "string"},
                "emotion": {"type": "string", "enum": ["neutral", "calm", "urgent", "confirm"], "default": "neutral"},
            },
            ["text"],
        ),
    },
    {
        "name": "ask_confirmation",
        "description": "Pergunta algo de sim/não em voz alta e escuta a resposta. Obrigatório antes de ações destrutivas.",
        "input_schema": _schema({"question": {"type": "string"}}, ["question"]),
    },
]

TOOL_NAMES: frozenset[str] = frozenset(tool["name"] for tool in TOOLS)

#: Comandos de shell que exigem confirmação explícita antes de rodar.
_DANGEROUS_SHELL = re.compile(
    r"\b(rm\s+-r|rmdir\s+/s|rd\s+/s|del\s+/[sfq]|erase\s|format\s+[a-z]:|remove-item\b.*-recurse|"
    r"diskpart|reg\s+delete|shutdown\b|restart-computer|stop-computer|bcdedit|cipher\s+/w|"
    r"takeown|icacls\b.*\/reset|clear-disk|initialize-disk)",
    re.IGNORECASE,
)

SpeakFn = Callable[[str, str], Awaitable[None]]
ConfirmFn = Callable[[str], Awaitable[bool]]
ClickElementFn = Callable[[str, str, bool], Awaitable[ActionResult]]


def _result_json(result: ActionResult) -> str:
    """Serializa um `ActionResult` para o conteúdo de `tool_result`."""
    payload: dict[str, Any] = {"ok": result.ok, "message": result.message}
    if result.data:
        payload["data"] = result.data
    return json.dumps(payload, ensure_ascii=False, default=str)[:12000]


def encode_image(image: Any, max_side: int = 1568, quality: int = 80) -> tuple[str, float]:
    """
    Reduz uma imagem PIL para caber no limite da API e devolve (base64 JPEG,
    fator de escala original/reduzida).
    """
    width, height = image.size
    scale = max(width, height) / max_side if max(width, height) > max_side else 1.0
    if scale > 1.0:
        image = image.resize((int(width / scale), int(height / scale)))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii"), scale


class ToolExecutor:
    """Despacha chamadas de ferramenta para o executor real."""

    def __init__(
        self,
        pc: PCController,
        browser: BrowserController,
        scheduler: Scheduler,
        memory: MemoryStore,
        speak: SpeakFn,
        confirm: ConfirmFn,
        click_element: ClickElementFn,
    ) -> None:
        self.pc = pc
        self.browser = browser
        self.scheduler = scheduler
        self.memory = memory
        self._speak = speak
        self._confirm = confirm
        self._click_element = click_element

    async def execute(self, name: str, args: dict[str, Any]) -> tuple[str | list[dict[str, Any]], bool]:
        """
        Executa uma ferramenta.

        Returns:
            `(conteúdo, is_error)` — conteúdo é string JSON ou lista de blocos
            (texto + imagem) para `take_screenshot`.
        """
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return json.dumps({"ok": False, "message": f"ferramenta desconhecida: {name}"}), True
        try:
            output = await handler(**args)
        except TypeError as exc:
            return json.dumps({"ok": False, "message": f"argumentos inválidos para {name}: {exc}"}), True
        except Exception as exc:
            log.error("tools.failed", tool=name, error=str(exc))
            return json.dumps({"ok": False, "message": f"{name} falhou: {exc}"}, ensure_ascii=False), True

        if isinstance(output, ActionResult):
            return _result_json(output), not output.ok
        return output

    # ------------------------------------------------------------------ #
    async def _t_execute_shell(self, cmd: str, admin: bool = False, cwd: str = "") -> ActionResult:
        if _DANGEROUS_SHELL.search(cmd) and not settings.allow_destructive:
            approved = await self._confirm(f"O comando pode ser destrutivo: {cmd[:80]}. Executo?")
            if not approved:
                return ActionResult.fail("o usuário não autorizou o comando")
        return await asyncio.to_thread(self.pc.run_shell, cmd, admin, cwd)

    async def _t_open_application(self, name_or_path: str) -> ActionResult:
        return await asyncio.to_thread(self.pc.open_app, name_or_path)

    async def _t_close_application(self, name: str, force: bool = False) -> ActionResult:
        return await asyncio.to_thread(self.pc.close_app, name, force)

    async def _t_control_window(self, action: str, target_title: str = "") -> ActionResult:
        return await asyncio.to_thread(self.pc.control_window, action, target_title)

    async def _t_list_windows(self) -> ActionResult:
        return await asyncio.to_thread(self.pc.list_windows)

    async def _t_type_text(self, text: str, interval_ms: int = 12) -> ActionResult:
        return await asyncio.to_thread(self.pc.type_text, text, interval_ms)

    async def _t_send_hotkey(self, keys: list[str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.send_hotkey, keys)

    async def _t_click_at(self, x: int, y: int, button: str = "left", clicks: int = 1) -> ActionResult:
        return await asyncio.to_thread(self.pc.click, x, y, button, clicks)

    async def _t_click_element(self, description: str, button: str = "left", double: bool = False) -> ActionResult:
        return await self._click_element(description, button, double)

    async def _t_read_file(self, path: str, max_lines: int = 300) -> ActionResult:
        result = await asyncio.to_thread(files_mod.read_file, path, max_lines)
        return ActionResult(result.ok, result.message, {"content": result.content} if result.ok else {})

    async def _t_write_file(self, path: str, content: str, mode: str = "write") -> ActionResult:
        result = await asyncio.to_thread(files_mod.write_file, path, content, mode)
        return ActionResult(result.ok, result.message, {"paths": result.paths})

    async def _t_search_files(self, query: str, root: str = "", max_results: int = 15) -> ActionResult:
        return await asyncio.to_thread(self.pc.search_files, query, root, max_results)

    async def _t_list_folder(self, folder: str) -> ActionResult:
        result = await asyncio.to_thread(files_mod.list_folder, folder)
        return ActionResult(result.ok, result.message, {"entries": result.content})

    async def _t_browser_navigate(self, url: str) -> ActionResult:
        result = await self.browser.navigate(url)
        return ActionResult(result.ok, result.message, {"url": result.url, **result.data})

    async def _t_browser_search_and_extract(self, query: str, extract_prompt: str = "") -> ActionResult:
        result = await self.browser.search_and_extract(query, extract_prompt)
        return ActionResult(result.ok, result.message, {"url": result.url, "text": result.text})

    async def _t_browser_read_page(self, selector: str = "body") -> ActionResult:
        result = await self.browser.get_text(selector)
        return ActionResult(result.ok, result.message, {"url": result.url, "text": result.text})

    async def _t_browser_action(self, action: str, selector: str = "", value: str = "") -> ActionResult:
        result = await self.browser.action(action, selector, value)
        return ActionResult(result.ok, result.message, {"url": result.url, **result.data})

    async def _t_take_screenshot(
        self, region: list[int] | None = None, save_path: str = ""
    ) -> tuple[list[dict[str, Any]], bool]:
        result = await asyncio.to_thread(
            self.pc.screenshot, tuple(region) if region else None, save_path  # type: ignore[arg-type]
        )
        if not result.ok:
            return [{"type": "text", "text": _result_json(result)}], True
        from PIL import Image

        with Image.open(result.data["path"]) as image:
            data, scale = await asyncio.to_thread(encode_image, image.copy())
        note = dict(result.data, scale=round(scale, 4))
        note["hint"] = "coordenadas na imagem * scale = coordenadas reais da tela"
        return [
            {"type": "text", "text": json.dumps({"ok": True, "message": "captura feita", "data": note}, ensure_ascii=False)},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        ], False

    async def _t_get_system_info(self) -> ActionResult:
        return await asyncio.to_thread(self.pc.system_info)

    async def _t_media_control(self, action: str, value: float | None = None) -> ActionResult:
        match action:
            case "play_pause":
                media = await asyncio.to_thread(media_mod.play_pause)
            case "next":
                media = await asyncio.to_thread(media_mod.next_track)
            case "previous":
                media = await asyncio.to_thread(media_mod.previous_track)
            case "stop":
                media = await asyncio.to_thread(media_mod.stop_media)
            case "mute":
                media = await asyncio.to_thread(media_mod.set_mute, True)
            case "unmute":
                media = await asyncio.to_thread(media_mod.set_mute, False)
            case "volume":
                if value is None:
                    media = await asyncio.to_thread(media_mod.get_volume)
                else:
                    media = await asyncio.to_thread(media_mod.set_volume, value)
            case "brightness":
                if value is None:
                    media = await asyncio.to_thread(media_mod.get_brightness)
                else:
                    media = await asyncio.to_thread(media_mod.set_brightness, value)
            case _:
                return ActionResult.fail(f"ação de mídia desconhecida: {action}")
        return ActionResult(media.ok, media.message, {"value": media.value})

    async def _t_schedule_task(self, when_iso: str, what: str, mode: str = "remind") -> ActionResult:
        try:
            when = datetime.fromisoformat(when_iso)
        except ValueError:
            return ActionResult.fail(f"data inválida: {when_iso}")
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        if when <= datetime.now():
            return ActionResult.fail("o horário informado já passou")
        item = self.scheduler.add("command" if mode == "execute" else "reminder", when, what)
        return ActionResult(True, f"agendado para {when:%d/%m %H:%M}", {"id": item.id, "due": item.due_iso})

    async def _t_remember_fact(self, key: str, value: str) -> ActionResult:
        await asyncio.to_thread(self.memory.remember, key, value)
        return ActionResult(True, f"guardei {key}")

    async def _t_speak(self, text: str, emotion: str = "neutral") -> ActionResult:
        await self._speak(text, emotion)
        return ActionResult(True, "falado")

    async def _t_ask_confirmation(self, question: str) -> ActionResult:
        approved = await self._confirm(question)
        return ActionResult(True, "o usuário respondeu SIM" if approved else "o usuário respondeu NÃO", {"approved": approved})


__all__ = ["TOOLS", "TOOL_NAMES", "ToolExecutor", "encode_image"]
