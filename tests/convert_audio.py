"""Audio for the Convert tests: tones that can be measured, and "music" that gives a lossy encoder something to spend bits on."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf


def tones(rate: int, seconds: float, channels: int = 2, hertz: tuple[float, ...] = (440.0, 660.0), level: float = 0.4) -> np.ndarray:
    """Float samples, shape (frames, channels); channel c is a sine at hertz[c % len(hertz)]."""
    t = np.arange(round(rate * seconds)) / rate
    return np.stack([level * np.sin(2 * np.pi * hertz[c % len(hertz)] * t) for c in range(channels)], axis=1).astype(np.float32)


def music(rate: int, seconds: float, channels: int = 2, seed: int = 1) -> np.ndarray:
    """Tones with a moving envelope plus a little noise: what a lossy encoder spends its bits on."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(rate * seconds)) / rate
    mix = sum(0.12 * np.sin(2 * np.pi * f * t + i) * (0.6 + 0.4 * np.sin(2 * np.pi * (0.3 + i / 7) * t)) for i, f in enumerate((220, 330, 440, 660, 880, 1320, 1760, 2640, 3520)))
    mix = mix + 0.02 * rng.standard_normal(len(t))
    both = np.stack([mix, np.roll(mix, 17) * 0.9], axis=1) if channels == 2 else mix[:, None]
    return np.clip(both, -0.99, 0.99).astype(np.float32)


def noise16(rate: int, seconds: float, channels: int = 2, seed: int = 3) -> np.ndarray:
    """Full-range random 16-bit integers: any change to a sample shows."""
    return np.random.default_rng(seed).integers(-32768, 32768, size=(round(rate * seconds), channels), dtype=np.int16)


def left_justified_24(rate: int, seconds: float, channels: int = 2, seed: int = 4) -> np.ndarray:
    """Random 24-bit samples as libsndfile hands them over: left-justified in 32 bits, the low byte empty."""
    values = np.random.default_rng(seed).integers(-(1 << 23), 1 << 23, size=(round(rate * seconds), channels), dtype=np.int64)
    return (values << 8).astype(np.int32)


def write_song(path: Path, samples: np.ndarray, rate: int, subtype: str | None = None, fmt: str | None = None, **options) -> Path:
    sf.write(str(path), samples, rate, subtype=subtype, format=fmt, **options)
    return path


def read(path: Path, dtype: str = "float32") -> tuple[np.ndarray, int]:
    data, rate = sf.read(str(path), dtype=dtype, always_2d=True)
    return data, rate


def peak_hertz(samples: np.ndarray, rate: int) -> float:
    """The frequency that is loudest in a stretch of sound (one channel)."""
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    return float(np.argmax(spectrum) * rate / len(samples))
