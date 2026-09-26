"""
Integração com o desktop Linux (Ubuntu/GNOME, KDE, XFCE…).

Cada função usa a ferramenta nativa mais comum e cai para alternativas:

| Área        | Primeira opção              | Alternativas                                  |
|-------------|-----------------------------|-----------------------------------------------|
| janelas     | wmctrl (X11/XWayland)       | xdotool; atalhos do GNOME                     |
| volume      | pactl (PulseAudio/PipeWire) | amixer                                        |
| brilho      | brightnessctl               | screen-brightness-control                     |
| mídia       | playerctl (MPRIS)           | teclas de mídia via pyautogui                 |
| energia     | loginctl / systemctl        | xdg-screensaver                               |
| captura     | pyautogui (X11)             | gnome-screenshot, grim, spectacle             |
| wi-fi       | nmcli                       | —                                             |
| lixeira     | gio trash                   | —                                             |
| busca       | plocate/locate              | varredura recursiva                           |

Todas as funções devolvem `(ok, mensagem, dados)` — `executor.pc` converte
para `ActionResult`. Nenhuma levanta exceção por ferramenta ausente: o motivo
real (ex.: "instale o wmctrl") volta na mensagem.
"""
from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

Result = tuple[bool, str, dict[str, Any]]


def _ok(message: str, **data: Any) -> Result:
    return True, message, data


def _fail(message: str) -> Result:
    return False, message, {}


def have(tool: str) -> bool:
    """True se o executável estiver no PATH."""
    return shutil.which(tool) is not None


def run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    """Executa sem shell; devolve None se o programa não existir ou travar."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        log.debug("linux.run_failed", cmd=cmd[0], error=str(exc))
        return None


def spawn(cmd: list[str]) -> bool:
    """Inicia um processo desacoplado (não espera terminar)."""
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (FileNotFoundError, OSError) as exc:
        log.debug("linux.spawn_failed", cmd=cmd[0], error=str(exc))
        return False


def session_type() -> str:
    """'x11', 'wayland' ou 'tty'."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if kind:
        return kind
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "tty"


def desktop() -> str:
    """Ambiente gráfico em minúsculas ('gnome', 'kde', 'xfce'...)."""
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def _press(*keys: str) -> bool:
    try:
        import pyautogui

        pyautogui.hotkey(*keys)
        return True
    except Exception as exc:
        log.debug("linux.hotkey_failed", keys=keys, error=str(exc))
        return False


# --------------------------------------------------------------------------- #
# Janelas
# --------------------------------------------------------------------------- #
def list_windows() -> list[tuple[str, str]]:
    """[(id, título)] das janelas abertas (wmctrl)."""
    completed = run(["wmctrl", "-l"])
    if completed is None or completed.returncode != 0:
        return []
    windows: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1] != "-1":  # -1 = painéis/desktop
            windows.append((parts[0], parts[3]))
    return windows


def find_window(title: str) -> tuple[str, str] | None:
    needle = title.strip().lower()
    return next(((wid, name) for wid, name in list_windows() if needle in name.lower()), None)


def active_window() -> tuple[str, str] | None:
    """(id hex, título) da janela em foco."""
    completed = run(["xdotool", "getactivewindow"])
    if completed is None or completed.returncode != 0 or not completed.stdout.strip():
        return None
    wid = f"0x{int(completed.stdout.strip()):08x}"
    name = run(["xdotool", "getactivewindow", "getwindowname"])
    return wid, (name.stdout.strip() if name is not None else "janela ativa")


_GNOME_KEYS: dict[str, tuple[str, ...]] = {
    "minimize": ("winleft", "h"),
    "maximize": ("winleft", "up"),
    "restore": ("winleft", "down"),
    "close": ("alt", "f4"),
}


