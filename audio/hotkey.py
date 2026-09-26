"""
Hotkey global (Ctrl+Alt+J) como terceira via de ativação.

Usa a biblioteca `keyboard`, que no Windows registra um hook global. Se ela
não estiver disponível (ou faltar permissão), o JARVIS segue funcionando
apenas por palma e wake word.
"""
from __future__ import annotations

import structlog

from config import settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


class HotkeyListener:
    """Registra um atalho global que publica `HOTKEY` no barramento."""

    def __init__(self, bus: EventBus, combination: str | None = None) -> None:
        self.bus = bus
        self.combination = combination or settings.hotkey
        self.available = False
        self._handle = None
        self._keyboard = None

    def start(self) -> bool:
        """Registra o atalho. Retorna False se não for possível."""
        if not settings.hotkey_enabled:
            return False
        try:
            import keyboard

            self._keyboard = keyboard
            self._handle = keyboard.add_hotkey(self.combination, self._fire, suppress=False)
            self.available = True
            log.info("hotkey.registered", combination=self.combination)
            return True
        except Exception as exc:
            log.warning("hotkey.unavailable", error=str(exc), combination=self.combination)
            self.available = False
            return False

    def _fire(self) -> None:
        """Callback do hook (roda em thread da lib `keyboard`)."""
        log.info("hotkey.activate", combination=self.combination)
        self.bus.emit_threadsafe(EventType.HOTKEY, source="hotkey", combination=self.combination)

    def stop(self) -> None:
        """Remove o atalho registrado."""
        if self._keyboard is not None and self._handle is not None:
            try:
                self._keyboard.remove_hotkey(self._handle)
            except Exception:  # pragma: no cover
                pass
        self._handle = None
        self.available = False


__all__ = ["HotkeyListener"]
