"""
Baixa todos os modelos do JARVIS para `models/`.

    python -m scripts.download_models            # tudo
    python -m scripts.download_models --only piper whisper

Componentes:
  * piper   — voz pt_BR (models/piper/<voz>.onnx + .onnx.json)
  * whisper — modelo faster-whisper (models/whisper/)
  * wake    — openWakeWord "hey jarvis" + modelos de features
  * vad     — silero-vad (cache do pacote)
  * assets  — efeitos sonoros sintéticos (assets/*.wav)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from config import MODELS_DIR, settings

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
COMPONENTS = ("assets", "piper", "whisper", "wake", "vad")


def _progress(label: str):
    started = time.monotonic()

    def hook(blocks: int, block_size: int, total: int) -> None:
        done = blocks * block_size
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            bar = "#" * int(pct / 4)
            speed = done / max(time.monotonic() - started, 1e-6) / 1e6
            print(f"\r  {label}: [{bar:<25}] {pct:5.1f}%  {speed:4.1f} MB/s", end="", flush=True)
        else:
            print(f"\r  {label}: {done / 1e6:.1f} MB", end="", flush=True)

    return hook


def _fetch(url: str, target: Path) -> None:
    tmp = target.with_suffix(target.suffix + ".part")
    urllib.request.urlretrieve(url, tmp, _progress(target.name))
    print()
    shutil.move(str(tmp), target)


def piper_url(voice: str, extension: str) -> str:
    """URL do HuggingFace para uma voz no formato `pt_BR-faber-medium`."""
    lang_code, name, quality = voice.split("-")
    family = lang_code.split("_")[0]
    return f"{PIPER_BASE}/{family}/{lang_code}/{name}/{quality}/{voice}{extension}"


def download_piper(voice: str | None = None) -> bool:
    voice = voice or settings.piper_voice
    target_dir = MODELS_DIR / "piper"
    target_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for extension in (".onnx", ".onnx.json"):
        target = target_dir / f"{voice}{extension}"
        if target.exists() and target.stat().st_size > 0:
            print(f"  {target.name}: já existe")
            continue
        try:
            _fetch(piper_url(voice, extension), target)
        except Exception as exc:
            print(f"\n  [erro] {target.name}: {exc}")
            ok = False
    return ok


def download_whisper(model: str | None = None) -> bool:
    model = model or settings.whisper_model
    try:
        from faster_whisper import download_model

        print(f"  faster-whisper {model} (pode levar alguns minutos)...")
        path = download_model(model, cache_dir=str(MODELS_DIR / "whisper"))
        print(f"  ok: {path}")
        return True
    except Exception as exc:
        print(f"  [erro] whisper: {exc}")
        return False


def download_wake() -> bool:
    try:
        from openwakeword.utils import download_models

        download_models(["hey_jarvis"])
        print("  ok: hey_jarvis + modelos de features")
        return True
    except Exception as exc:
        print(f"  [erro] openWakeWord: {exc}")
        return False


def download_vad() -> bool:
    try:
        from silero_vad import load_silero_vad

        load_silero_vad(onnx=False)
        print("  ok: silero-vad")
        return True
    except Exception as exc:
        print(f"  [erro] silero-vad: {exc}")
        return False


def generate_assets() -> bool:
    from assets.generate_assets import ensure_assets

    created = ensure_assets()
    print(f"  {len(created)} som(ns) gerado(s)" if created else "  todos os sons já existem")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Baixa os modelos do JARVIS.")
    parser.add_argument("--only", nargs="+", choices=COMPONENTS, help="baixa só estes componentes")
    args = parser.parse_args(argv)

    steps = {
        "assets": generate_assets,
        "piper": download_piper,
        "whisper": download_whisper,
        "wake": download_wake,
        "vad": download_vad,
    }
    selected = args.only or list(COMPONENTS)
    failures: list[str] = []
    for name in selected:
        print(f"[{name}]")
        if not steps[name]():
            failures.append(name)

    if failures:
        print(f"\nFalharam: {', '.join(failures)}. O JARVIS roda em modo degradado sem eles.")
        return 1
    print("\nTudo pronto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
