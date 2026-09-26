"""
Vinheta de DJ com voz grossa que vibra.

    python -m scripts.dj_voice "Senhoras e senhores... DJ MATTEO!" -o vinheta.wav
    python -m scripts.dj_voice --file scripts/dj_tags.txt -o tags/          # uma vinheta por linha
    python -m scripts.dj_voice --input minha_voz.wav -o vinheta.wav         # trata a SUA gravação

Cadeia (ordem importa):
  voz neural (Edge/ElevenLabs/piper) ou gravação
  → pitch −3 st → EQ (corte 70 Hz, +6 dB em 120 Hz, −3 dB em 400 Hz, +3 dB em 4 kHz)
  → compressor 6:1 → saturação em paralelo
  → camada SUB-OITAVA (a voz −12 st, só abaixo de 160 Hz) — é o que faz vibrar
  → eco rítmico no BPM (1/4 de tempo) → reverb de arena → impacto 808 → limiter.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

SR = 44100


# --------------------------------------------------------------------------- #
# Fonte da voz
# --------------------------------------------------------------------------- #
def synthesize(text: str, engine: str) -> tuple[np.ndarray, int]:
    """Voz crua em float32 mono, usando os motores do JARVIS."""
    from config import settings

    if engine == "elevenlabs":
        from tts.cloud import synthesize_elevenlabs

        return synthesize_elevenlabs(text, "urgent")
    if engine == "edge":
        from tts.cloud import synthesize_edge

        # Mais lento e grave que o padrão do Jarvis: postura de locutor.
        original = (settings.edge_rate, settings.edge_pitch)
        settings.edge_rate, settings.edge_pitch = "-10%", "-12Hz"
        try:
            return synthesize_edge(text, "neutral")
        finally:
            settings.edge_rate, settings.edge_pitch = original
    from tts.piper_engine import PiperEngine

    tts = PiperEngine()
    tts._load_sync()
    if tts.backend != "piper":
        raise RuntimeError("voz piper indisponível; use --engine edge ou --input")
    return tts._synthesize_piper(text, "calm"), tts.sample_rate


def load_file(path: Path) -> tuple[np.ndarray, int]:
    from pedalboard.io import AudioFile

    with AudioFile(str(path)) as handle:
        audio = handle.read(handle.frames)
        rate = int(handle.samplerate)
    return (audio.mean(axis=0) if audio.ndim == 2 else audio).astype(np.float32), rate


def resample(audio: np.ndarray, rate: int, target: int = SR) -> np.ndarray:
    if rate == target or audio.size == 0:
        return audio.astype(np.float32)
    from scipy.signal import resample_poly

    divisor = np.gcd(rate, target)
    return resample_poly(audio, target // divisor, rate // divisor).astype(np.float32)


# --------------------------------------------------------------------------- #
# Efeitos
# --------------------------------------------------------------------------- #
def impact(seconds: float = 1.6, level: float = 0.9) -> np.ndarray:
    """Boom 808: seno que despenca de 110 para 38 Hz + clique de ataque."""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    freq = 38.0 + 72.0 * np.exp(-t * 9.0)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 2.4)
    body = np.tanh(body * 2.2) / np.tanh(2.2)  # saturação: harmônicos que aparecem em caixa pequena
    click = np.random.default_rng(3).normal(0, 1, n) * np.exp(-t * 180.0) * 0.25
    return ((body + click) * level).astype(np.float32)


def dj_chain(
    voice: np.ndarray,
    *,
    pitch: float = -3.0,
    sub: float = 0.55,
    bpm: float = 128.0,
    echo: float = 0.28,
    reverb: float = 0.16,
    drive: float = 0.35,
    with_impact: bool = True,
) -> np.ndarray:
    """Aplica a cadeia completa numa voz mono a 44,1 kHz."""
    from pedalboard import (
        Compressor,
        Delay,
        Distortion,
        Gain,
        HighpassFilter,
        Limiter,
        LowpassFilter,
        LowShelfFilter,
        PeakFilter,
        Pedalboard,
        PitchShift,
        Reverb,
    )

    # 1. Corpo da voz: tom mais baixo, peito em 120 Hz, sem lama, com presença.
    body = Pedalboard(
        [
            *([PitchShift(semitones=pitch)] if pitch else []),
            HighpassFilter(cutoff_frequency_hz=70.0),
            LowShelfFilter(cutoff_frequency_hz=140.0, gain_db=4.0, q=0.7),
            PeakFilter(cutoff_frequency_hz=120.0, gain_db=6.0, q=0.9),
            PeakFilter(cutoff_frequency_hz=400.0, gain_db=-3.0, q=1.2),
            PeakFilter(cutoff_frequency_hz=4000.0, gain_db=3.0, q=1.0),
            Compressor(threshold_db=-22.0, ratio=6.0, attack_ms=5.0, release_ms=120.0),
            Gain(gain_db=4.0),
        ]
    )
    dry = body(voice.reshape(1, -1), SR)[0]

    # 2. Saturação em paralelo: calor de rádio sem perder a inteligibilidade.
    if drive > 0:
        crushed = Pedalboard([Distortion(drive_db=18.0), LowpassFilter(cutoff_frequency_hz=6000.0)])(
            dry.reshape(1, -1), SR
        )[0]
        dry = dry * (1 - drive * 0.5) + crushed * drive * 0.5

    # 3. Sub-oitava: a mesma fala uma oitava abaixo, só o grave. É o que VIBRA.
    if sub > 0:
        low = Pedalboard(
            [
                PitchShift(semitones=-12.0),
                LowpassFilter(cutoff_frequency_hz=160.0),
                HighpassFilter(cutoff_frequency_hz=35.0),
                Compressor(threshold_db=-25.0, ratio=4.0, attack_ms=10.0, release_ms=150.0),
            ]
        )(voice.reshape(1, -1), SR)[0]
        peak = float(np.max(np.abs(low))) or 1.0
        dry = dry + low / peak * float(np.max(np.abs(dry))) * sub

    # 4. Espaço: eco no tempo da música + arena. Cauda de 3 s para o eco morrer.
    tail = np.zeros(int(SR * 3.0), dtype=np.float32)
    signal = np.concatenate([dry, tail])
    beat = 60.0 / max(bpm, 40.0)
    space = Pedalboard(
        [
            Delay(delay_seconds=beat / 2, feedback=0.38, mix=echo),
            Reverb(room_size=0.85, damping=0.4, wet_level=reverb, dry_level=1.0, width=1.0),
        ]
    )
    signal = space(signal.reshape(1, -1), SR)[0]

    # 5. Impacto 808 junto com a primeira palavra.
    if with_impact:
        hit = impact()
        start = int(SR * 0.05)
        pre = np.zeros(start, dtype=np.float32)
        signal = np.concatenate([pre, signal])
        signal[: hit.size] += hit * (float(np.max(np.abs(signal))) or 1.0) * 0.8

    # 6. Masterização: volume alto e sem clipar.
    master = Pedalboard([Gain(gain_db=2.0), Limiter(threshold_db=-1.0, release_ms=60.0)])
    out = master(signal.reshape(1, -1), SR)[0]
    # O Limiter não é true-peak: garante teto de -0,5 dBFS (sem clipar em nenhum player).
    peak = float(np.max(np.abs(out))) or 1.0
    out = out * min(1.0, 0.944 / peak)
    # Corta o silêncio final (depois que o eco morreu).
    loud = np.nonzero(np.abs(out) > 0.003)[0]
    if loud.size:
        out = out[: min(out.size, loud[-1] + int(SR * 0.3))]
    return out.astype(np.float32)


def write_wav(path: Path, audio: np.ndarray) -> None:
    from pedalboard.io import AudioFile

    path.parent.mkdir(parents=True, exist_ok=True)
    stereo = np.vstack([audio, audio])
    with AudioFile(str(path), "w", SR, 2, bit_depth=24) as handle:
        handle.write(stereo)


def slug(text: str) -> str:
    folded = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return folded[:40] or "vinheta"


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera vinhetas de DJ com voz grave.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("text", nargs="?", help="texto da vinheta")
    source.add_argument("--file", type=Path, help="arquivo com uma vinheta por linha")
    source.add_argument("--input", type=Path, help="trata uma gravação sua (wav/mp3/flac)")
    parser.add_argument("-o", "--output", type=Path, default=Path("vinheta.wav"),
                        help="arquivo .wav (ou pasta, com --file)")
    parser.add_argument("--engine", choices=["edge", "elevenlabs", "piper"], default="edge")
    parser.add_argument("--pitch", type=float, default=-3.0, help="semitons (padrão -3)")
    parser.add_argument("--sub", type=float, default=0.55, help="camada sub-oitava 0-1 (padrão 0.55)")
    parser.add_argument("--bpm", type=float, default=128.0, help="BPM do eco (padrão 128)")
    parser.add_argument("--echo", type=float, default=0.28, help="quantidade de eco 0-1")
    parser.add_argument("--reverb", type=float, default=0.16, help="quantidade de reverb 0-1")
    parser.add_argument("--drive", type=float, default=0.35, help="saturação 0-1")
    parser.add_argument("--no-impact", action="store_true", help="sem o boom 808 no início")
    args = parser.parse_args(argv)

    chain = {
        "pitch": args.pitch, "sub": args.sub, "bpm": args.bpm, "echo": args.echo,
        "reverb": args.reverb, "drive": args.drive, "with_impact": not args.no_impact,
    }
    jobs: list[tuple[str | None, Path]] = []
    if args.file:
        lines = [line.strip() for line in args.file.read_text(encoding="utf-8").splitlines()]
        lines = [line for line in lines if line and not line.startswith("#")]
        for index, line in enumerate(lines, 1):
            jobs.append((line, args.output / f"{index:02d}_{slug(line)}.wav"))
    else:
        jobs.append((args.text, args.output))

    for text, target in jobs:
        try:
            if args.input:
                voice, rate = load_file(args.input)
            else:
                assert text is not None
                voice, rate = synthesize(text, args.engine)
        except Exception as exc:
            print(f"[erro] {text or args.input}: {exc}")
            return 1
        audio = dj_chain(resample(voice, rate), **chain)
        write_wav(target, audio)
        print(f"ok: {target}  ({audio.size / SR:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
