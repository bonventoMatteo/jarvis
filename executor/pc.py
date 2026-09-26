"""
`PCController` — a camada que realmente mexe no Windows.

Agrupa teclado/mouse (pyautogui), janelas (win32gui/pywinauto), energia,
screenshots, área de transferência, informações do sistema e timers.
Todos os métodos são síncronos e devolvem `ActionResult`; o orquestrador
os executa em thread (`asyncio.to_thread`).
"""
from __future__ import annotations

import ctypes
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from config import IS_LINUX, IS_WINDOWS, settings
from executor import apps as apps_mod
from executor import files as files_mod
from executor import linux as linux_mod
from executor import media as media_mod

log = structlog.get_logger(__name__)

_WEEKDAYS = (
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
)
_MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


@dataclass(slots=True)
class ActionResult:
    """Resultado padronizado de qualquer ação do executor."""

    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        """Texto curto para o TTS / para devolver ao modelo."""
        return self.message

    @classmethod
    def fail(cls, reason: str) -> ActionResult:
        return cls(False, reason)

    @classmethod
    def of(cls, result: tuple[bool, str, dict[str, Any]]) -> ActionResult:
        """Converte o `(ok, mensagem, dados)` de `executor.linux`."""
        ok, message, data = result
        return cls(ok, message, dict(data))


@dataclass(slots=True)
class Timer:
    """Um timer agendado."""

    name: str
    fires_at: datetime
    handle: threading.Timer

    @property
    def remaining_s(self) -> float:
        return max(0.0, (self.fires_at - datetime.now()).total_seconds())