def window_action(action: str, target_title: str = "") -> Result:
    """minimize | maximize | restore | close | focus."""
    if target_title:
        found = find_window(target_title)
        if found is None:
            if not have("wmctrl"):
                return _fail("preciso do wmctrl para achar janelas pelo nome (sudo apt install wmctrl)")
            return _fail(f"não encontrei a janela {target_title}")
    else:
        found = active_window()

    if found is not None and have("wmctrl"):
        wid, caption = found
        commands: dict[str, list[list[str]]] = {
            "focus": [["wmctrl", "-i", "-a", wid]],
            "close": [["wmctrl", "-i", "-c", wid]],
            "maximize": [["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"]],
            "restore": [["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz,hidden"],
                        ["wmctrl", "-i", "-a", wid]],
            "minimize": [["xdotool", "windowminimize", str(int(wid, 16))]],
        }
        if action not in commands:
            return _fail(f"ação de janela desconhecida: {action}")
        results = [run(cmd) for cmd in commands[action]]
        if all(item is not None and item.returncode == 0 for item in results):
            labels = {"focus": "foquei", "close": "fechei", "maximize": "maximizei",
                      "restore": "restaurei", "minimize": "minimizei"}
            return _ok(f"{labels[action]} {caption[:60]}")

    # Wayland puro ou sem wmctrl: atalhos do GNOME na janela em foco.
    if not target_title and action in _GNOME_KEYS and _press(*_GNOME_KEYS[action]):
        return _ok({"minimize": "minimizei", "maximize": "maximizei", "restore": "restaurei",
                    "close": "fechei"}[action] + " a janela ativa")
    if session_type() == "wayland":
        return _fail("no Wayland só consigo controlar a janela em foco; use uma sessão Xorg para mais controle")
    return _fail("não consegui controlar a janela (instale wmctrl e xdotool)")


def show_desktop() -> Result:
    completed = run(["wmctrl", "-k", "on"])
    if completed is not None and completed.returncode == 0:
        return _ok("mostrando a área de trabalho")
    if _press("winleft", "d"):
        return _ok("mostrando a área de trabalho")
    return _fail("não consegui mostrar a área de trabalho")


# --------------------------------------------------------------------------- #
# Energia e sessão
# --------------------------------------------------------------------------- #
def lock() -> Result:
    for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"],
                ["gnome-screensaver-command", "-l"], ["dm-tool", "lock"]):
        completed = run(cmd)
        if completed is not None and completed.returncode == 0:
            return _ok("sessão bloqueada")
    return _fail("não consegui bloquear a sessão")


def suspend() -> Result:
    completed = run(["systemctl", "suspend"])
    if completed is not None and completed.returncode == 0:
        return _ok("suspendendo")
    return _fail("o sistema recusou a suspensão")


_power_timer: threading.Timer | None = None


def schedule_power(action: str, delay_s: int) -> Result:
    """Desliga ('poweroff') ou reinicia ('reboot') após `delay_s` segundos (cancelável)."""
    global _power_timer
    cancel_power()

    def _fire() -> None:
        run(["systemctl", action], timeout=30)

    _power_timer = threading.Timer(max(0, delay_s), _fire)
    _power_timer.daemon = True
    _power_timer.start()
    verb = "desligando" if action == "poweroff" else "reiniciando"
    return _ok(f"{verb} em {delay_s} segundos")


def cancel_power() -> bool:
    global _power_timer
    if _power_timer is not None and _power_timer.is_alive():
        _power_timer.cancel()
        _power_timer = None
        return True
    # Também cancela um `shutdown +N` feito fora do Jarvis.
    completed = run(["shutdown", "-c"])
    return completed is not None and completed.returncode == 0


def task_manager() -> Result:
    for cmd in (["gnome-system-monitor"], ["plasma-systemmonitor"], ["ksysguard"],
                ["xfce4-taskmanager"], ["mate-system-monitor"]):
        if have(cmd[0]) and spawn(cmd):
            return _ok("monitor do sistema aberto")
    return terminal(["htop"] if have("htop") else ["top"])


