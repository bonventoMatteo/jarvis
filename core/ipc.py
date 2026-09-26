"""
Gatilho externo por socket local (127.0.0.1).

Permite ativar o JARVIS de fora do processo — essencial no Wayland, onde
aplicativos não recebem atalhos globais: registre nas configurações do GNOME
um atalho que roda `python main.py --activate`.

Protocolo (uma linha UTF-8):
    ACTIVATE        -> mesmo efeito de palma / wake word / hotkey
    TEXT <comando>  -> executa o comando como se tivesse sido falado
"""
from __future__ import annotations

import asyncio
import socket

import structlog

from config import settings
from core.events import EventBus, EventType

log = structlog.get_logger(__name__)


class TriggerServer:
    """Servidor TCP mínimo, só em localhost."""

    def __init__(self, bus: EventBus, port: int | None = None) -> None:
        self.bus = bus
        self.port = port or settings.ipc_port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> bool:
        try:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
            log.info("ipc.listening", port=self.port)
            return True
        except OSError as exc:
            log.warning("ipc.unavailable", port=self.port, error=str(exc))
            return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 5.0)).decode("utf-8", "replace").strip()
            command, _, rest = line.partition(" ")
            if command == "ACTIVATE":
                self.bus.emit(EventType.HOTKEY, source="ipc", combination="externo")
                writer.write(b"OK\n")
            elif command == "TEXT" and rest.strip():
                self.bus.emit(EventType.NOTICE, source="ipc", text=f"comando externo: {rest.strip()}")
                self.bus.emit(EventType.TRANSCRIPT_REQUEST, source="ipc", text=rest.strip())
                writer.write(b"OK\n")
            else:
                writer.write(b"ERR comando desconhecido\n")
            await writer.drain()
        except (TimeoutError, ConnectionError) as exc:
            log.debug("ipc.client_error", error=str(exc))
        finally:
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def send(message: str, port: int | None = None, timeout: float = 3.0) -> str:
    """Envia uma linha ao JARVIS em execução e devolve a resposta."""
    with socket.create_connection(("127.0.0.1", port or settings.ipc_port), timeout=timeout) as conn:
        conn.sendall(message.strip().encode("utf-8") + b"\n")
        return conn.recv(256).decode("utf-8", "replace").strip()


__all__ = ["TriggerServer", "send"]
