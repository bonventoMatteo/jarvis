"""Loop do agente (cliente falso) e executor de comandos rápidos (PC falso)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config import settings
from core.events import EventBus, EventType
from core.scheduler import Scheduler
from executor.commands import FastCommandExecutor
from executor.pc import ActionResult
from llm.agent import JarvisAgent
from llm.router import Router
from llm.tools import TOOL_NAMES, TOOLS, ToolExecutor
from memory.store import MemoryStore


# --------------------------------------------------------------------------- #
# Dublês
# --------------------------------------------------------------------------- #
class FakePC:
    """Registra chamadas em vez de mexer no sistema."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):
        def method(*args: Any) -> ActionResult:
            self.calls.append((name, args))
            if name == "volume":
                percent, delta = args
                return ActionResult(True, f"volume em {round(percent if percent is not None else 50 + delta)} por cento")
            return ActionResult(True, f"{name} ok", {"args": list(args)})

        return method


class FakeStream:
    def __init__(self, message: Any, text: str) -> None:
        self._message = message
        self._text = text

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self):
        async def gen():
            if self._text:
                yield SimpleNamespace(type="text", text=self._text)

        return gen()

    async def get_final_message(self) -> Any:
        return self._message


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.requests.append(dict(kwargs, messages=list(kwargs["messages"])))
        message = self.script.pop(0)
        text = "".join(block.text for block in message.content if block.type == "text")
        return FakeStream(message, text)


def _text(value: str) -> Any:
    return SimpleNamespace(type="text", text=value)


def _tool(tool_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)


def _msg(stop: str, *content: Any) -> Any:
    return SimpleNamespace(stop_reason=stop, content=list(content))


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(db_path=tmp_path / "j.db", prefs_path=tmp_path / "prefs.json")
    store.connect()
    yield store
    store.close()


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def test_tool_schemas_are_valid() -> None:
    required = {
        "execute_shell", "open_application", "control_window", "type_text", "send_hotkey", "click_at",
        "click_element", "read_file", "write_file", "search_files", "browser_navigate",
        "browser_search_and_extract", "browser_action", "browser_read_page", "take_screenshot",
        "get_system_info", "schedule_task", "speak", "ask_confirmation",
    }
    assert required <= TOOL_NAMES
    for tool in TOOLS:
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]