def terminal(inner: list[str] | None = None) -> Result:
    """Abre um terminal (opcionalmente rodando `inner`)."""
    inner = inner or []
    candidates = [
        ["gnome-terminal", "--", *inner] if inner else ["gnome-terminal"],
        ["konsole", "-e", *inner] if inner else ["konsole"],
        ["xfce4-terminal", "-x", *inner] if inner else ["xfce4-terminal"],
        ["x-terminal-emulator", "-e", *inner] if inner else ["x-terminal-emulator"],
        ["xterm", "-e", *inner] if inner else ["xterm"],
    ]
    for cmd in candidates:
        if have(cmd[0]) and spawn(cmd):
            return _ok("terminal aberto")
    return _fail("nenhum emulador de terminal encontrado")


def admin_terminal() -> Result:
    result = terminal(["sudo", "-i"])
    if result[0]:
        return _ok("terminal de administrador aberto; digite sua senha")
    return result


# --------------------------------------------------------------------------- #
# Tela
# --------------------------------------------------------------------------- #
def screenshot(path: Path, region: tuple[int, int, int, int] | None = None) -> Result:
    """Captura com ferramentas do sistema (usado quando o pyautogui falha, ex. Wayland)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[list[str]] = []
    if region is None:
        attempts += [["gnome-screenshot", "-f", str(path)], ["grim", str(path)],
                     ["spectacle", "-b", "-n", "-f", "-o", str(path)], ["scrot", "-o", str(path)]]
    else:
        x, y, w, h = region
        attempts += [["grim", "-g", f"{x},{y} {w}x{h}", str(path)],
                     ["scrot", "-o", "-a", f"{x},{y},{w},{h}", str(path)]]
    for cmd in attempts:
        if not have(cmd[0]):
            continue
        completed = run(cmd, timeout=15)
        if completed is not None and completed.returncode == 0 and path.exists():
            return _ok("captura salva", path=str(path))
    return _fail("não consegui capturar a tela (instale gnome-screenshot ou grim)")


def snip() -> Result:
    for cmd in (["gnome-screenshot", "-a", "-c"], ["flameshot", "gui"], ["spectacle", "-r"]):
        if have(cmd[0]) and spawn(cmd):
            return _ok("selecione a região")
    if _press("shift", "printscreen"):
        return _ok("selecione a região")
    return _fail("nenhuma ferramenta de recorte encontrada")


# --------------------------------------------------------------------------- #
# Volume, brilho e mídia
# --------------------------------------------------------------------------- #
def get_volume() -> float | None:
    completed = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if completed is not None and completed.returncode == 0:
        match = re.search(r"(\d+)%", completed.stdout)
        if match:
            return float(match.group(1))
    completed = run(["amixer", "get", "Master"])
    if completed is not None and completed.returncode == 0:
        match = re.search(r"\[(\d+)%\]", completed.stdout)
        if match:
            return float(match.group(1))
    return None


def set_volume(percent: float) -> Result:
    level = int(max(0, min(100, percent)))
    for cmd in (["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
                ["amixer", "-q", "set", "Master", f"{level}%"]):
        completed = run(cmd)
        if completed is not None and completed.returncode == 0:
            set_mute(False)
            return _ok(f"volume em {level} por cento", value=float(level))
    return _fail("não consegui ajustar o volume (pactl/amixer indisponíveis)")


def set_mute(muted: bool) -> Result:
    for cmd in (["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"],
                ["amixer", "-q", "set", "Master", "mute" if muted else "unmute"]):
        completed = run(cmd)
        if completed is not None and completed.returncode == 0:
            return _ok("áudio mudo" if muted else "áudio reativado")
    return _fail("não consegui alterar o mudo")


def get_brightness() -> float | None:
    current, maximum = run(["brightnessctl", "get"]), run(["brightnessctl", "max"])
    if current is None or maximum is None or current.returncode or maximum.returncode:
        return None
    try:
        return round(int(current.stdout.strip()) * 100 / max(1, int(maximum.stdout.strip())))
    except ValueError:
        return None


def set_brightness(percent: float) -> Result:
    level = int(max(1, min(100, percent)))  # 0 apagaria a tela
    completed = run(["brightnessctl", "set", f"{level}%"])
    if completed is not None and completed.returncode == 0:
        return _ok(f"brilho em {level} por cento", value=float(level))
    return _fail("não consegui ajustar o brilho (instale brightnessctl e entre no grupo video)")


def media(action: str) -> Result:
    """play-pause | next | previous | stop via MPRIS (Spotify, navegadores, VLC…)."""
    completed = run(["playerctl", action])
    if completed is not None and completed.returncode == 0:
        return _ok({"play-pause": "play/pause", "next": "próxima faixa",
                    "previous": "faixa anterior", "stop": "reprodução parada"}[action])
    keys = {"play-pause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stop"}
    if _press(keys[action]):
        return _ok("comando de mídia enviado")
    return _fail("nenhum player de mídia respondeu (instale playerctl)")


# --------------------------------------------------------------------------- #
# Rede e lixeira
# --------------------------------------------------------------------------- #
def wifi(enable: bool) -> Result:
    completed = run(["nmcli", "radio", "wifi", "on" if enable else "off"])
    if completed is not None and completed.returncode == 0:
        return _ok("wi-fi ligado" if enable else "wi-fi desligado")
    detail = (completed.stderr.strip() if completed is not None else "nmcli não encontrado")[:120]
    return _fail(f"não consegui alterar o wi-fi: {detail}")


def empty_trash() -> Result:
    completed = run(["gio", "trash", "--empty"], timeout=60)
    if completed is not None and completed.returncode == 0:
        return _ok("lixeira esvaziada")
    trash = Path.home() / ".local/share/Trash"
    try:
        for sub in ("files", "info"):
            folder = trash / sub
            if folder.exists():
                shutil.rmtree(folder)
                folder.mkdir()
        return _ok("lixeira esvaziada")
    except OSError as exc:
        return _fail(f"não consegui esvaziar a lixeira: {exc}")


# --------------------------------------------------------------------------- #
# Pastas e busca
# --------------------------------------------------------------------------- #
def user_dir(kind: str) -> Path | None:
    """Pasta XDG localizada (DOWNLOAD -> ~/Downloads ou ~/Transferências)."""
    completed = run(["xdg-user-dir", kind])
    if completed is not None and completed.returncode == 0 and completed.stdout.strip():
        path = Path(completed.stdout.strip())
        if path.exists() and path != Path.home():
            return path
    return None


def locate(query: str, max_results: int) -> list[str]:
    """Busca rápida pelo índice do plocate/locate."""
    for tool in ("plocate", "locate"):
        if not have(tool):
            continue
        completed = run([tool, "-i", "-b", "-l", str(max_results), query], timeout=15)
        if completed is not None and completed.returncode == 0:
            home = str(Path.home())
            hits = [line for line in completed.stdout.splitlines() if line.startswith(home)]
            return [hit for hit in hits if os.path.exists(hit)][:max_results]
    return []


# --------------------------------------------------------------------------- #
# Aplicativos
# --------------------------------------------------------------------------- #
#: nome canônico -> comandos candidatos no Linux
LINUX_APPS: dict[str, tuple[str, ...]] = {
    "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "firefox": ("firefox",),
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "vscode": ("code", "codium"),
    "word": ("libreoffice --writer", "lowriter"),
    "excel": ("libreoffice --calc", "localc"),
    "powerpoint": ("libreoffice --impress", "loimpress"),
    "outlook": ("thunderbird", "evolution"),
    "explorer": ("nautilus", "dolphin", "thunar", "nemo", "xdg-open ~"),
    "calculadora": ("gnome-calculator", "kcalc", "galculator"),
    "notepad": ("gnome-text-editor", "gedit", "kate", "mousepad", "xed"),
    "spotify": ("spotify",),
    "discord": ("discord",),
    "whatsapp": ("whatsapp-for-linux", "xdg-open https://web.whatsapp.com"),
    "steam": ("steam",),
    "obs": ("obs",),
    "telegram": ("telegram-desktop", "Telegram"),
    "terminal": ("gnome-terminal", "konsole", "xfce4-terminal", "x-terminal-emulator"),
    "powershell": ("pwsh",),
    "cmd": ("gnome-terminal", "konsole", "x-terminal-emulator"),
    "paint": ("pinta", "kolourpaint", "gimp"),
    "taskmgr": ("gnome-system-monitor", "plasma-systemmonitor", "xfce4-taskmanager"),
    "settings": ("gnome-control-center", "systemsettings", "xfce4-settings-manager"),
    "notion": ("notion-app", "xdg-open https://www.notion.so"),
}

#: Processos a encerrar por app (quando difere do nome canônico).
LINUX_PROCESSES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome", "chromium"),
    "vscode": ("code",),
    "word": ("soffice.bin",),
    "excel": ("soffice.bin",),
    "powerpoint": ("soffice.bin",),
    "explorer": ("nautilus", "dolphin", "thunar", "nemo"),
    "calculadora": ("gnome-calculator", "kcalc"),
    "notepad": ("gnome-text-editor", "gedit", "kate", "mousepad"),
    "obs": ("obs",),
    "telegram": ("telegram-desktop",),
    "discord": ("discord", "Discord"),
    "spotify": ("spotify",),
}


def _desktop_entries() -> list[Path]:
    dirs = [Path.home() / ".local/share/applications", Path("/usr/share/applications"),
            Path("/var/lib/snapd/desktop/applications"), Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications"]
    return [entry for folder in dirs if folder.exists() for entry in folder.glob("*.desktop")]


def find_desktop_entry(name: str) -> str | None:
    """ID (.desktop) do app cujo Name contém `name` — cobre snaps e flatpaks."""
    needle = name.strip().lower()
    for entry in _desktop_entries():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(entry, encoding="utf-8")
            section = parser["Desktop Entry"]
        except (configparser.Error, KeyError, UnicodeDecodeError):
            continue
        if section.get("NoDisplay", "false").lower() == "true":
            continue
        names = [section.get("Name", ""), *(v for k, v in section.items() if k.startswith("name["))]
        if any(needle and needle in item.lower() for item in names) or needle == entry.stem.lower():
            return entry.stem
    return None


def open_app(canonical: str, raw: str) -> Result:
    """Abre um app: comandos conhecidos → PATH → .desktop (gtk-launch)."""
    for command in (*LINUX_APPS.get(canonical, ()), raw, raw.replace(" ", "-")):
        parts = [os.path.expanduser(part) for part in command.split()]
        if parts and have(parts[0]) and spawn(parts):
            log.info("apps.opened", app=canonical, cmd=command)
            return _ok(f"abri {canonical}", detail=command)
    entry = find_desktop_entry(raw) or find_desktop_entry(canonical)
    if entry and have("gtk-launch") and spawn(["gtk-launch", entry]):
        return _ok(f"abri {raw}", detail=f"{entry}.desktop")
    return _fail(f"não encontrei o aplicativo {raw}")


__all__ = [
    "LINUX_APPS",
    "LINUX_PROCESSES",
    "active_window",
    "admin_terminal",
    "cancel_power",
    "desktop",
    "empty_trash",
    "find_desktop_entry",
    "get_brightness",
    "get_volume",
    "have",
    "list_windows",
    "locate",
    "lock",
    "media",
    "open_app",
    "schedule_power",
    "screenshot",
    "session_type",
    "set_brightness",
    "set_mute",
    "set_volume",
    "show_desktop",
    "snip",
    "suspend",
    "task_manager",
    "terminal",
    "user_dir",
    "wifi",
    "window_action",
]
