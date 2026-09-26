"""
Controle de mídia, volume (pycaw) e brilho (screen-brightness-control).

As teclas de mídia usam `pyautogui`, que envia os virtual-keys nativos
(`playpause`, `nexttrack`, `prevtrack`) reconhecidos por Spotify, YouTube,
navegadores e players em geral.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class MediaResult:
    """Resultado de uma operação de mídia."""

    ok: bool
    message: str
    value: float | None = None


# --------------------------------------------------------------------------- #
# Volume — pycaw (Core Audio)
# --------------------------------------------------------------------------- #
_volume_interface = None


def _get_volume():  # noqa: ANN202
    """Obtém (e memoiza) a interface `IAudioEndpointVolume` do dispositivo padrão."""
    global _volume_interface
    if _volume_interface is not None:
        return _volume_interface
    from comtypes import CLSCTX_ALL
    from ctypes import POINTER, cast

    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    _volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
    return _volume_interface


def get_volume() -> MediaResult:
    """Lê o volume atual do sistema (0–100)."""
    try:
        level = _get_volume().GetMasterVolumeLevelScalar()
        percent = round(level * 100)
        return MediaResult(True, f"volume em {percent} por cento", float(percent))
    except Exception as exc:
        log.warning("media.get_volume_failed", error=str(exc))
        return MediaResult(False, f"não consegui ler o volume: {exc}")


def set_volume(percent: float) -> MediaResult:
    """Define o volume do sistema (0–100)."""
    percent = max(0.0, min(100.0, float(percent)))
    try:
        volume = _get_volume()
        volume.SetMute(0, None)
        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
        log.info("media.volume_set", percent=percent)
        return MediaResult(True, f"volume em {round(percent)} por cento", percent)
    except Exception as exc:
        log.warning("media.set_volume_failed", error=str(exc))
        return MediaResult(False, f"não consegui ajustar o volume: {exc}")


def change_volume(delta: float) -> MediaResult:
    """Soma `delta` pontos percentuais ao volume atual."""
    current = get_volume()
    if not current.ok or current.value is None:
        return current
    return set_volume(current.value + delta)


def set_mute(muted: bool) -> MediaResult:
    """Silencia ou reativa o áudio do sistema."""
    try:
        _get_volume().SetMute(1 if muted else 0, None)
        return MediaResult(True, "áudio mudo" if muted else "áudio reativado")
    except Exception as exc:
        return MediaResult(False, f"não consegui alterar o mudo: {exc}")


def toggle_mute() -> MediaResult:
    """Inverte o estado de mudo."""
    try:
        volume = _get_volume()
        muted = bool(volume.GetMute())
        volume.SetMute(0 if muted else 1, None)
        return MediaResult(True, "áudio reativado" if muted else "áudio mudo")
    except Exception as exc:
        return MediaResult(False, f"não consegui alterar o mudo: {exc}")


# --------------------------------------------------------------------------- #
# Teclas de mídia
# --------------------------------------------------------------------------- #
def _press(key: str) -> None:
    import pyautogui

    pyautogui.press(key)


def play_pause() -> MediaResult:
    """Alterna play/pause da mídia ativa."""
    try:
        _press("playpause")
        return MediaResult(True, "play/pause")
    except Exception as exc:
        return MediaResult(False, f"não consegui enviar play/pause: {exc}")


def next_track() -> MediaResult:
    """Pula para a próxima faixa."""
    try:
        _press("nexttrack")
        return MediaResult(True, "próxima faixa")
    except Exception as exc:
        return MediaResult(False, f"não consegui pular a faixa: {exc}")


def previous_track() -> MediaResult:
    """Volta para a faixa anterior."""
    try:
        _press("prevtrack")
        return MediaResult(True, "faixa anterior")
    except Exception as exc:
        return MediaResult(False, f"não consegui voltar a faixa: {exc}")


def stop_media() -> MediaResult:
    """Para a reprodução."""
    try:
        _press("stop")
        return MediaResult(True, "reprodução parada")
    except Exception as exc:
        return MediaResult(False, f"não consegui parar a mídia: {exc}")


# --------------------------------------------------------------------------- #
# Brilho
# --------------------------------------------------------------------------- #
def get_brightness() -> MediaResult:
    """Lê o brilho do monitor principal (0–100)."""
    try:
        import screen_brightness_control as sbc

        values = sbc.get_brightness()
        level = float(values[0]) if values else 0.0
        return MediaResult(True, f"brilho em {round(level)} por cento", level)
    except Exception as exc:
        log.warning("media.get_brightness_failed", error=str(exc))
        return MediaResult(False, f"não consegui ler o brilho: {exc}")


def set_brightness(percent: float) -> MediaResult:
    """Define o brilho (0–100). Requer monitor com suporte a DDC/CI ou notebook."""
    percent = max(0.0, min(100.0, float(percent)))
    try:
        import screen_brightness_control as sbc

        sbc.set_brightness(int(percent))
        log.info("media.brightness_set", percent=percent)
        return MediaResult(True, f"brilho em {round(percent)} por cento", percent)
    except Exception as exc:
        log.warning("media.set_brightness_failed", error=str(exc))
        return MediaResult(
            False,
            "não consegui ajustar o brilho — o monitor pode não suportar controle por software",
        )


def change_brightness(delta: float) -> MediaResult:
    """Soma `delta` pontos percentuais ao brilho atual."""
    current = get_brightness()
    if not current.ok or current.value is None:
        return current
    return set_brightness(current.value + delta)


__all__ = [
    "MediaResult",
    "change_brightness",
    "change_volume",
    "get_brightness",
    "get_volume",
    "next_track",
    "play_pause",
    "previous_track",
    "set_brightness",
    "set_mute",
    "set_volume",
    "stop_media",
    "toggle_mute",
]
