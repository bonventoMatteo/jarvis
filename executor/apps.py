"""
Abertura e fechamento de aplicativos no Windows.

A busca segue quatro estratégias, em ordem:
  1. caminhos conhecidos do registro interno (`APP_REGISTRY`);
  2. executável no PATH (`shutil.which`);
  3. `Get-StartApps` do PowerShell — cobre apps da Microsoft Store (UWP);
  4. `os.startfile` com o nome cru (deixa o shell resolver).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from config import IS_WINDOWS

log = structlog.get_logger(__name__)

_LOCAL = os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local")
_PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
_APPDATA = os.environ.get("APPDATA", r"C:\Users\Default\AppData\Roaming")

#: nome canônico -> (apelidos, executáveis candidatos, nomes de processo)
APP_REGISTRY: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "chrome": (
        ("google chrome", "cromo", "navegador"),
        (
            "chrome.exe",
            rf"{_PF}\Google\Chrome\Application\chrome.exe",
            rf"{_PF86}\Google\Chrome\Application\chrome.exe",
            rf"{_LOCAL}\Google\Chrome\Application\chrome.exe",
        ),
        ("chrome.exe",),
    ),
    "firefox": (
        ("mozilla", "mozilla firefox"),
        ("firefox.exe", rf"{_PF}\Mozilla Firefox\firefox.exe", rf"{_PF86}\Mozilla Firefox\firefox.exe"),
        ("firefox.exe",),
    ),
    "edge": (
        ("microsoft edge",),
        ("msedge.exe", rf"{_PF86}\Microsoft\Edge\Application\msedge.exe"),
        ("msedge.exe",),
    ),
    "vscode": (
        ("code", "visual studio code", "vs code", "vscode"),
        ("code.cmd", "code.exe", rf"{_LOCAL}\Programs\Microsoft VS Code\Code.exe"),
        ("Code.exe",),
    ),
    "word": (("microsoft word",), ("winword.exe",), ("WINWORD.EXE",)),
    "excel": (("microsoft excel",), ("excel.exe",), ("EXCEL.EXE",)),
    "powerpoint": (("microsoft powerpoint", "power point"), ("powerpnt.exe",), ("POWERPNT.EXE",)),
    "outlook": (("microsoft outlook",), ("outlook.exe",), ("OUTLOOK.EXE",)),
    "explorer": (
        ("explorador", "explorador de arquivos", "gerenciador de arquivos", "arquivos"),
        ("explorer.exe",),
        ("explorer.exe",),
    ),
    "calculadora": (("calculator", "calc", "calculadora"), ("calc.exe",), ("Calculator.exe", "CalculatorApp.exe")),
    "notepad": (("bloco de notas", "notepad", "bloco de nota"), ("notepad.exe",), ("notepad.exe",)),
    "spotify": (
        ("música", "musica"),
        ("spotify.exe", rf"{_APPDATA}\Spotify\Spotify.exe"),
        ("Spotify.exe",),
    ),
    "discord": (
        (),
        ("discord.exe", rf"{_LOCAL}\Discord\Update.exe"),
        ("Discord.exe",),
    ),
    "whatsapp": (("zap", "whats", "whats app"), ("whatsapp.exe",), ("WhatsApp.exe",)),
    "steam": ((), ("steam.exe", rf"{_PF86}\Steam\steam.exe"), ("steam.exe",)),
    "obs": (
        ("obs studio",),
        ("obs64.exe", rf"{_PF}\obs-studio\bin\64bit\obs64.exe"),
        ("obs64.exe",),
    ),
    "telegram": ((), ("telegram.exe", rf"{_APPDATA}\Telegram Desktop\Telegram.exe"), ("Telegram.exe",)),
    "terminal": (
        ("windows terminal", "terminal do windows"),
        ("wt.exe",),
        ("WindowsTerminal.exe",),
    ),
    "powershell": (("power shell",), ("powershell.exe", "pwsh.exe"), ("powershell.exe", "pwsh.exe")),
    "cmd": (("prompt", "prompt de comando", "linha de comando"), ("cmd.exe",), ("cmd.exe",)),
    "paint": (("ms paint",), ("mspaint.exe",), ("mspaint.exe",)),
    "taskmgr": (
        ("gerenciador de tarefas", "task manager"),
        ("taskmgr.exe",),
        ("Taskmgr.exe",),
    ),
    "settings": (("configurações", "configuracoes", "ajustes"), ("ms-settings:",), ()),
    "notion": ((), ("notion.exe", rf"{_LOCAL}\Programs\Notion\Notion.exe"), ("Notion.exe",)),
}


@dataclass(slots=True)
class AppResult:
    """Resultado de uma operação com aplicativos."""

    ok: bool
    message: str
    detail: str = ""


def resolve_alias(name: str) -> str:
    """Traduz um apelido falado para o nome canônico do app."""
    key = name.strip().lower()
    if key in APP_REGISTRY:
        return key
    for canonical, (aliases, _paths, _procs) in APP_REGISTRY.items():
        if key == canonical or key in aliases:
            return canonical
    for canonical, (aliases, _paths, _procs) in APP_REGISTRY.items():
        if key in canonical or any(key in alias for alias in aliases):
            return canonical
    return key


def _find_start_app(name: str) -> str | None:
    """Procura o AppID de um app da Store via `Get-StartApps`."""
    if not IS_WINDOWS:
        return None
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-StartApps | Where-Object {{ $_.Name -like '*{name}*' }} | "
        "Select-Object -First 1 -ExpandProperty AppID"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        app_id = completed.stdout.strip().splitlines()
        return app_id[0].strip() if app_id and app_id[0].strip() else None
    except Exception as exc:
        log.debug("apps.start_apps_failed", name=name, error=str(exc))
        return None


def open_app(name_or_path: str) -> AppResult:
    """
    Abre um aplicativo pelo nome falado, apelido ou caminho completo.

    Returns:
        `AppResult` com `ok=False` e a razão real quando não encontra.
    """
    raw = name_or_path.strip()
    if not raw:
        return AppResult(False, "nome do aplicativo vazio")

    # Caminho explícito.
    candidate = Path(raw)
    if candidate.exists():
        try:
            os.startfile(str(candidate))  # type: ignore[attr-defined]
            return AppResult(True, f"abri {candidate.name}", str(candidate))
        except Exception as exc:
            return AppResult(False, f"não consegui abrir {candidate.name}: {exc}")

    canonical = resolve_alias(raw)
    entry = APP_REGISTRY.get(canonical)
    candidates: tuple[str, ...] = entry[1] if entry else (raw, f"{raw}.exe")

    for target in candidates:
        if target.endswith(":"):  # URI (ms-settings:)
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return AppResult(True, f"abri {canonical}", target)
            except Exception:
                continue
        path = Path(target)
        found = str(path) if path.is_absolute() and path.exists() else shutil.which(target)
        if not found:
            continue
        try:
            subprocess.Popen(
                [found],
                shell=False,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
            log.info("apps.opened", app=canonical, path=found)
            return AppResult(True, f"abri {canonical}", found)
        except Exception as exc:
            log.debug("apps.launch_failed", path=found, error=str(exc))

    # App da Store.
    app_id = _find_start_app(canonical if entry else raw)
    if app_id:
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            log.info("apps.opened_store", app=canonical, app_id=app_id)
            return AppResult(True, f"abri {canonical}", app_id)
        except Exception as exc:
            log.debug("apps.store_failed", app_id=app_id, error=str(exc))

    # Última tentativa: deixa o shell resolver.
    try:
        os.startfile(raw)  # type: ignore[attr-defined]
        return AppResult(True, f"abri {raw}", raw)
    except Exception as exc:
        log.warning("apps.not_found", app=raw, error=str(exc))
        return AppResult(False, f"não encontrei o aplicativo {raw}")


def close_app(name: str, force: bool = False) -> AppResult:
    """Fecha todos os processos de um aplicativo."""
    import psutil

    canonical = resolve_alias(name)
    entry = APP_REGISTRY.get(canonical)
    targets = {proc.lower() for proc in (entry[2] if entry else ())}
    targets.add(f"{canonical.lower()}.exe")
    targets.add(canonical.lower())

    killed = 0
    for process in psutil.process_iter(["name"]):
        process_name = (process.info.get("name") or "").lower()
        if process_name in targets or process_name.removesuffix(".exe") in targets:
            try:
                process.kill() if force else process.terminate()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                log.debug("apps.close_denied", pid=process.pid, error=str(exc))

    if killed:
        log.info("apps.closed", app=canonical, count=killed)
        return AppResult(True, f"fechei {canonical}", f"{killed} processo(s)")
    return AppResult(False, f"{canonical} não estava aberto")


def is_running(name: str) -> bool:
    """Indica se um aplicativo tem ao menos um processo ativo."""
    import psutil

    canonical = resolve_alias(name)
    entry = APP_REGISTRY.get(canonical)
    targets = {proc.lower() for proc in (entry[2] if entry else ())} | {f"{canonical}.exe"}
    for process in psutil.process_iter(["name"]):
        if (process.info.get("name") or "").lower() in targets:
            return True
    return False


def known_apps() -> list[str]:
    """Lista os nomes canônicos suportados."""
    return sorted(APP_REGISTRY)


__all__ = ["APP_REGISTRY", "AppResult", "close_app", "is_running", "known_apps", "open_app", "resolve_alias"]