# --------------------------------------------------------------------------- #
# Agente
# --------------------------------------------------------------------------- #
async def test_agent_runs_tool_loop(memory: MemoryStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-" + "x" * 40)
    bus = EventBus()
    bus.bind_loop()
    events = bus.subscribe(maxsize=100)
    pc = FakePC()
    spoken: list[str] = []

    async def speak(text: str, _emotion: str) -> None:
        spoken.append(text)

    async def confirm(_q: str) -> bool:
        return True

    agent = JarvisAgent(bus, memory, pc)  # type: ignore[arg-type]
    target = tmp_path / "nota.txt"
    agent.bind_tools(
        ToolExecutor(pc, None, Scheduler(path=tmp_path / "s.json"), memory, speak, confirm, agent.click_element)  # type: ignore[arg-type]
    )
    script = [
        _msg("tool_use", _text("Abrindo o bloco de notas."), _tool("t1", "open_application", {"name_or_path": "notepad"})),
        _msg("tool_use", _tool("t2", "write_file", {"path": str(target), "content": "olá"})),
        _msg("end_turn", _text("Feito, senhor.")),
    ]
    fake = FakeMessages(script)
    agent._client = SimpleNamespace(messages=fake, close=None)

    interim: list[str] = []

    async def on_interim(text: str) -> None:
        interim.append(text)

    result = await agent.run("abre o bloco de notas e escreve olá em nota.txt", on_interim=on_interim)

    assert result.success and result.text == "Feito, senhor."
    assert result.tools == ["open_application", "write_file"]
    assert interim == ["Abrindo o bloco de notas."]
    assert ("open_app", ("notepad",)) in pc.calls
    assert target.read_text(encoding="utf-8") == "olá"

    # O 2º request carrega o tool_result do 1º, no mesmo turno do usuário.
    second = fake.requests[1]["messages"]
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "t1"
    assert json.loads(second[-1]["content"][0]["content"])["ok"] is True
    # System estável com cache + contexto volátil.
    system = fake.requests[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"} and "Data e hora" in system[1]["text"]

    kinds = [events.get_nowait().type for _ in range(events.qsize())]
    assert EventType.AGENT_DELTA in kinds and kinds.count(EventType.TOOL_CALL) == 2


async def test_agent_reports_tool_error(memory: MemoryStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-" + "x" * 40)
    bus = EventBus()
    bus.bind_loop()
    pc = FakePC()

    async def noop(*_: Any) -> Any:
        return True

    agent = JarvisAgent(bus, memory, pc)  # type: ignore[arg-type]
    agent.bind_tools(ToolExecutor(pc, None, Scheduler(path=tmp_path / "s.json"), memory, noop, noop, agent.click_element))  # type: ignore[arg-type]
    fake = FakeMessages(
        [
            _msg("tool_use", _tool("t1", "read_file", {"path": str(tmp_path / "nao_existe.txt")})),
            _msg("end_turn", _text("Não encontrei o arquivo, senhor.")),
        ]
    )
    agent._client = SimpleNamespace(messages=fake)
    result = await agent.run("lê o arquivo")
    block = fake.requests[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "não encontrado" in json.loads(block["content"])["message"]
    assert result.success


async def test_agent_without_key(memory: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    agent = JarvisAgent(EventBus(), memory, FakePC())  # type: ignore[arg-type]
    result = await agent.run("qualquer coisa")
    assert not result.success and "chave" in result.error


# --------------------------------------------------------------------------- #
# Comandos rápidos
# --------------------------------------------------------------------------- #
@pytest.fixture
def executor(tmp_path: Path, memory: MemoryStore) -> tuple[FastCommandExecutor, FakePC, Scheduler]:
    pc = FakePC()
    scheduler = Scheduler(path=tmp_path / "s.json")
    return FastCommandExecutor(pc, scheduler, memory, explorer_folder=lambda: tmp_path), pc, scheduler  # type: ignore[arg-type]


async def _run(executor: FastCommandExecutor, phrase: str) -> ActionResult:
    intent = Router().match(phrase)
    assert intent is not None, phrase
    return await executor.execute(intent)


async def test_command_timer(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, _pc, scheduler = executor
    result = await _run(runner, "timer de 5 minutos")
    assert result.ok and result.message == "timer de 5 minutos iniciado"
    assert scheduler.pending()[0].kind == "timer"


async def test_command_reminder(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, _pc, scheduler = executor
    result = await _run(runner, "me lembra de beber água em 10 minutos")
    assert result.ok
    assert scheduler.pending()[0].text == "beber água"


async def test_command_volume_words(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, pc, _ = executor
    result = await _run(runner, "volume no máximo")
    assert result.ok and pc.calls[-1] == ("volume", (100.0, None))
    await _run(runner, "volume trinta")
    assert pc.calls[-1] == ("volume", (30.0, None))


async def test_command_google(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, pc, _ = executor
    await _run(runner, "pesquisa no google previsão do tempo")
    name, args = pc.calls[-1]
    assert name == "open_url" and "q=previs%C3%A3o+do+tempo" in args[0]


async def test_command_create_folder_here(executor: tuple[FastCommandExecutor, FakePC, Scheduler], tmp_path: Path) -> None:
    runner, _pc, _ = executor
    result = await _run(runner, "criar pasta Projetos aqui")
    assert result.ok and (tmp_path / "Projetos").is_dir()


async def test_command_browser_keys(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, pc, _ = executor
    await _run(runner, "nova aba")
    assert pc.calls[-1] == ("send_hotkey", (["ctrl", "t"],))


async def test_every_fast_command_has_handler(executor: tuple[FastCommandExecutor, FakePC, Scheduler]) -> None:
    runner, _pc, _ = executor
    from llm.router import FAST_COMMANDS

    meta = {"cancel", "repeat", "quit"}
    missing = [c.name for c in FAST_COMMANDS if c.name not in meta and not hasattr(runner, f"_do_{c.name}")]
    assert missing == []
