"""The formats Convert writes, what each of them can hold, and the choices a person makes about them.

Everything about the encoders here was measured on the audio library the app ships (libsndfile 1.2), not assumed, and the tests check it again:

- MP3 is written at a constant bitrate (libsndfile's "variable" setting does not behave like LAME's V0 and V2, as `discs.encode` found), and only at
  32, 44.1 and 48 kHz: it refuses anything above 48 kHz and the lower rates use other bitrate tables, so the names below would be wrong for them.
- Ogg Vorbis is variable-rate; libsndfile's compression level is one minus the quality (0 to 10) divided by ten, so quality 4 (about 128 kbps) is 0.6.
- Opus writes only 8, 12, 16, 24 and 48 kHz (a CD's 44.1 kHz is refused), and its bitrate follows the level and the number of channels: the levels below
  give the stated bitrate for stereo and about half of it for mono.
- FLAC and WAV hold 16 or 24 bits at any rate; libsndfile cannot write AAC.
"""

from __future__ import annotations

from dataclasses import dataclass

MP3_RATES = (32000, 44100, 48000)
OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
MAX_RATES = (0, 48000, 44100)  # what a person may ask for: 0 keeps whatever the song has
DEPTHS = ("16", "keep")
EXISTING = ("copy", "convert", "skip")
FLAC_LEVEL = 0.625  # libsndfile's 0 to 1 scale for FLAC's 0 to 8: level 5, the usual choice


@dataclass(frozen=True)
class Quality:
    id: str
    label: str
    kbps: int  # for the lossy formats, what the quality is called after (stereo); 0 for the lossless ones
    level: float | None  # libsndfile's compression level


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    extension: str
    lossy: bool
    qualities: tuple[Quality, ...]
    default: str
    blurb: str

    def quality(self, quality: str | None) -> Quality:
        wanted = quality or self.default
        for one in self.qualities:
            if one.id == wanted:
                return one
        raise ValueError(f"quality for {self.id} must be one of: {', '.join(q.id for q in self.qualities)}")


FORMATS: dict[str, Format] = {
    "mp3": Format(
        "mp3", "MP3", "mp3", True,
        (Quality("320", "320 kbps", 320, 0.05), Quality("256", "256 kbps", 256, 0.19), Quality("192", "192 kbps", 192, 0.44), Quality("128", "128 kbps", 128, 0.65)),
        "256", "Plays everywhere. Constant bitrate; 32, 44.1 and 48 kHz.",
    ),
    "ogg": Format(
        "ogg", "Ogg Vorbis", "ogg", True,
        (Quality("320", "About 320 kbps", 320, 0.1), Quality("256", "About 256 kbps", 256, 0.2), Quality("192", "About 192 kbps", 192, 0.4),
         Quality("160", "About 160 kbps", 160, 0.5), Quality("128", "About 128 kbps", 128, 0.6), Quality("96", "About 96 kbps", 96, 0.8)),
        "160", "Open and efficient; most players and phones read it. Variable bitrate.",
    ),
    "opus": Format(
        "opus", "Opus", "opus", True,
        (Quality("192", "About 192 kbps", 192, 0.645), Quality("160", "About 160 kbps", 160, 0.707), Quality("128", "About 128 kbps", 128, 0.772),
         Quality("96", "About 96 kbps", 96, 0.835), Quality("64", "About 64 kbps", 64, 0.90)),
        "128", "The best sound for its size; newer players and phones. Written at 48 kHz.",
    ),
    "flac": Format("flac", "FLAC", "flac", False, (Quality("standard", "Lossless", 0, FLAC_LEVEL),), "standard", "Exactly what the song is, in about half the space."),
    "wav": Format("wav", "WAV", "wav", False, (Quality("pcm", "Uncompressed", 0, None),), "pcm", "Uncompressed. Large files, read by everything."),
}


def check_choice(fmt: str, quality: str | None) -> Quality:
    """The quality chosen for a format, or a ValueError that says what is allowed."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    return FORMATS[fmt].quality(quality)


def _capped(rate: int, cap: int) -> int:
    """A rate no higher than `cap`. A song of the 44.1 kHz family (88.2, 176.4 kHz ...) stays in it under a 48 kHz cap."""
    if not cap or rate <= cap:
        return rate
    if cap == 48000 and rate % 44100 == 0:
        return 44100
    return cap


def _mp3_rate(rate: int) -> int:
    if rate in MP3_RATES:
        return rate
    if rate > 48000:
        return 44100 if rate % 44100 == 0 else 48000
    for times in (2, 3, 4, 6):  # a lower rate is raised by a whole number of times when that reaches one MP3 has
        if rate * times in MP3_RATES:
            return rate * times
    return 44100 if rate <= 44100 else 48000


def target_rate(fmt: str, rate: int, max_rate: int = 0) -> int:
    """The sample rate the converted song has: the song's own, unless the person capped it or the format cannot hold it."""
    if max_rate not in MAX_RATES:
        raise ValueError(f"the highest rate must be one of: {', '.join(str(r) for r in MAX_RATES)}")
    rate = _capped(int(rate), max_rate)
    if fmt == "mp3":
        return _mp3_rate(rate)
    if fmt == "opus":
        return rate if rate in OPUS_RATES else 48000
    return rate


def target_bits(fmt: str, depth: str, source_bits: int) -> int:
    """16 or 24 bits for FLAC and WAV: 24 only when the person said to keep the depth and the song has more than 16 bits. 0 for the lossy formats."""
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of: {', '.join(DEPTHS)}")
    if fmt not in ("flac", "wav"):
        return 0
    return 24 if depth == "keep" and source_bits > 16 else 16


def estimate_bytes(fmt: str, quality: str | None, seconds: float, channels: int, rate: int, bits: int) -> int:
    """About how big the converted file will be: exact for WAV, the bitrate times the length for MP3, and a fair guess for the variable ones.
    Meant for 'will it fit'; the plan adds room to spare."""
    chosen = check_choice(fmt, quality)
    seconds = max(float(seconds), 0.0)
    if fmt == "wav":
        return int(seconds * rate * channels * (bits // 8)) + 44
    if fmt == "flac":
        return int(seconds * rate * channels * (bits // 8) * 0.62)
    if fmt == "mp3":
        return int(seconds * chosen.kbps * 125)
    return int(seconds * chosen.kbps * 125 * channels / 2)
