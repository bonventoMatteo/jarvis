"""
Falas do JARVIS por estado.

Centralizar as frases aqui mantém a personalidade consistente e permite
variar as respostas sem repetir sempre a mesma coisa.
"""
from __future__ import annotations

import random

from config import settings

TITLE = settings.user_title

BOOT: tuple[str, ...] = (
    "Sistemas online. Ao seu dispor.",
    "Todos os sistemas operacionais. Ao seu dispor.",
    "Inicialização concluída. Ao seu dispor.",
)

ACKNOWLEDGE: tuple[str, ...] = (
    "Sim?",
    "Pronto.",
    "Escutando.",
    "Diga.",
    "Às ordens.",
    "Sim, {title}?",
)

WORKING: tuple[str, ...] = (
    "Um momento.",
    "Processando.",
    "Já verifico.",
    "Deixe comigo.",
)

DONE: tuple[str, ...] = (
    "Feito.",
    "Concluído.",
    "Pronto, {title}.",
    "Executado.",
)

NOT_UNDERSTOOD: tuple[str, ...] = (
    "Não captei, {title}.",
    "Desculpe, não entendi.",
    "Não consegui ouvir direito. Repita, por favor.",
)

NO_SPEECH: tuple[str, ...] = (
    "Continuo à disposição.",
    "Sigo aqui.",
)

CONFIRM_QUESTION: tuple[str, ...] = (
    "Confirma, {title}?",
    "Devo prosseguir?",
)

CANCELLED: tuple[str, ...] = (
    "Cancelado.",
    "Operação abortada.",
)

GOODBYE: tuple[str, ...] = (
    "Desligando. Até logo, {title}.",
    "Sistemas offline. Até logo.",
)


def pick(pool: tuple[str, ...], title: str | None = None) -> str:
    """Escolhe uma frase do conjunto e interpola o tratamento do usuário."""
    return random.choice(pool).format(title=title or settings.user_title)


def error_line(reason: str = "", title: str | None = None) -> str:
    """Monta a fala de erro — sempre com o motivo real, nunca inventado."""
    who = title or settings.user_title
    reason = (reason or "").strip().rstrip(".")
    if not reason:
        return f"Desculpe, {who}, não consegui executar."
    return f"Desculpe, {who}, não consegui: {reason}."


__all__ = [
    "ACKNOWLEDGE",
    "BOOT",
    "CANCELLED",
    "CONFIRM_QUESTION",
    "DONE",
    "GOODBYE",
    "NOT_UNDERSTOOD",
    "NO_SPEECH",
    "WORKING",
    "error_line",
    "pick",
]
