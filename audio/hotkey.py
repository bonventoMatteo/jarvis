"""
Hotkey global (Ctrl+Alt+J) como terceira via de ativação.

* Windows: biblioteca `keyboard` (hook global, sem privilégios).
* Linux: `pynput` (X11/XWayland, sem root — a `keyboard` exigiria root lá).
  Em sessões Wayland puras o compositor não entrega teclas globais a
  aplicativos: nesse caso registre o atalho nas configurações do GNOME
  apontando para `jarvis.sh --activate` (veja o README).

Se nada funcionar, o JARVIS segue funcionando por palma e wake word.
"""
from __future__ import annotations

import structlog

from config import IS_WINDOWS, settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


def to_pynput(combination: str) -> str:
    """'ctrl+alt+j' -> '<ctrl>+<alt>+j' (formato do pynput)."""
    parts = []
    for key in combination.lower().split("+"):
        key = key.strip()
        if not key:
            continue
        key = {"win": "cmd", "super": "cmd", "windows": "cmd", "control": "ctrl"}.get(key, key)
        parts.append(key if len(key) == 1 else f"<{key}>")
    return "+".join(parts)


class HotkeyListener:
    """Registra um atalho global que publica `HOTKEY` no barramento."""

    def __init__(self, bus: EventBus, combination: str | None = None) -> None:
        self.bus = bus
        self.combination = combination or settings.hotkey
        self.available = False
        self._handle = None
        self._keyboard = None
        self._listener = None

    def start(self) -> bool:
        """Registra o atalho. Retorna False se não for possível."""
        if not settings.hotkey_enabled:
            return False
        try:
            if IS_WINDOWS:
                import keyboard

                self._keyboard = keyboard
                self._handle = keyboard.add_hotkey(self.combination, self._fire, suppress=False)
            else:
                from pynput import keyboard as pynput_keyboard

                self._listener = pynput_keyboard.GlobalHotKeys({to_pynput(self.combination): self._fire})
                self._listener.daemon = True
                self._listener.start()
            self.available = True
            log.info("hotkey.registered", combination=self.combination)
            return True
        except Exception as exc:
            log.warning("hotkey.unavailable", error=str(exc), combination=self.combination)
            self.available = False
            return False

    def _fire(self) -> None:
        """Callback do hook (roda na thread da biblioteca de teclado)."""
        log.info("hotkey.activate", combination=self.combination)
        self.bus.emit_threadsafe(EventType.HOTKEY, source="hotkey", combination=self.combination)

    def stop(self) -> None:
        """Remove o atalho registrado."""
        if self._keyboard is not None and self._handle is not None:
            try:
                self._keyboard.remove_hotkey(self._handle)
            except Exception:  # pragma: no cover
                pass
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # pragma: no cover
                pass
        self._handle = None
        self._listener = None
        self.available = False


__all__ = ["HotkeyListener", "to_pynput"]
