"""Detector de palmas com sinais sintéticos."""
from __future__ import annotations

import numpy as np
from scipy import signal as sp_signal

from audio.clap import ClapDetector

SR = 16000
BLOCK = 640  # 40 ms
RNG = np.random.default_rng(42)


def _ambient(seconds: float, level: float = 0.003) -> np.ndarray:
    return (RNG.normal(0.0, level, int(SR * seconds))).astype(np.float32)


def _clap(peak: float = 0.6) -> np.ndarray:
    """Ruído filtrado em 1,5–5 kHz com ataque instantâneo e decaimento de ~12 ms."""
    n = int(SR * 0.08)
    noise = RNG.normal(0.0, 1.0, n)
    sos = sp_signal.butter(4, [1500, 5000], btype="bandpass", fs=SR, output="sos")
    band = sp_signal.sosfilt(sos, noise)
    env = np.exp(-np.arange(n) / (SR * 0.012))
    out = band * env
    return (out / np.max(np.abs(out)) * peak).astype(np.float32)


def _voice(seconds: float = 0.4, peak: float = 0.6) -> np.ndarray:
    """Vogal sintética: fundamental 140 Hz + harmônicos graves, ataque lento."""
    t = np.arange(int(SR * seconds)) / SR
    wave = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 6))
    env = np.minimum(1.0, t / 0.06) * np.minimum(1.0, (seconds - t) / 0.05)
    out = wave * env
    return (out / np.max(np.abs(out)) * peak).astype(np.float32)


def _feed(detector: ClapDetector, audio: np.ndarray, start_ms: float = 0.0) -> list[float]:
    """Alimenta em blocos de 40 ms; devolve os instantes (ms) em que ativou."""
    fired: list[float] = []
    for index in range(0, audio.size - BLOCK + 1, BLOCK):
        now = start_ms + index / SR * 1000.0
        if detector.process(audio[index : index + BLOCK], now):
            fired.append(now)
    return fired


def _calibrated() -> ClapDetector:
    detector = ClapDetector(SR)
    detector.calibrate(_ambient(5.0))
    return detector


def test_calibration_sets_noise_floor() -> None:
    detector = _calibrated()
    assert 0.001 < detector.noise_floor < 0.02
    assert detector.threshold >= detector.noise_floor * detector.threshold_mult


def test_single_clap_is_recognized() -> None:
    detector = _calibrated()
    analysis = detector.analyze(np.concatenate([_ambient(0.01), _clap(), _ambient(0.01)]))
    assert analysis.is_clap, analysis.reason


def test_voice_is_rejected() -> None:
    detector = _calibrated()
    analysis = detector.analyze(_voice()[:1280])
    assert not analysis.is_clap


def test_double_clap_activates_once() -> None:
    detector = _calibrated()
    audio = np.concatenate([_ambient(0.3), _clap(), _ambient(0.4), _clap(), _ambient(0.8)])
    assert len(_feed(detector, audio)) == 1


def test_single_clap_does_not_activate() -> None:
    detector = _calibrated()
    audio = np.concatenate([_ambient(0.3), _clap(), _ambient(2.0)])
    assert _feed(detector, audio) == []


def test_claps_too_far_apart_do_not_activate() -> None:
    detector = _calibrated()
    audio = np.concatenate([_ambient(0.3), _clap(), _ambient(2.0), _clap(), _ambient(0.5)])
    assert _feed(detector, audio) == []


def test_speech_does_not_activate() -> None:
    detector = _calibrated()
    audio = np.concatenate([_ambient(0.2), _voice(), _ambient(0.2), _voice(), _ambient(0.5)])
    assert _feed(detector, audio) == []
