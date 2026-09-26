"""
Demonstração da voz do Jarvis.

    python demo.py

1. Pré-carrega as frases comuns (mostra o tempo — na 2ª execução vem do cache).
2. Fala 5 frases com moods diferentes.
3. Modo interativo: digite um texto para ouvir; "sair" encerra.
   Prefixe com o mood para testar: "urgent: Alerta de intrusão."
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_voice import COMMON_PHRASES, MOODS, JarvisVoice

DEMO_LINES: list[tuple[str, str]] = [
    ("Sistemas online. Ao seu dispor.", "neutral"),
    ("Executando comando.", "confirm"),
    ("Alerta detectado, senhor.", "urgent"),
    ("Tudo tranquilo por aqui.", "calm"),
    ("Missão concluída com sucesso.", "neutral"),
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        voice = JarvisVoice()
    except FileNotFoundError as exc:
        print(exc)
        return 1

    started = time.perf_counter()
    ready = voice.preload(COMMON_PHRASES)
    print(f"Pré-carregadas {ready}/{len(COMMON_PHRASES)} frases em {(time.perf_counter() - started) * 1000:.0f} ms")

    started = time.perf_counter()
    voice.render("Sim senhor.")
    print(f"Frase em cache: {(time.perf_counter() - started) * 1000:.2f} ms\n")

    for text, mood in DEMO_LINES:
        print(f"[{mood:>7}] {text}")
        voice.speak(text, mood=mood)
        time.sleep(0.25)

    print("\nDigite um texto para o Jarvis falar ('sair' para encerrar).")
    print(f"Moods: {', '.join(MOODS)} — ex.: 'calm: Boa noite, senhor.'")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() == "sair":
            break
        mood = "neutral"
        head, sep, tail = line.partition(":")
        if sep and head.strip().lower() in MOODS:
            mood, line = head.strip().lower(), tail.strip()
        started = time.perf_counter()
        audio = voice.render(line, mood)
        print(f"  ({(time.perf_counter() - started) * 1000:.0f} ms, {audio.size / voice.sample_rate:.1f} s de áudio)")
        voice.speak(line, mood=mood)
    return 0


if __name__ == "__main__":
    sys.exit(main())
