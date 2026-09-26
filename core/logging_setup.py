"""Configuração de logging: structlog em JSON (arquivo) + rich (console)."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from config import LOGS_DIR, settings

_CONFIGURED = False


def configure_logging(console: bool = True, level: str | None = None) -> None:
    """
    Configura structlog.

    Args:
        console: se False, só grava no arquivo JSON (usado quando o dashboard
            rich está ocupando o terminal).
        level: sobrescreve `settings.log_level`.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, (level or settings.log_level).upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    if settings.log_json_file:
        log_file: Path = LOGS_DIR / "jarvis.jsonl"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(processor=structlog.processors.JSONRenderer())
        )
        handlers.append(file_handler)

    if console:
        try:
            from rich.logging import RichHandler

            rich_handler = RichHandler(rich_tracebacks=True, markup=False, show_path=False)
            rich_handler.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    processor=structlog.dev.ConsoleRenderer(colors=True)
                )
            )
            handlers.append(rich_handler)
        except Exception:  # pragma: no cover - rich sempre presente em prod
            stream = logging.StreamHandler(sys.stderr)
            handlers.append(stream)

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(log_level)

    # Bibliotecas barulhentas.
    for noisy in ("asyncio", "urllib3", "httpx", "httpcore", "anthropic", "numba", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


__all__ = ["configure_logging"]