class PCController:
    """Executor de ações no PC."""

    def __init__(self, on_timer: Callable[[str], None] | None = None) -> None:
        self.on_timer = on_timer
        self.timers: dict[str, Timer] = {}
        self._pyautogui = None
        settings.screenshot_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # pyautogui (import preguiçoso: ele abre conexão com o display)
    # ------------------------------------------------------------------ #
    @property
    def gui(self):
        if self._pyautogui is None:
            import pyautogui

            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0.02
            self._pyautogui = pyautogui
        return self._pyautogui

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #
    def run_shell(self, cmd: str, admin: bool = False, cwd: str = "") -> ActionResult:
        """
        Executa um comando no shell.

        Com `admin=True` dispara um processo elevado via `Start-Process -Verb
        RunAs` (o Windows mostra o UAC; a saída não é capturada).
        """
        if not settings.allow_shell:
            return ActionResult.fail("execução de comandos está desabilitada na configuração")
        if not cmd.strip():
            return ActionResult.fail("comando vazio")

        workdir = cwd or None
        if workdir and not Path(workdir).exists():
            return ActionResult.fail(f"diretório inexistente: {workdir}")

        if admin and IS_WINDOWS:
            escaped = cmd.replace("'", "''")
            launcher = (
                f"Start-Process powershell -Verb RunAs -ArgumentList "
                f"'-NoProfile','-NoExit','-Command','{escaped}'"
            )
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", launcher],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return ActionResult(True, "comando enviado com elevação (confirme o UAC)")
            except Exception as exc:
                return ActionResult.fail(f"falha ao elevar: {exc}")

        if admin and IS_LINUX:
            # pkexec mostra o diálogo gráfico de senha do polkit.
            cmd = f"pkexec sh -c {shlex.quote(cmd)}"

        try:
            completed = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=settings.shell_timeout_s,
                cwd=workdir,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return ActionResult.fail(f"o comando excedeu {settings.shell_timeout_s} segundos")
        except Exception as exc:
            return ActionResult.fail(f"falha ao executar: {exc}")

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        ok = completed.returncode == 0
        log.info("pc.shell", cmd=cmd[:120], returncode=completed.returncode)
        return ActionResult(
            ok,
            "comando executado" if ok else f"comando falhou (código {completed.returncode})",
            {"returncode": completed.returncode, "stdout": stdout[:4000], "stderr": stderr[:2000]},
        )

    # ------------------------------------------------------------------ #
    # Teclado e mouse
    # ------------------------------------------------------------------ #
    def type_text(self, text: str, interval_ms: int = 12) -> ActionResult:
        """Digita um texto na janela em foco."""
        if not text:
            return ActionResult.fail("texto vazio")
        try:
            self.gui.write(text, interval=max(0.0, interval_ms / 1000.0))
            return ActionResult(True, f"digitei {len(text)} caracteres")
        except Exception as exc:
            return ActionResult.fail(f"não consegui digitar: {exc}")

    def send_hotkey(self, keys: list[str] | str) -> ActionResult:
        """Envia uma combinação de teclas (ex.: `['ctrl','shift','esc']`)."""
        sequence = [k.strip().lower() for k in (keys.split("+") if isinstance(keys, str) else keys) if k.strip()]
        if not sequence:
            return ActionResult.fail("combinação de teclas vazia")
        try:
            self.gui.hotkey(*sequence)
            return ActionResult(True, f"enviei {'+'.join(sequence)}")
        except Exception as exc:
            return ActionResult.fail(f"não consegui enviar as teclas: {exc}")

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> ActionResult:
        """Clica em uma coordenada absoluta da tela."""
        try:
            self.gui.click(x=int(x), y=int(y), button=button, clicks=int(clicks))
            return ActionResult(True, f"cliquei em {x}, {y}")
        except Exception as exc:
            return ActionResult.fail(f"não consegui clicar: {exc}")

    def scroll(self, amount: int) -> ActionResult:
        """Rola a roda do mouse (positivo = para cima)."""
        try:
            self.gui.scroll(int(amount))
            return ActionResult(True, "rolagem enviada")
        except Exception as exc:
            return ActionResult.fail(f"não consegui rolar: {exc}")

    # ------------------------------------------------------------------ #
    # Janelas
    # ------------------------------------------------------------------ #
    def _find_window(self, title: str):
        """Procura uma janela visível cujo título contenha `title`."""
        if not IS_WINDOWS:
            return None
        import win32gui

        needle = title.strip().lower()
        matches: list[tuple[int, str]] = []

        def _enum(hwnd: int, _extra: Any) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            caption = win32gui.GetWindowText(hwnd)
            if caption and needle in caption.lower():
                matches.append((hwnd, caption))

        win32gui.EnumWindows(_enum, None)
        return matches[0] if matches else None

    def control_window(self, action: str, target_title: str = "") -> ActionResult:
        """
        Controla uma janela: `minimize`, `maximize`, `close`, `focus`,
        `restore`. Sem `target_title`, age na janela em foco.
        """
        if IS_LINUX:
            return ActionResult.of(linux_mod.window_action(action, target_title))
        if not IS_WINDOWS:
            return ActionResult.fail("controle de janelas não suportado neste sistema")

        import win32con
        import win32gui

        if target_title:
            found = self._find_window(target_title)
            if found is None:
                return ActionResult.fail(f"não encontrei a janela {target_title}")
            hwnd, caption = found
        else:
            hwnd = win32gui.GetForegroundWindow()
            caption = win32gui.GetWindowText(hwnd) or "janela ativa"

        commands = {
            "minimize": win32con.SW_MINIMIZE,
            "maximize": win32con.SW_MAXIMIZE,
            "restore": win32con.SW_RESTORE,
            "focus": win32con.SW_RESTORE,
        }
        try:
            if action == "close":
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return ActionResult(True, f"fechei {caption[:60]}")
            if action not in commands:
                return ActionResult.fail(f"ação de janela desconhecida: {action}")
            win32gui.ShowWindow(hwnd, commands[action])
            if action == "focus":
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    # O Windows recusa foco vindo de processo sem input recente.
                    self.gui.press("alt")
                    win32gui.SetForegroundWindow(hwnd)
            labels = {
                "minimize": "minimizei",
                "maximize": "maximizei",
                "restore": "restaurei",
                "focus": "foquei",
            }
            return ActionResult(True, f"{labels[action]} {caption[:60]}")
        except Exception as exc:
            return ActionResult.fail(f"não consegui controlar a janela: {exc}")

    def list_windows(self, max_items: int = 20) -> ActionResult:
        """Lista os títulos das janelas visíveis."""
        if IS_LINUX:
            linux_titles = [title for _wid, title in linux_mod.list_windows()][:max_items]
            if not linux_titles and not linux_mod.have("wmctrl"):
                return ActionResult.fail("preciso do wmctrl para listar janelas (sudo apt install wmctrl)")
            return ActionResult(True, f"{len(linux_titles)} janela(s) aberta(s)", {"windows": linux_titles})
        if not IS_WINDOWS:
            return ActionResult.fail("só disponível no Windows")
        import win32gui

        titles: list[str] = []

        def _enum(hwnd: int, _extra: Any) -> None:
            if win32gui.IsWindowVisible(hwnd):
                caption = win32gui.GetWindowText(hwnd)
                if caption.strip():
                    titles.append(caption)

        win32gui.EnumWindows(_enum, None)
        titles = titles[:max_items]
        return ActionResult(True, f"{len(titles)} janela(s) aberta(s)", {"windows": titles})

    def minimize_all(self) -> ActionResult:
        """Minimiza todas as janelas (Win+M / "mostrar área de trabalho" no Linux)."""
        if IS_LINUX:
            ok, message, _data = linux_mod.show_desktop()
            return ActionResult(ok, "minimizei tudo" if ok else message)
        result = self.send_hotkey(["win", "m"])
        return ActionResult(result.ok, "minimizei tudo" if result.ok else result.message)

    def show_desktop(self) -> ActionResult:
        """Mostra a área de trabalho (Win+D)."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.show_desktop())
        result = self.send_hotkey(["win", "d"])
        return ActionResult(result.ok, "mostrando a área de trabalho" if result.ok else result.message)

    def alt_tab(self) -> ActionResult:
        """Alterna para a próxima janela."""
        try:
            self.gui.keyDown("alt")
            self.gui.press("tab")
            self.gui.keyUp("alt")
            return ActionResult(True, "próxima janela")
        except Exception as exc:
            return ActionResult.fail(f"não consegui alternar: {exc}")

    def toggle_fullscreen(self) -> ActionResult:
        """Alterna tela cheia (F11)."""
        result = self.send_hotkey(["f11"])
        return ActionResult(result.ok, "tela cheia alternada" if result.ok else result.message)

    # ------------------------------------------------------------------ #
    # Energia e sessão
    # ------------------------------------------------------------------ #
    def lock(self) -> ActionResult:
        """Bloqueia a estação de trabalho."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.lock())
        if not IS_WINDOWS:
            return ActionResult.fail("só disponível no Windows")
        try:
            ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
            return ActionResult(True, "estação bloqueada")
        except Exception as exc:
            return ActionResult.fail(f"não consegui bloquear: {exc}")

    def sleep(self) -> ActionResult:
        """Suspende o computador."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.suspend())
        result = self.run_shell("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return ActionResult(result.ok, "suspendendo" if result.ok else result.message)

    def shutdown(self, delay_s: int = 5) -> ActionResult:
        """Desliga o computador após `delay_s` segundos."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.schedule_power("poweroff", int(delay_s)))
        result = self.run_shell(f"shutdown /s /t {max(0, int(delay_s))}")
        return ActionResult(result.ok, f"desligando em {delay_s} segundos" if result.ok else result.message)

    def restart(self, delay_s: int = 5) -> ActionResult:
        """Reinicia o computador após `delay_s` segundos."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.schedule_power("reboot", int(delay_s)))
        result = self.run_shell(f"shutdown /r /t {max(0, int(delay_s))}")
        return ActionResult(result.ok, f"reiniciando em {delay_s} segundos" if result.ok else result.message)

    def cancel_shutdown(self) -> ActionResult:
        """Cancela um desligamento agendado."""
        if IS_LINUX:
            cancelled = linux_mod.cancel_power()
            return ActionResult(cancelled, "desligamento cancelado" if cancelled else "não havia desligamento agendado")
        result = self.run_shell("shutdown /a")
        return ActionResult(result.ok, "desligamento cancelado" if result.ok else "não havia desligamento agendado")

    def task_manager(self) -> ActionResult:
        """Abre o Gerenciador de Tarefas."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.task_manager())
        result = self.send_hotkey(["ctrl", "shift", "esc"])
        return ActionResult(result.ok, "gerenciador de tarefas aberto" if result.ok else result.message)

    # ------------------------------------------------------------------ #
    # Tela
    # ------------------------------------------------------------------ #
    def screenshot(
        self,
        region: tuple[int, int, int, int] | None = None,
        save_path: str = "",
    ) -> ActionResult:
        """
        Captura a tela inteira ou uma região `(x, y, largura, altura)`.

        Returns:
            `ActionResult` com `data["path"]` apontando para o PNG salvo.
        """
        target = (
            Path(save_path)
            if save_path
            else settings.screenshot_dir / f"shot_{datetime.now():%Y%m%d_%H%M%S}.png"
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image = self.gui.screenshot(region=tuple(region) if region else None)
            image.save(target)
            log.info("pc.screenshot", path=str(target), region=region)
            return ActionResult(
                True,
                "captura salva",
                {"path": str(target), "width": image.width, "height": image.height},
            )
        except Exception as exc:
            if IS_LINUX:
                # Wayland (ou sem scrot): ferramentas de captura do sistema.
                fallback = linux_mod.screenshot(target, tuple(region) if region else None)
                if fallback[0]:
                    return self._with_size(ActionResult.of(fallback))
            return ActionResult.fail(f"não consegui capturar a tela: {exc}")

    @staticmethod
    def _with_size(result: ActionResult) -> ActionResult:
        """Acrescenta largura/altura ao resultado de uma captura externa."""
        try:
            from PIL import Image

            with Image.open(result.data["path"]) as image:
                result.data.update(width=image.width, height=image.height)
        except Exception:  # pragma: no cover - imagem ilegível
            pass
        return result

    def screen_size(self) -> tuple[int, int]:
        """Resolução da tela principal."""
        try:
            size = self.gui.size()
            return int(size[0]), int(size[1])
        except Exception:
            return (1920, 1080)

    def snip(self) -> ActionResult:
        """Abre a ferramenta de recorte (Win+Shift+S)."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.snip())
        result = self.send_hotkey(["win", "shift", "s"])
        return ActionResult(result.ok, "recorte de tela aberto" if result.ok else result.message)

    # ------------------------------------------------------------------ #
    # Área de transferência
    # ------------------------------------------------------------------ #
    def clipboard_get(self) -> ActionResult:
        """Lê o texto da área de transferência."""
        try:
            import pyperclip

            content = pyperclip.paste() or ""
            preview = content[:500]
            return ActionResult(
                bool(content),
                f"área de transferência: {preview}" if content else "a área de transferência está vazia",
                {"content": content},
            )
        except Exception as exc:
            return ActionResult.fail(f"não consegui ler a área de transferência: {exc}")

    def clipboard_set(self, text: str) -> ActionResult:
        """Grava texto na área de transferência."""
        try:
            import pyperclip

            pyperclip.copy(text)
            return ActionResult(True, "copiado para a área de transferência")
        except Exception as exc:
            return ActionResult.fail(f"não consegui copiar: {exc}")

    def copy(self) -> ActionResult:
        """Envia Ctrl+C."""
        result = self.send_hotkey(["ctrl", "c"])
        return ActionResult(result.ok, "copiado" if result.ok else result.message)

    def paste(self) -> ActionResult:
        """Envia Ctrl+V."""
        result = self.send_hotkey(["ctrl", "v"])
        return ActionResult(result.ok, "colado" if result.ok else result.message)

    # ------------------------------------------------------------------ #
    # Informação
    # ------------------------------------------------------------------ #
    def system_info(self) -> ActionResult:
        """CPU, memória, disco, bateria e rede."""
        import psutil

        try:
            cpu = psutil.cpu_percent(interval=0.4)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\" if IS_WINDOWS else "/")
            net = psutil.net_io_counters()
            data: dict[str, Any] = {
                "cpu_percent": cpu,
                "cpu_cores": psutil.cpu_count(logical=True),
                "ram_percent": memory.percent,
                "ram_used_gb": round(memory.used / 1e9, 1),
                "ram_total_gb": round(memory.total / 1e9, 1),
                "disk_percent": disk.percent,
                "disk_free_gb": round(disk.free / 1e9, 1),
                "net_sent_mb": round(net.bytes_sent / 1e6, 1),
                "net_recv_mb": round(net.bytes_recv / 1e6, 1),
                "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
            }
            battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            if battery is not None:
                data["battery_percent"] = round(battery.percent)
                data["battery_plugged"] = battery.power_plugged

            summary = (
                f"CPU em {cpu:.0f} por cento, memória em {memory.percent:.0f} por cento "
                f"({data['ram_used_gb']} de {data['ram_total_gb']} gigabytes), "
                f"disco com {data['disk_free_gb']} gigabytes livres"
            )
            if "battery_percent" in data:
                summary += f", bateria em {data['battery_percent']} por cento"
            return ActionResult(True, summary, data)
        except Exception as exc:
            return ActionResult.fail(f"não consegui ler o estado do sistema: {exc}")

    def current_time(self) -> ActionResult:
        """Hora atual por extenso."""
        now = datetime.now()
        return ActionResult(
            True,
            f"são {now.hour} horas e {now.minute:02d} minutos",
            {"iso": now.isoformat(timespec="seconds")},
        )

    def current_date(self) -> ActionResult:
        """Data atual por extenso."""
        now = datetime.now()
        text = f"hoje é {_WEEKDAYS[now.weekday()]}, {now.day} de {_MONTHS[now.month - 1]} de {now.year}"
        return ActionResult(True, text, {"iso": now.date().isoformat()})

    def weather(self, city: str) -> ActionResult:
        """Consulta o clima em `city` via wttr.in."""
        import urllib.parse
        import urllib.request

        place = urllib.parse.quote(city.strip() or "")
        url = f"https://wttr.in/{place}?format=%C+%t+sensacao+%f+vento+%w&lang=pt&m"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace").strip()
            if not body or "Unknown location" in body:
                return ActionResult.fail(f"não encontrei a cidade {city}")
            return ActionResult(True, f"em {city}: {body}", {"raw": body, "city": city})
        except Exception as exc:
            return ActionResult.fail(f"não consegui consultar o clima: {exc}")

    # ------------------------------------------------------------------ #
    # Timers
    # ------------------------------------------------------------------ #
    def start_timer(self, seconds: float, name: str = "") -> ActionResult:
        """Agenda um timer que dispara o callback `on_timer`."""
        if seconds <= 0:
            return ActionResult.fail("duração inválida")
        label = name.strip() or f"timer{len(self.timers) + 1}"

        def _fire() -> None:
            self.timers.pop(label, None)
            if self.on_timer is not None:
                try:
                    self.on_timer(label)
                except Exception as exc:  # pragma: no cover
                    log.warning("pc.timer_callback_failed", error=str(exc))

        existing = self.timers.pop(label, None)
        if existing is not None:
            existing.handle.cancel()

        handle = threading.Timer(seconds, _fire)
        handle.daemon = True
        handle.start()
        self.timers[label] = Timer(label, datetime.now() + timedelta(seconds=seconds), handle)

        minutes = seconds / 60
        spoken = f"{minutes:.0f} minutos" if minutes >= 1 else f"{seconds:.0f} segundos"
        log.info("pc.timer_started", name=label, seconds=seconds)
        return ActionResult(True, f"timer de {spoken} iniciado", {"name": label, "seconds": seconds})

    def cancel_timer(self, name: str = "") -> ActionResult:
        """Cancela um timer (ou todos, se `name` for vazio)."""
        if name:
            timer = self.timers.pop(name, None)
            if timer is None:
                return ActionResult.fail(f"não há timer chamado {name}")
            timer.handle.cancel()
            return ActionResult(True, f"timer {name} cancelado")
        if not self.timers:
            return ActionResult.fail("não há timers ativos")
        count = len(self.timers)
        for timer in self.timers.values():
            timer.handle.cancel()
        self.timers.clear()
        return ActionResult(True, f"{count} timer(s) cancelado(s)")

    def list_timers(self) -> ActionResult:
        """Lista os timers ativos e quanto falta para cada um."""
        if not self.timers:
            return ActionResult(True, "nenhum timer ativo", {"timers": []})
        parts = [f"{name}: {timer.remaining_s / 60:.1f} min" for name, timer in self.timers.items()]
        return ActionResult(True, "; ".join(parts), {"timers": parts})

    # ------------------------------------------------------------------ #
    # Delegações
    # ------------------------------------------------------------------ #
    def open_app(self, name: str) -> ActionResult:
        """Abre um aplicativo (ver `executor.apps`)."""
        result = apps_mod.open_app(name)
        return ActionResult(result.ok, result.message, {"detail": result.detail})

    def close_app(self, name: str, force: bool = False) -> ActionResult:
        """Fecha um aplicativo."""
        result = apps_mod.close_app(name, force)
        return ActionResult(result.ok, result.message, {"detail": result.detail})

    def open_folder(self, name: str) -> ActionResult:
        """Abre uma pasta no Explorer."""
        result = files_mod.open_folder(name)
        return ActionResult(result.ok, result.message, {"paths": result.paths})

    def search_files(self, query: str, root: str = "", max_results: int = 15) -> ActionResult:
        """Busca arquivos por nome."""
        result = files_mod.search_files(query, root, max_results)
        return ActionResult(result.ok, result.message, {"paths": result.paths})

    def volume(self, percent: float | None = None, delta: float | None = None) -> ActionResult:
        """Define ou ajusta o volume."""
        if delta is not None:
            result = media_mod.change_volume(delta)
        elif percent is not None:
            result = media_mod.set_volume(percent)
        else:
            result = media_mod.get_volume()
        return ActionResult(result.ok, result.message, {"value": result.value})

    def brightness(self, percent: float | None = None, delta: float | None = None) -> ActionResult:
        """Define ou ajusta o brilho."""
        if delta is not None:
            result = media_mod.change_brightness(delta)
        elif percent is not None:
            result = media_mod.set_brightness(percent)
        else:
            result = media_mod.get_brightness()
        return ActionResult(result.ok, result.message, {"value": result.value})

    # ------------------------------------------------------------------ #
    # Edição (atalhos universais)
    # ------------------------------------------------------------------ #
    def edit_action(self, action: str) -> ActionResult:
        """Atalhos de edição: `undo`, `redo`, `cut`, `select_all`, `save`, `new`, `find`."""
        mapping: dict[str, tuple[list[str], str]] = {
            "undo": (["ctrl", "z"], "desfeito"),
            "redo": (["ctrl", "y"], "refeito"),
            "cut": (["ctrl", "x"], "recortado"),
            "select_all": (["ctrl", "a"], "tudo selecionado"),
            "save": (["ctrl", "s"], "salvo"),
            "new": (["ctrl", "n"], "novo documento aberto"),
            "find": (["ctrl", "f"], "busca aberta"),
            "print": (["ctrl", "p"], "impressão aberta"),
        }
        if action not in mapping:
            return ActionResult.fail(f"ação de edição desconhecida: {action}")
        keys, label = mapping[action]
        result = self.send_hotkey(keys)
        return ActionResult(result.ok, label if result.ok else result.message)

    # ------------------------------------------------------------------ #
    # Rede, lixeira, exclusão, terminal elevado
    # ------------------------------------------------------------------ #
    def wifi(self, enable: bool) -> ActionResult:
        """
        Liga/desliga o Wi-Fi.

        Desabilitar a interface exige privilégios de administrador; sem eles
        o Windows devolve erro e o motivo real é repassado.
        """
        if IS_LINUX:
            return ActionResult.of(linux_mod.wifi(enable))
        if not IS_WINDOWS:
            return ActionResult.fail("controle de Wi-Fi não suportado neste sistema")
        interface = self._wifi_interface() or "Wi-Fi"
        state = "enabled" if enable else "disabled"
        result = self.run_shell(f'netsh interface set interface name="{interface}" admin={state}')
        if result.ok:
            return ActionResult(True, "wi-fi ligado" if enable else "wi-fi desligado")
        detail = (result.data.get("stdout") or result.data.get("stderr") or "").strip()
        if "elevation" in detail.lower() or "administrador" in detail.lower() or "requires" in detail.lower():
            return ActionResult.fail("preciso rodar como administrador para mexer no wi-fi")
        return ActionResult.fail(f"não consegui alterar o wi-fi: {detail[:120] or result.message}")

    def _wifi_interface(self) -> str | None:
        """Descobre o nome da interface sem fio (varia com o idioma do Windows)."""
        result = self.run_shell("netsh wlan show interfaces")
        for line in (result.data.get("stdout") or "").splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() in {"name", "nome"} and value.strip():
                return value.strip()
        return None

    def empty_recycle_bin(self) -> ActionResult:
        """Esvazia a lixeira de todas as unidades, sem diálogo."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.empty_trash())
        if not IS_WINDOWS:
            return ActionResult.fail("só disponível no Windows")
        try:
            # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            code = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)  # type: ignore[attr-defined]
            # -2147418113 (E_UNEXPECTED) significa "lixeira já vazia".
            if code in (0, -2147418113):
                return ActionResult(True, "lixeira esvaziada")
            return ActionResult.fail(f"o Windows recusou esvaziar a lixeira (código {code})")
        except Exception as exc:
            return ActionResult.fail(f"não consegui esvaziar a lixeira: {exc}")

    def delete_path(self, target: str) -> ActionResult:
        """
        Envia um arquivo/pasta para a lixeira (reversível).

        Aceita caminho completo ou nome; se for nome, usa o primeiro
        resultado da busca de arquivos.
        """
        path = Path(os.path.expandvars(os.path.expanduser(target.strip())))
        if not path.exists():
            found = files_mod.search_files(target, "", 1)
            if not found.ok or not found.paths:
                return ActionResult.fail(f"não encontrei {target}")
            path = Path(found.paths[0])
        try:
            from send2trash import send2trash

            send2trash(str(path))
            log.info("pc.deleted", path=str(path))
            return ActionResult(True, f"{path.name} foi para a lixeira", {"path": str(path)})
        except Exception as exc:
            return ActionResult.fail(f"não consegui apagar {path.name}: {exc}")

    def find_path(self, target: str) -> ActionResult:
        """Resolve um nome de arquivo para um caminho (sem alterar nada)."""
        path = Path(os.path.expandvars(os.path.expanduser(target.strip())))
        if path.exists():
            return ActionResult(True, str(path), {"path": str(path)})
        found = files_mod.search_files(target, "", 1)
        if not found.ok or not found.paths:
            return ActionResult.fail(f"não encontrei {target}")
        return ActionResult(True, found.paths[0], {"path": found.paths[0]})

    def admin_terminal(self, shell: str = "cmd") -> ActionResult:
        """Abre um terminal elevado (o Windows exibe o UAC; no Linux, `sudo -i`)."""
        if IS_LINUX:
            return ActionResult.of(linux_mod.admin_terminal())
        if not IS_WINDOWS:
            return ActionResult.fail("só disponível no Windows")
        exe = "powershell.exe" if shell == "powershell" else "cmd.exe"
        try:
            code = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, None, 1)  # type: ignore[attr-defined]
            if code <= 32:
                return ActionResult.fail("a elevação foi cancelada ou negada")
            label = "powershell" if exe.startswith("power") else "prompt"
            return ActionResult(True, f"{label} de administrador aberto")
        except Exception as exc:
            return ActionResult.fail(f"não consegui abrir o terminal elevado: {exc}")

    def open_url(self, url: str) -> ActionResult:
        """Abre uma URL no navegador padrão do usuário."""
        import webbrowser

        target = url.strip()
        if not target:
            return ActionResult.fail("endereço vazio")
        if not target.startswith(("http://", "https://")):
            target = f"https://{target}"
        try:
            webbrowser.open(target, new=2)
            return ActionResult(True, "abrindo no navegador", {"url": target})
        except Exception as exc:
            return ActionResult.fail(f"não consegui abrir o navegador: {exc}")

    def shutdown_timers(self) -> None:
        """Cancela todos os timers pendentes (usado no encerramento)."""
        for timer in self.timers.values():
            timer.handle.cancel()
        self.timers.clear()


__all__ = ["ActionResult", "PCController", "Timer"]
