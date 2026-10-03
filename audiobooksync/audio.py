"""Audio I/O: ffmpeg decodes to mono 16 kHz float32 (what the feature extractor expects), and
ffprobe supplies the duration."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Sample rate every decode is resampled to.
SAMPLE_RATE = 16000

AUDIO_EXTENSIONS = {
    ".m4b",
    ".m4a",
    ".mp3",
    ".mp4",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".wav",
    ".wma",
    ".mka",
}

EBOOK_EXTENSIONS = {".epub"}


class AudioError(RuntimeError):
    """Raised when ffmpeg or ffprobe cannot do what was asked."""


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is not on PATH")
    if shutil.which("ffprobe") is None:
        raise AudioError("ffprobe is not on PATH")


@dataclass
class AudioInfo:
    duration: float


def probe(path: str | Path) -> AudioInfo:
    """Read the duration of an audio file via ffprobe."""
    _require_ffmpeg()
    path = Path(path)
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioError(f"ffprobe failed on {path.name}: {proc.stderr.decode(errors='replace').strip()}")
    data = json.loads(proc.stdout or b"{}")

    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if audio_stream is None:
        raise AudioError(f"{path.name} has no audio stream")

    duration = 0.0
    for source in (data.get("format", {}), audio_stream):
        for key in ("duration", "DURATION"):
            raw = source.get(key)
            if raw:
                try:
                    duration = max(duration, float(raw))
                except (TypeError, ValueError):
                    pass
    if duration <= 0:
        raise AudioError(f"could not determine the duration of {path.name}")

    return AudioInfo(duration=duration)


def decode(
    path: str | Path,
    start: float = 0.0,
    duration: float | None = None,
    sample_rate: int = SAMPLE_RATE,
):
    """Decode a span to mono float32 at `sample_rate`.

    With a duration the array has exactly ``round(duration * sample_rate)`` samples: ffmpeg can
    return a few fewer after a seek, which would shift every later timestamp.
    """
    import numpy as np

    _require_ffmpeg()
    if duration is not None and duration <= 0:
        raise AudioError("duration must be positive")

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioError(
            f"ffmpeg failed decoding {Path(path).name} at {start:.2f}s: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )

    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if duration is not None:
        want = int(round(duration * sample_rate))
        if audio.size < want:
            audio = np.pad(audio, (0, want - audio.size))
        elif audio.size > want:
            audio = audio[:want]
    return audio


def find_audio_files(folder: str | Path) -> list[Path]:
    """Audio files directly inside `folder`, in natural (name) order."""
    folder = Path(folder)
    found = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(found, key=lambda p: p.name.lower())


def find_epub_files(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    found = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in EBOOK_EXTENSIONS
    ]
    return sorted(found, key=lambda p: p.name.lower())
