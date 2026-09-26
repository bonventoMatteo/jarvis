"""
Execução dos comandos rápidos reconhecidos pelo roteador (sem LLM).

Cada `Intent` vira uma chamada a `PCController`, ao agendador ou a um
atalho de teclado. Tudo que é bloqueante roda em `asyncio.to_thread`.
Os comandos de meta-controle (`cancel`, `repeat`, `quit`) são tratados pelo
orquestrador, não aqui.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import structlog

from core.scheduler import Scheduler, duration_seconds, parse_clock, parse_number
from executor import apps as apps_mod
from executor import files as files_mod
from executor.pc import ActionResult, PCController
from llm.router import Intent
from memory.store import MemoryStore

log = structlog.get_logger(__name__)

#: Atalhos de navegador: nome do intent -> (teclas, mensagem)
_BROWSER_KEYS: dict[str, tuple[list[str], str]] = {
    "new_tab": (["ctrl", "t"], "nova aba"),
    "close_tab": (["ctrl", "w"], "aba fechada"),
    "next_tab": (["ctrl", "tab"], "próxima aba"),
    "prev_tab": (["ctrl", "shift", "tab"], "aba anterior"),
    "reopen_tab": (["ctrl", "shift", "t"], "aba reaberta"),
    "reload": (["f5"], "página recarregada"),
    "history": (["ctrl", "h"], "histórico aberto"),
    "browser_downloads": (["ctrl", "j"], "downloads abertos"),
}

_EDIT_ACTIONS: dict[str, str] = {
    "undo": "undo",
    "redo": "redo",
    "cut": "cut",
    "select_all": "select_all",
    "save": "save",
    "new_document": "new",
}

_LEVELS = {"maximo": 100.0, "máximo": 100.0, "minimo": 0.0, "mínimo": 0.0, "metade": 50.0, "meio": 50.0}

_UNIT_LABEL = {"s": "segundo", "seg": "segundo", "min": "minuto", "h": "hora"}


def _level(params: dict[str, str]) -> float | None:
    """Extrai um percentual de `level` ('máximo') ou `n` ('70', 'setenta')."""
    if params.get("level"):
        return _LEVELS.get(params["level"].lower())
    if params.get("n"):
        return parse_number(params["n"])
    return None


def _spoken_duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return f"{hours} hora{'s' if hours > 1 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minuto{'s' if minutes > 1 else ''}"
    if seconds >= 60:
        return f"{seconds / 60:.1f} minutos".replace(".", ",")
    return f"{int(seconds)} segundos"


class FastCommandExecutor:
    """Executa `Intent`s rápidos."""

    def __init__(
        self,
        pc: PCController,
        scheduler: Scheduler,
        memory: MemoryStore,
        explorer_folder: Callable[[], Path | None] = files_mod.active_explorer_folder,
    ) -> None:
        self.pc = pc
        self.scheduler = scheduler
        self.memory = memory
        self.explorer_folder = explorer_folder

    async def execute(self, intent: Intent) -> ActionResult:
        """Executa o intent e devolve o resultado real (nunca inventado)."""
        handler = getattr(self, f"_do_{intent.name}", None)
        if handler is None:
            return ActionResult.fail(f"comando {intent.name} não implementado")
        try:
            result = handler(intent.params)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as exc:
            log.error("commands.failed", intent=intent.name, error=str(exc))
            return ActionResult.fail(f"erro ao executar {intent.name}: {exc}")

    async def _run(self, func: Callable[..., ActionResult], *args: object) -> ActionResult:
        return await asyncio.to_thread(func, *args)

    # ----------------------------- info -------------------------------- #
    async def _do_time(self, _p: dict[str, str]) -> ActionResult:
        return self.pc.current_time()

    async def _do_date(self, _p: dict[str, str]) -> ActionResult:
        return self.pc.current_date()

    async def _do_weather(self, p: dict[str, str]) -> ActionResult:
        city = p.get("city") or str(self.memory.get_pref("default_city", "") or "")
        result = await self._run(self.pc.weather, city)
        if result.ok and not city:
            result.message = result.message.replace("em : ", "aqui: ", 1)
        return result

    async def _do_system_info(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.system_info)

    async def _do_list_windows(self, _p: dict[str, str]) -> ActionResult:
        result = await self._run(self.pc.list_windows)
        if not result.ok:
            return result
        titles = result.data.get("windows", [])
        short = [title.split(" - ")[-1][:40] for title in titles[:6]]
        spoken = ", ".join(dict.fromkeys(short))
        return ActionResult(True, f"{len(titles)} janelas abertas: {spoken}", result.data)

    # ---------------------------- energia ------------------------------ #
    async def _do_lock(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.lock)

    async def _do_sleep(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.sleep)

    async def _do_restart(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.restart, 10)

    async def _do_shutdown(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.shutdown, 10)

    async def _do_cancel_shutdown(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.cancel_shutdown)

    async def _do_task_manager(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.task_manager)

    async def _do_admin_terminal(self, p: dict[str, str]) -> ActionResult:
        shell = "powershell" if "power" in p.get("shell", "").lower() else "cmd"
        return await self._run(self.pc.admin_terminal, shell)

    # ---------------------------- janelas ------------------------------ #
    async def _do_minimize_all(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.minimize_all)

    async def _do_show_desktop(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.show_desktop)

    async def _do_alt_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.alt_tab)

    async def _do_close_window(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.control_window, "close", "")

    async def _do_minimize_window(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.control_window, "minimize", "")

    async def _do_maximize_window(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.control_window, "maximize", "")

    async def _do_fullscreen(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.toggle_fullscreen)

    async def _do_focus_window(self, p: dict[str, str]) -> ActionResult:
        target = p.get("app", "")
        result = await self._run(self.pc.control_window, "focus", target)
        if result.ok:
            return result
        canonical = apps_mod.resolve_alias(target)
        if canonical != target.lower():
            return await self._run(self.pc.control_window, "focus", canonical)
        return result

    # ----------------------------- mídia ------------------------------- #
    async def _do_play_pause(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.play_pause)
        return ActionResult(result.ok, result.message)

    async def _do_next_track(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.next_track)
        return ActionResult(result.ok, result.message)

    async def _do_prev_track(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.previous_track)
        return ActionResult(result.ok, result.message)

    async def _do_stop_media(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.stop_media)
        return ActionResult(result.ok, result.message)

    # ---------------------------- volume ------------------------------- #
    async def _do_mute(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.set_mute, True)
        return ActionResult(result.ok, result.message)

    async def _do_unmute(self, _p: dict[str, str]) -> ActionResult:
        from executor import media

        result = await asyncio.to_thread(media.set_mute, False)
        return ActionResult(result.ok, result.message)

    async def _do_volume_get(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.volume)

    async def _do_volume_up(self, _p: dict[str, str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.volume, None, 10.0)

    async def _do_volume_down(self, _p: dict[str, str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.volume, None, -10.0)

    async def _do_volume_set(self, p: dict[str, str]) -> ActionResult:
        level = _level(p)
        if level is None:
            return ActionResult.fail("não entendi o nível de volume")
        return await asyncio.to_thread(self.pc.volume, level, None)

    # ---------------------------- brilho ------------------------------- #
    async def _do_brightness_up(self, _p: dict[str, str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.brightness, None, 10.0)

    async def _do_brightness_down(self, _p: dict[str, str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.brightness, None, -10.0)

    async def _do_brightness_set(self, p: dict[str, str]) -> ActionResult:
        level = _level(p)
        if level is None:
            return ActionResult.fail("não entendi o nível de brilho")
        return await asyncio.to_thread(self.pc.brightness, level, None)

    # ----------------------------- tela -------------------------------- #
    async def _do_screenshot(self, _p: dict[str, str]) -> ActionResult:
        result = await self._run(self.pc.screenshot)
        if result.ok:
            result.message = "captura de tela salva na pasta de screenshots"
        return result

    async def _do_snip(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.snip)

    # --------------------------- navegador ----------------------------- #
    async def _browser_key(self, name: str) -> ActionResult:
        keys, label = _BROWSER_KEYS[name]
        result = await self._run(self.pc.send_hotkey, keys)
        return ActionResult(result.ok, label if result.ok else result.message)

    async def _do_new_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("new_tab")

    async def _do_close_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("close_tab")

    async def _do_next_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("next_tab")

    async def _do_prev_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("prev_tab")

    async def _do_reopen_tab(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("reopen_tab")

    async def _do_reload(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("reload")

    async def _do_history(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("history")

    async def _do_browser_downloads(self, _p: dict[str, str]) -> ActionResult:
        return await self._browser_key("browser_downloads")

    async def _do_google(self, p: dict[str, str]) -> ActionResult:
        query = p.get("q", "").strip()
        if not query:
            return ActionResult.fail("não entendi o que pesquisar")
        url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}&hl=pt-BR"
        result = await self._run(self.pc.open_url, url)
        return ActionResult(result.ok, f"pesquisando {query}" if result.ok else result.message, result.data)

    async def _do_youtube(self, p: dict[str, str]) -> ActionResult:
        query = p.get("q", "").strip()
        url = (
            f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
            if query
            else "https://www.youtube.com"
        )
        return await self._run(self.pc.open_url, url)

    async def _do_gmail(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.open_url, "https://mail.google.com")

    async def _do_github(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.open_url, "https://github.com")

    async def _do_open_url(self, p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.open_url, p.get("url", ""))

    # ---------------------------- arquivos ----------------------------- #
    async def _do_open_folder(self, p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.open_folder, p.get("folder", ""))

    async def _do_create_folder(self, p: dict[str, str]) -> ActionResult:
        name = (p.get("name") or "Nova pasta").strip()
        parent_name = (p.get("parent") or "").strip()
        if p.get("here") or not parent_name:
            base = await asyncio.to_thread(self.explorer_folder) or files_mod.HOME / "Desktop"
        else:
            base = files_mod.resolve_folder(parent_name)
            if base is None:
                return ActionResult.fail(f"não encontrei a pasta {parent_name}")
        target = base / name
        try:
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
        except Exception as exc:
            return ActionResult.fail(f"não consegui criar a pasta: {exc}")
        return ActionResult(True, f"pasta {name} criada em {base.name or base}", {"path": str(target)})

    async def _do_search_file(self, p: dict[str, str]) -> ActionResult:
        query = p.get("q", "")
        result = await asyncio.to_thread(self.pc.search_files, query, "", 10)
        if not result.ok:
            return result
        paths: list[str] = result.data.get("paths", [])
        first = Path(paths[0]) if paths else None
        spoken = f"encontrei {len(paths)} resultado{'s' if len(paths) != 1 else ''}"
        if first is not None:
            spoken += f". O primeiro é {first.name}, em {first.parent.name}"
        return ActionResult(True, spoken, result.data)

    async def _do_open_file(self, p: dict[str, str]) -> ActionResult:
        found = await self._run(self.pc.find_path, p.get("q", ""))
        if not found.ok:
            return found
        opened = await asyncio.to_thread(files_mod.open_path, found.data["path"])
        return ActionResult(opened.ok, opened.message, {"path": found.data["path"]})

    async def _do_delete_file(self, p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.delete_path, p.get("q", ""))

    async def _do_empty_trash(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.empty_recycle_bin)

    # ------------------------ timers e lembretes ------------------------ #
    async def _do_timer(self, p: dict[str, str]) -> ActionResult:
        seconds = duration_seconds(p.get("n", ""), p.get("unit", "segundos"))
        if seconds is None or seconds <= 0:
            return ActionResult.fail("não entendi a duração do timer")
        spoken = _spoken_duration(seconds)
        item = self.scheduler.add_in("timer", seconds, spoken)
        return ActionResult(True, f"timer de {spoken} iniciado", {"id": item.id, "seconds": seconds})

    async def _do_alarm(self, p: dict[str, str]) -> ActionResult:
        when = parse_clock(p.get("clock", ""))
        if when is None:
            return ActionResult.fail(f"não entendi o horário {p.get('clock', '')}")
        item = self.scheduler.add("alarm", when, "")
        day = "amanhã" if when.date() > when.now().date() else "hoje"
        return ActionResult(True, f"alarme para {day} às {when:%H:%M}", {"id": item.id, "due": item.due_iso})

    async def _do_reminder(self, p: dict[str, str]) -> ActionResult:
        what = p.get("what", "").strip()
        if not what:
            return ActionResult.fail("não entendi do que lembrar")
        if p.get("clock"):
            when = parse_clock(p["clock"])
            if when is None:
                return ActionResult.fail(f"não entendi o horário {p['clock']}")
            item = self.scheduler.add("reminder", when, what)
            return ActionResult(True, f"vou lembrar às {when:%H:%M}", {"id": item.id})
        seconds = duration_seconds(p.get("n", ""), p.get("unit", "minutos"))
        if seconds is None or seconds <= 0:
            return ActionResult.fail("não entendi quando lembrar")
        item = self.scheduler.add_in("reminder", seconds, what)
        return ActionResult(True, f"vou lembrar em {_spoken_duration(seconds)}", {"id": item.id})

    async def _do_list_schedule(self, _p: dict[str, str]) -> ActionResult:
        items = self.scheduler.pending()
        if not items:
            return ActionResult(True, "nenhum timer, alarme ou lembrete ativo")
        return ActionResult(True, "; ".join(item.describe() for item in items[:5]))

    async def _do_cancel_timer(self, _p: dict[str, str]) -> ActionResult:
        count = self.scheduler.cancel("timer")
        return ActionResult(bool(count), f"{count} timer(s) cancelado(s)" if count else "não há timers ativos")

    async def _do_cancel_alarm(self, _p: dict[str, str]) -> ActionResult:
        count = self.scheduler.cancel("alarm")
        return ActionResult(bool(count), f"{count} alarme(s) cancelado(s)" if count else "não há alarmes ativos")

    async def _do_cancel_reminder(self, _p: dict[str, str]) -> ActionResult:
        count = self.scheduler.cancel("reminder")
        return ActionResult(bool(count), f"{count} lembrete(s) cancelado(s)" if count else "não há lembretes ativos")

    # --------------------------- clipboard ----------------------------- #
    async def _do_clipboard_read(self, _p: dict[str, str]) -> ActionResult:
        result = await self._run(self.pc.clipboard_get)
        if result.ok:
            content: str = result.data.get("content", "")
            preview = content[:200] + ("..." if len(content) > 200 else "")
            result.message = f"na área de transferência: {preview}"
        return result

    async def _do_copy(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.copy)

    async def _do_paste(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.paste)

    async def _edit(self, name: str) -> ActionResult:
        return await self._run(self.pc.edit_action, _EDIT_ACTIONS[name])

    async def _do_cut(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("cut")

    async def _do_undo(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("undo")

    async def _do_redo(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("redo")

    async def _do_select_all(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("select_all")

    async def _do_save(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("save")

    async def _do_new_document(self, _p: dict[str, str]) -> ActionResult:
        return await self._edit("new_document")

    # ------------------------------ rede ------------------------------- #
    async def _do_wifi_on(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.wifi, True)

    async def _do_wifi_off(self, _p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.wifi, False)

    # ------------------------------ apps ------------------------------- #
    async def _do_open_app(self, p: dict[str, str]) -> ActionResult:
        return await self._run(self.pc.open_app, p.get("app", ""))

    async def _do_close_app(self, p: dict[str, str]) -> ActionResult:
        return await asyncio.to_thread(self.pc.close_app, p.get("app", ""), False)


__all__ = ["FastCommandExecutor"]
