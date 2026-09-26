"""Constantes do módulo de voz do Jarvis."""
from __future__ import annotations

from pathlib import Path

#: Voz Piper padrão (pt-BR, masculina, qualidade média).
DEFAULT_MODEL: str = "pt_BR-faber-medium"

#: Pasta onde `download_voice.py` grava o modelo (.onnx + .onnx.json).
MODELS_DIR: Path = Path(__file__).parent / "models"

#: Cache das frases já sintetizadas (.npy, float32 com efeitos aplicados).
CACHE_DIR: Path = Path(__file__).parent / "cache"

#: Taxa de amostragem das vozes Piper "medium".
SAMPLE_RATE: int = 22050

#: URL base das vozes oficiais no HuggingFace.
HF_BASE_URL: str = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def default_model_path() -> Path:
    """Caminho do .onnx da voz padrão."""
    return MODELS_DIR / f"{DEFAULT_MODEL}.onnx"


__all__ = ["CACHE_DIR", "DEFAULT_MODEL", "HF_BASE_URL", "MODELS_DIR", "SAMPLE_RATE", "default_model_path"]
