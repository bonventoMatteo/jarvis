"""
Baixa a voz Piper pt_BR-faber-medium para `models/`.

    python download_voice.py

Tenta primeiro o downloader oficial do piper (`piper.download_voices`) e, se
ele falhar, baixa direto do HuggingFace com barra de progresso.
"""
from __future__ import annotations

import shutil
import sys
import time
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    # Executado como script de dentro da pasta: torna o pacote importável.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_voice.config import DEFAULT_MODEL, HF_BASE_URL, MODELS_DIR


def voice_urls(voice: str = DEFAULT_MODEL) -> tuple[str, str]:
    """URLs do `.onnx` e do `.onnx.json` de uma voz `lang_REGION-nome-qualidade`."""
    lang_code, name, quality = voice.split("-")
    family = lang_code.split("_")[0]
    base = f"{HF_BASE_URL}/{family}/{lang_code}/{name}/{quality}/{voice}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _is_complete(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _progress_hook(label: str):
    started = time.monotonic()

    def hook(blocks: int, block_size: int, total: int) -> None:
        done = blocks * block_size
        elapsed = max(time.monotonic() - started, 1e-6)
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            bar = "#" * int(pct / 4)
            print(f"\r{label}: [{bar:<25}] {pct:5.1f}%  {done / elapsed / 1e6:4.1f} MB/s", end="", flush=True)
        else:
            print(f"\r{label}: {done / 1e6:.1f} MB", end="", flush=True)

    return hook


def _download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    urllib.request.urlretrieve(url, partial, _progress_hook(target.name))
    print()
    shutil.move(str(partial), target)


def download_with_piper(voice: str, target_dir: Path) -> bool:
    """Usa o downloader oficial do piper-tts. Devolve False se não der."""
    try:
        from piper.download_voices import download_voice
    except ImportError:
        return False
    try:
        print(f"Baixando {voice} com piper.download_voices...")
        download_voice(voice, target_dir)
    except Exception as exc:
        print(f"piper.download_voices falhou ({exc}); tentando download direto.")
        return False
    return all(_is_complete(target_dir / f"{voice}{ext}") for ext in (".onnx", ".onnx.json"))


def download_direct(voice: str, target_dir: Path) -> None:
    """Baixa direto do HuggingFace (fallback)."""
    for url in voice_urls(voice):
        target = target_dir / url.rsplit("/", 1)[-1]
        if _is_complete(target):
            print(f"{target.name}: já existe")
            continue
        _download(url, target)


def main(voice: str = DEFAULT_MODEL, target_dir: Path = MODELS_DIR) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [target_dir / f"{voice}{ext}" for ext in (".onnx", ".onnx.json")]
    if all(_is_complete(path) for path in files):
        print(f"Voz {voice} já está em {target_dir}.")
        return 0

    if not download_with_piper(voice, target_dir):
        try:
            download_direct(voice, target_dir)
        except Exception as exc:
            print(f"\nFalha no download: {exc}")
            print("Baixe manualmente estes arquivos para a pasta models/:")
            for url in voice_urls(voice):
                print(f"  {url}")
            return 1

    for path in files:
        print(f"ok: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
