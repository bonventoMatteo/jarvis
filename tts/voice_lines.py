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

DOUBLE_CONFIRM: tuple[str, ...] = (
    "Tem certeza, {title}? Diga confirmo para prosseguir.",
    "Última verificação: diga confirmo para prosseguir.",
)

CONFIRM_REPEAT: tuple[str, ...] = (
    "Não entendi. Sim ou não?",
    "Preciso de um sim ou um não, {title}.",
)

TIMER_DONE: tuple[str, ...] = (
    "{title}, o timer de {what} terminou.",
    "Tempo esgotado, {title}. Timer de {what} concluído.",
)

ALARM: tuple[str, ...] = (
    "{title}, são {time}. Seu alarme.",
    "Alarme, {title}. São {time}.",
)

REMINDER: tuple[str, ...] = (
    "{title}, lembrete: {what}.",
    "Lembrete, {title}: {what}.",
)

NO_API: tuple[str, ...] = (
    "Esse pedido precisa do meu módulo de raciocínio, e a chave da API não está configurada.",
)

GOODBYE: tuple[str, ...] = (
    "Desligando. Até logo, {title}.",
    "Sistemas offline. Até logo.",
)


def pick(pool: tuple[str, ...], title: str | None = None, **values: str) -> str:
    """Escolhe uma frase do conjunto e interpola o tratamento e os valores."""
    phrase = random.choice(pool).format(title=title or settings.user_title, **values)
    return phrase[:1].upper() + phrase[1:]


def all_fixed_phrases() -> list[str]:
    """Todas as variações sem campos variáveis (para pré-carregar no TTS)."""
    phrases: list[str] = []
    for pool in (ACKNOWLEDGE, WORKING, DONE, NOT_UNDERSTOOD, NO_SPEECH, CONFIRM_QUESTION,
                 CANCELLED, DOUBLE_CONFIRM, CONFIRM_REPEAT):
        for template in pool:
            phrase = template.format(title=settings.user_title)
            phrases.append(phrase[:1].upper() + phrase[1:])
    return phrases


def error_line(reason: str = "", title: str | None = None) -> str:
    """Monta a fala de erro — sempre com o motivo real, nunca inventado."""
    who = title or settings.user_title
    reason = (reason or "").strip().rstrip(".")
    if not reason:
        return f"Desculpe, {who}, não consegui executar."
    return f"Desculpe, {who}, não consegui: {reason}."


__all__ = [
    "ACKNOWLEDGE",
    "ALARM",
    "BOOT",
    "CANCELLED",
    "CONFIRM_QUESTION",
    "CONFIRM_REPEAT",
    "DONE",
    "DOUBLE_CONFIRM",
    "GOODBYE",
    "NOT_UNDERSTOOD",
    "NO_API",
    "NO_SPEECH",
    "REMINDER",
    "TIMER_DONE",
    "WORKING",
    "all_fixed_phrases",
    "error_line",
    "pick",
]
