"""The playing track's audio format, from Kodi's MusicPlayer info labels (pure, testable).

"FLAC · 24-bit / 96 kHz", "MP3 · 320 kbps · 44.1 kHz", "DSD64 · 2.8 MHz", …
"""
from __future__ import annotations

LABELS = ["MusicPlayer.Codec", "MusicPlayer.BitRate", "MusicPlayer.SampleRate",
          "MusicPlayer.BitsPerSample", "MusicPlayer.Channels"]

_NAMES = {"flac": "FLAC", "alac": "ALAC", "mp3": "MP3", "mp3float": "MP3", "mp2": "MP2",
          "aac": "AAC", "aac_latm": "AAC", "vorbis": "Ogg Vorbis", "opus": "Opus",
          "wmav2": "WMA", "wmapro": "WMA Pro", "wmalossless": "WMA Lossless",
          "wavpack": "WavPack", "ape": "Monkey's Audio", "tta": "TTA", "ac3": "Dolby Digital",
          "eac3": "Dolby Digital Plus", "truehd": "Dolby TrueHD", "dts": "DTS",
          "dtshd_ma": "DTS-HD MA", "mlp": "MLP", "musepack7": "Musepack",
          "musepack8": "Musepack", "cdda": "CD Audio"}
_LOSSLESS = {"flac", "alac", "wavpack", "ape", "tta", "wmalossless", "truehd", "mlp",
             "dtshd_ma", "cdda"}
_CHANNELS = {1: "Mono", 3: "2.1", 6: "5.1", 8: "7.1"}


def _num(text) -> float:
    try:
        return float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _khz(value: float) -> float:
    """Kodi gives kHz ("44.1"; DSD "2822.4"); older versions gave Hz ("44100")."""
    return value / 1000 if value >= 8000 else value


def _fmt_khz(khz: float) -> str:
    if khz >= 1000:                         # DSD rates: 2822.4 kHz
        return f"{khz / 1000:.1f} MHz"
    return f"{khz:g} kHz" if khz != int(khz) else f"{int(khz)} kHz"


def codec_name(codec: str) -> str:
    c = (codec or "").strip().lower()
    if not c:
        return ""
    if c.startswith("pcm"):
        return "PCM"
    if c.startswith("dsd"):
        return "DSD"
    return _NAMES.get(c, c.upper())


def describe(labels: dict | None) -> str:
    """One line for the player, or "" when Kodi reported nothing useful."""
    labels = labels or {}
    raw = (labels.get("MusicPlayer.Codec") or "").strip().lower()
    name = codec_name(raw)
    khz = _khz(_num(labels.get("MusicPlayer.SampleRate")))
    bits = int(_num(labels.get("MusicPlayer.BitsPerSample")))
    kbps = int(_num(labels.get("MusicPlayer.BitRate")))
    channels = int(_num(labels.get("MusicPlayer.Channels")))
    parts = []
    if name == "DSD" and khz:
        parts.append(f"DSD{int(round(khz / 44.1))}")   # 2822.4 kHz → DSD64
        parts.append(_fmt_khz(khz))
    else:
        if name:
            parts.append(name)
        lossless = raw in _LOSSLESS or raw.startswith("pcm")
        if lossless or (not kbps and bits):
            if bits and khz:
                parts.append(f"{bits}-bit / {_fmt_khz(khz)}")
            elif khz:
                parts.append(_fmt_khz(khz))
        else:
            if kbps:
                parts.append(f"{kbps} kbps")
            if khz:
                parts.append(_fmt_khz(khz))
    if channels in _CHANNELS:
        parts.append(_CHANNELS[channels])
    elif channels > 2:
        parts.append(f"{channels} channels")
    return " · ".join(parts)


__all__ = ["LABELS", "describe", "codec_name"]
