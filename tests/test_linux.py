"""Camada Linux (comandos do sistema simulados), hotkey e gatilho IPC."""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from audio.hotkey import to_pynput
from core.events import EventBus, EventType
from core.ipc import TriggerServer, send
from executor import linux


class FakeRun:
    """Responde a comandos por prefixo e registra o que foi chamado."""

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for prefix, (code, out) in self.responses.items():
            if joined.startswith(prefix):
                return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
        return None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    def install(responses: dict[str, tuple[int, str]], tools: set[str] | None = None) -> FakeRun:
        runner = FakeRun(responses)
        monkeypatch.setattr(linux, "run", runner)
        monkeypatch.setattr(linux, "have", lambda tool: tools is None or tool in tools)
        monkeypatch.setattr(linux, "_press", lambda *keys: False)
        return runner

    return install


def test_volume_pactl(fake: Any) -> None:
    runner = fake({
        "pactl get-sink-volume": (0, "Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50%"),
        "pactl set-sink-volume": (0, ""),
        "pactl set-sink-mute": (0, ""),
    })
    assert linux.get_volume() == 50.0
    ok, message, data = linux.set_volume(130)
    assert ok and data["value"] == 100.0 and "100" in message
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"] in runner.calls


def test_volume_amixer_fallback(fake: Any) -> None:
    fake({"amixer get Master": (0, "Mono: Playback 40 [62%] [-12.00dB] [on]")})
    assert linux.get_volume() == 62.0


def test_brightness(fake: Any) -> None:
    fake({"brightnessctl get": (0, "600\n"), "brightnessctl max": (0, "1200\n"), "brightnessctl set": (0, "")})
    assert linux.get_brightness() == 50
    assert linux.set_brightness(0)[2]["value"] == 1.0  # nunca apaga a tela


def test_list_and_focus_window(fake: Any) -> None:
    runner = fake({
        "wmctrl -l": (0, "0x03a00003  0 host Documento - LibreOffice Writer\n0x04200007 -1 host Painel\n"
                         "0x05000001  0 host Spotify Premium\n"),
        "wmctrl -i -a": (0, ""),
    })
    assert [title for _w, title in linux.list_windows()] == ["Documento - LibreOffice Writer", "Spotify Premium"]
    ok, message, _ = linux.window_action("focus", "spotify")
    assert ok and "Spotify" in message
    assert ["wmctrl", "-i", "-a", "0x05000001"] in runner.calls


def test_window_missing_tools_reports_reason(fake: Any) -> None:
    fake({}, tools=set())
    ok, message, _ = linux.window_action("close", "firefox")
    assert not ok and "wmctrl" in message


def test_media_playerctl(fake: Any) -> None:
    runner = fake({"playerctl": (0, "")})
    assert linux.media("next")[1] == "próxima faixa"
    assert runner.calls[-1] == ["playerctl", "next"]


def test_wifi_nmcli(fake: Any) -> None:
    runner = fake({"nmcli radio wifi": (0, "")})
    assert linux.wifi(False) == (True, "wi-fi desligado", {})
    assert runner.calls[-1] == ["nmcli", "radio", "wifi", "off"]


def test_locate_filters_to_home(fake: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux.Path, "home", staticmethod(lambda: tmp_path))
    hit = tmp_path / "contrato.pdf"
    hit.write_text("x")
    fake({"plocate": (0, f"/usr/share/doc/contrato.pdf\n{hit}\n")})
    assert linux.locate("contrato", 5) == [str(hit)]


def test_power_schedule_and_cancel(fake: Any) -> None:
    runner = fake({"shutdown -c": (1, "")})
    ok, message, _ = linux.schedule_power("poweroff", 60)
    assert ok and "60" in message
    assert linux.cancel_power() is True
    assert linux.cancel_power() is False
    assert ["systemctl", "poweroff"] not in runner.calls


@pytest.mark.parametrize(
    ("combo", "expected"),
    [("ctrl+alt+j", "<ctrl>+<alt>+j"), ("win+shift+F12", "<cmd>+<shift>+<f12>"), ("Control+J", "<ctrl>+j")],
)
def test_to_pynput(combo: str, expected: str) -> None:
    assert to_pynput(combo) == expected


async def test_ipc_activate_and_text() -> None:
    bus = EventBus()
    bus.bind_loop()
    queue = bus.subscribe()
    server = TriggerServer(bus, port=47999)
    assert await server.start()
    try:
        assert await asyncio.to_thread(send, "ACTIVATE", 47999) == "OK"
        assert (await asyncio.wait_for(queue.get(), 2)).type is EventType.HOTKEY
        assert await asyncio.to_thread(send, "TEXT que horas são", 47999) == "OK"
        events = [await asyncio.wait_for(queue.get(), 2) for _ in range(2)]
        request = next(e for e in events if e.type is EventType.TRANSCRIPT_REQUEST)
        assert request.get("text") == "que horas são"
        assert (await asyncio.to_thread(send, "BLA", 47999)).startswith("ERR")
    finally:
        await server.stop()
