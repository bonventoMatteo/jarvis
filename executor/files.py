"""
Operações de arquivo e pastas.

Tudo é resolvido a partir de pastas conhecidas do usuário (Downloads,
Documentos, Área de Trabalho...) com nomes em pt-BR, e a busca usa o índice
do Windows Search quando disponível, caindo para varredura recursiva.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from config import IS_WINDOWS

log = structlog.get_logger(__name__)

HOME = Path.home()

#: Pastas conhecidas com seus nomes falados em pt-BR.
KNOWN_FOLDERS: dict[str, Path] = {
    "downloads": HOME / "Downloads",
    "documentos": HOME / "Documents",
    "documents": HOME / "Documents",
    "desktop": HOME / "Desktop",
    "área de trabalho": HOME / "Desktop",
    "area de trabalho": HOME / "Desktop",
    "imagens": HOME / "Pictures",
    "pictures": HOME / "Pictures",
    "fotos": HOME / "Pictures",
    "vídeos": HOME / "Videos",
    "videos": HOME / "Videos",
    "música": HOME / "Music",
    "musica": HOME / "Music",
    "music": HOME / "Music",
    "home": HOME,
    "usuário": HOME,
    "usuario": HOME,
}

#: Pastas ignoradas na busca recursiva — economizam segundos.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "AppData",
    "Windows", "$Recycle.Bin", "System Volume Information", ".cache",
}


@dataclass(slots=True)
class FileResult:
    """Resultado de uma operação de arquivo."""

    ok: bool
    message: str
    paths: list[str] = field(default_factory=list)
    content: str = ""


def resolve_folder(name: str) -> Path | None:
    """Traduz o nome falado de uma pasta para um caminho real."""
    key = name.strip().lower().rstrip("/\\")
    if key in KNOWN_FOLDERS:
        return KNOWN_FOLDERS[key]
    candidate = Path(os.path.expandvars(os.path.expanduser(name)))
    if candidate.exists():
        return candidate
    for known, path in KNOWN_FOLDERS.items():
        if key and key in known:
            return path
    return None


def open_folder(name: str) -> FileResult:
    """Abre uma pasta no Explorer."""
    path = resolve_folder(name)
    if path is None or not path.exists():
        return FileResult(False, f"não encontrei a pasta {name}")
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
        log.info("files.open_folder", path=str(path))
        return FileResult(True, f"abri {path.name}", [str(path)])
    except Exception as exc:
        return FileResult(False, f"não consegui abrir {path}: {exc}")


def open_path(target: str) -> FileResult:
    """Abre um arquivo ou pasta com o aplicativo padrão."""
    path = Path(os.path.expandvars(os.path.expanduser(target)))
    if not path.exists():
        resolved = resolve_folder(target)
        if resolved is None:
            return FileResult(False, f"caminho inexistente: {target}")
        path = resolved
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return FileResult(True, f"abri {path.name}", [str(path)])
    except Exception as exc:
        return FileResult(False, f"não consegui abrir {path.name}: {exc}")


def create_folder(name: str, parent: str = "desktop") -> FileResult:
    """Cria uma pasta dentro de uma pasta conhecida."""
    base = resolve_folder(parent) or HOME / "Desktop"
    target = base / name.strip()
    try:
        target.mkdir(parents=True, exist_ok=True)
        log.info("files.create_folder", path=str(target))
        return FileResult(True, f"criei a pasta {name} em {base.name}", [str(target)])
    except Exception as exc:
        return FileResult(False, f"não consegui criar a pasta: {exc}")


def read_file(path: str, max_lines: int = 300) -> FileResult:
    """Lê um arquivo de texto (até `max_lines` linhas)."""
    target = Path(os.path.expandvars(os.path.expanduser(path)))
    if not target.exists():
        return FileResult(False, f"arquivo não encontrado: {path}")
    if target.is_dir():
        return FileResult(False, f"{path} é uma pasta, não um arquivo")
    try:
        lines: list[str] = []
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    lines.append(f"... (truncado em {max_lines} linhas)")
                    break
                lines.append(line.rstrip("\n"))
        return FileResult(True, f"li {target.name}", [str(target)], "\n".join(lines))
    except Exception as exc:
        return FileResult(False, f"não consegui ler {target.name}: {exc}")


def write_file(path: str, content: str, mode: str = "write") -> FileResult:
    """Escreve (`write`) ou acrescenta (`append`) conteúdo a um arquivo."""
    target = Path(os.path.expandvars(os.path.expanduser(path)))
    flag = "a" if mode == "append" else "w"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open(flag, encoding="utf-8") as handle:
            handle.write(content)
        log.info("files.write", path=str(target), mode=mode, bytes=len(content))
        verb = "acrescentei a" if mode == "append" else "gravei"
        return FileResult(True, f"{verb} {target.name}", [str(target)])
    except Exception as exc:
        return FileResult(False, f"não consegui gravar {target.name}: {exc}")


def _search_windows_index(query: str, max_results: int) -> list[str]:
    """Consulta o índice do Windows Search (rápido) via PowerShell/ADO."""
    if not IS_WINDOWS:
        return []
    safe = query.replace("'", "''")
    script = f"""
