"""
JARVIS — configuração central.

Todas as opções são lidas de variáveis de ambiente / arquivo `.env`
(ver `.env.example`). Importe sempre `from config import settings`.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Caminhos base
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent
ASSETS_DIR: Path = BASE_DIR / "assets"
MODELS_DIR: Path = BASE_DIR / "models"
DATA_DIR: Path = BASE_DIR / "data"
LOGS_DIR: Path = BASE_DIR / "logs"

for _directory in (ASSETS_DIR, MODELS_DIR, DATA_DIR, LOGS_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

IS_WINDOWS: bool = sys.platform == "win32"


class Settings(BaseSettings):
    """Configuração completa do assistente."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # ----------------------------- Identidade ----------------------------- #
    assistant_name: str = Field(default="Jarvis", description="Nome do assistente.")
    user_title: str = Field(default="senhor", description="Como o Jarvis trata o usuário.")
    language: str = Field(default="pt", description="Idioma ISO-639-1 para STT/TTS.")

    # ------------------------------ Anthropic ----------------------------- #
    anthropic_api_key: str = Field(default="", description="Chave da API Anthropic.")
    # Modelos: o roteador rápido usa Haiku; o agente com tool use usa Sonnet.
    # Alternativas mais capazes disponíveis: claude-sonnet-5 / claude-opus-5.
    model_fast: str = Field(default="claude-haiku-4-5")
    model_agent: str = Field(default="claude-sonnet-4-5")
    model_vision: str = Field(default="claude-sonnet-4-5")
    max_tokens_fast: int = Field(default=512, ge=64, le=8192)
    max_tokens_agent: int = Field(default=4096, ge=256, le=32000)
    agent_max_turns: int = Field(default=12, ge=1, le=50)

    # -------------------------------- Áudio ------------------------------- #
    sample_rate: int = Field(default=16000, description="Taxa de amostragem da captura (Hz).")
    channels: int = Field(default=1)
    block_ms: int = Field(default=40, ge=10, le=100, description="Tamanho do bloco de captura (ms).")
    ring_seconds: float = Field(default=30.0, gt=1.0, description="Tamanho do buffer circular (s).")
    input_device: int | None = Field(default=None, description="Índice do dispositivo de entrada.")
    output_device: int | None = Field(default=None, description="Índice do dispositivo de saída.")

    # ----------------------------- Detector de palmas --------------------- #
    clap_enabled: bool = Field(default=True)
    clap_threshold_mult: float = Field(default=8.0, gt=1.0)
    clap_min_gap_ms: int = Field(default=150, ge=50)
    clap_max_gap_ms: int = Field(default=1200, ge=300)
    clap_debounce_ms: int = Field(default=300, ge=0)
    clap_attack_ms: float = Field(default=15.0, gt=0)
    clap_band_ratio: float = Field(default=0.30, gt=0, lt=1)
    clap_band_low_hz: int = Field(default=2000, ge=200)
    clap_band_high_hz: int = Field(default=4000, ge=400)
    clap_min_peak: float = Field(default=0.045, ge=0.0, description="Piso absoluto do pico (0-1).")
    clap_required: int = Field(default=2, ge=2, le=4, description="Palmas necessárias.")
    calibration_seconds: float = Field(default=5.0, gt=0.5)

    # --------------------------------- Wake ------------------------------- #
    wake_enabled: bool = Field(default=True)
    wake_model: str = Field(default="hey_jarvis_v0.1", description="Modelo openWakeWord (nome ou .onnx/.tflite).")
    wake_threshold: float = Field(default=0.55, gt=0, lt=1)
    wake_cooldown_ms: int = Field(default=2000, ge=0)

    # -------------------------------- Hotkey ------------------------------ #
    hotkey_enabled: bool = Field(default=True)
    hotkey: str = Field(default="ctrl+alt+j")

    # ---------------------------------- VAD ------------------------------- #
    vad_threshold: float = Field(default=0.5, gt=0, lt=1)
    vad_silence_ms: int = Field(default=800, ge=200, description="Silêncio que encerra a fala.")
    vad_max_record_s: float = Field(default=20.0, gt=1.0)
    vad_min_speech_ms: int = Field(default=250, ge=50)
    vad_prespeech_ms: int = Field(default=300, ge=0, description="Áudio mantido antes do início da fala.")

    # ---------------------------------- STT ------------------------------- #
    # ATENÇÃO: `distil-large-v3` é somente-inglês. Para pt-BR use
    # `large-v3-turbo` (padrão), `large-v3`, `medium` ou `small`.
    whisper_model: str = Field(default="large-v3-turbo")
    whisper_device: Literal["auto", "cuda", "cpu"] = Field(default="auto")
    whisper_compute_type: str = Field(default="auto")
    whisper_beam_size: int = Field(default=1, ge=1, le=10)
    whisper_vad_filter: bool = Field(default=True)

    # ---------------------------------- TTS ------------------------------- #
    piper_voice: str = Field(default="pt_BR-faber-medium")
    piper_length_scale: float = Field(default=0.95, gt=0.3, lt=3.0)
    piper_noise_scale: float = Field(default=0.667, ge=0.0)
    piper_noise_w: float = Field(default=0.8, ge=0.0)
    tts_effects: bool = Field(default=True, description="Aplica a cadeia pedalboard na voz.")
    tts_highpass_hz: float = Field(default=180.0, ge=20.0)
    tts_reverb_wet: float = Field(default=0.13, ge=0.0, le=1.0)
    tts_reverb_room: float = Field(default=0.22, ge=0.0, le=1.0)
    tts_gain_db: float = Field(default=1.5)
    tts_volume: float = Field(default=1.0, gt=0.0, le=2.0)

    # --------------------------------- Sons -------------------------------- #
    sfx_volume: float = Field(default=0.55, gt=0.0, le=2.0)
    ambient_volume: float = Field(default=0.18, gt=0.0, le=2.0)
    play_boot_sound: bool = Field(default=True)

    # ------------------------------- Executor ------------------------------ #
    allow_shell: bool = Field(default=True)
    allow_destructive: bool = Field(default=False, description="Desligar/reiniciar sem confirmação.")
    shell_timeout_s: int = Field(default=60, ge=1)
    screenshot_dir: Path = Field(default=DATA_DIR / "screenshots")
    confirm_timeout_s: float = Field(default=8.0, gt=1.0)

    # -------------------------------- Browser ------------------------------ #
    browser_headless: bool = Field(default=False)
    browser_channel: str = Field(default="chromium")
    browser_timeout_ms: int = Field(default=25000, ge=1000)

    # -------------------------------- Memória ------------------------------ #
    db_path: Path = Field(default=DATA_DIR / "jarvis.db")
    prefs_path: Path = Field(default=DATA_DIR / "prefs.json")
    history_context_turns: int = Field(default=6, ge=0, le=40)

    # ---------------------------------- UI --------------------------------- #
    dashboard_enabled: bool = Field(default=True)
    dashboard_fps: int = Field(default=12, ge=1, le=30)
    log_level: str = Field(default="INFO")
    log_json_file: bool = Field(default=True)

    # ------------------------------ Validadores ---------------------------- #
    @field_validator("screenshot_dir", "db_path", "prefs_path", mode="after")
    @classmethod
    def _ensure_parent(cls, value: Path) -> Path:
        target = value if value.suffix == "" else value.parent
        target.mkdir(parents=True, exist_ok=True)
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    # ------------------------------ Derivados ------------------------------ #
    @property
    def block_size(self) -> int:
        """Número de amostras por bloco de captura."""
        return int(self.sample_rate * self.block_ms / 1000)

    @property
    def ring_size(self) -> int:
        """Número de amostras do buffer circular."""
        return int(self.sample_rate * self.ring_seconds)

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.startswith("sk-"))

    def sound(self, name: str) -> Path:
        """Caminho de um efeito sonoro em `assets/`."""
        return ASSETS_DIR / f"{name}.wav"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Instância única de `Settings` (cacheada)."""
    return Settings()


settings: Settings = get_settings()

__all__ = [
    "ASSETS_DIR",
    "BASE_DIR",
    "DATA_DIR",
    "IS_WINDOWS",
    "LOGS_DIR",
    "MODELS_DIR",
    "Settings",
    "get_settings",
    "settings",
]