$ErrorActionPreference='SilentlyContinue'
$c = New-Object -ComObject ADODB.Connection
$r = New-Object -ComObject ADODB.Recordset
$c.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
$q = "SELECT TOP {max_results} System.ItemPathDisplay FROM SYSTEMINDEX WHERE System.FileName LIKE '%{safe}%'"
$r.Open($q, $c)
while (-not $r.EOF) {{ $r.Fields.Item(0).Value; $r.MoveNext() }}
$r.Close(); $c.Close()
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except Exception as exc:
        log.debug("files.index_search_failed", error=str(exc))
        return []


def search_files(query: str, root: str = "", max_results: int = 15) -> FileResult:
    """
    Busca arquivos por nome.

    Usa o índice do Windows quando `root` não é informado; senão faz
    varredura recursiva ignorando pastas de sistema.
    """
    query = query.strip()
    if not query:
        return FileResult(False, "termo de busca vazio")

    if not root:
        hits = _search_windows_index(query, max_results)
        if hits:
            return FileResult(True, f"{len(hits)} resultado(s) para {query}", hits)

    base = resolve_folder(root) if root else HOME
    if base is None or not base.exists():
        return FileResult(False, f"pasta inexistente: {root}")

    needle = query.lower()
    hits: list[str] = []
    try:
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for filename in files:
                if needle in filename.lower():
                    hits.append(str(Path(current) / filename))
                    if len(hits) >= max_results:
                        raise StopIteration
    except StopIteration:
        pass
    except Exception as exc:
        log.warning("files.search_failed", error=str(exc))

    if not hits:
        return FileResult(False, f"nenhum arquivo encontrado para {query}")
    return FileResult(True, f"{len(hits)} resultado(s) para {query}", hits)


def active_explorer_folder() -> Path | None:
    """
    Pasta aberta na janela do Explorer em primeiro plano (ou na última
    janela do Explorer, se nenhuma estiver em foco). Usa o COM do Shell.
    """
    if not IS_WINDOWS:
        return None
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client
        import win32gui

        pythoncom.CoInitialize()
        try:
            foreground = win32gui.GetForegroundWindow()
            shell = win32com.client.Dispatch("Shell.Application")
            fallback: Path | None = None
            for window in shell.Windows():
                try:
                    folder = Path(window.Document.Folder.Self.Path)
                except Exception:
                    continue
                if not folder.exists():
                    continue
                if int(window.HWND) == int(foreground):
                    return folder
                fallback = folder
            return fallback
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        log.debug("files.active_explorer_failed", error=str(exc))
        return None


def list_folder(name: str, max_items: int = 40) -> FileResult:
    """Lista o conteúdo de uma pasta conhecida."""
    path = resolve_folder(name)
    if path is None or not path.exists():
        return FileResult(False, f"não encontrei a pasta {name}")
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max_items]
        names = [f"{'[D] ' if entry.is_dir() else ''}{entry.name}" for entry in entries]
        return FileResult(True, f"{len(names)} item(ns) em {path.name}", [str(path)], "\n".join(names))
    except Exception as exc:
        return FileResult(False, f"não consegui listar {path.name}: {exc}")


__all__ = [
    "KNOWN_FOLDERS",
    "FileResult",
    "active_explorer_folder",
    "create_folder",
    "list_folder",
    "open_folder",
    "open_path",
    "read_file",
    "resolve_folder",
    "search_files",
    "write_file",
]
