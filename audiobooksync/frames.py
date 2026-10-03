"""The frame grid the encoder produces, and the shapes an alignment comes back as.

The grid is not exactly 12.5 frames/s: convolution padding adds a little over two frames per
forward pass, so `frame_time` applies a fitted offset.  Assuming 12.5 would drift by minutes over
a book.  CTC also needs at least one frame per label, so a window with more labels than the audio
has frames cannot be aligned and `align` returns `None`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .mel import DEFAULT_HOP_LENGTH

#: Encoder subsampling factor.
SUBSAMPLING = 8

#: Frame `f` begins at `(f + FRAME_OFFSET) / FRAME_RATE` seconds of audio.
FRAME_RATE = 12.5
FRAME_OFFSET = 0.8

#: Stand-in for log(0) in the Viterbi; real posteriors bottom out around -30.
NEG_INF = -1e30

#: Minimum frames per label.
MIN_FRAMES_PER_LABEL = 1.0

#: Widest silence (frames) allowed between two characters of one word.  A wider gap means the
#: decoder left the word and picked it up elsewhere, which would stretch it over unrelated audio.
MAX_LABEL_GAP = 12


@dataclass
class AlignedWord:
    """One word of the script, located in the audio."""

    index: int  #: index into the word list that was aligned
    text: str
    start: float
    end: float
    label_start: int  #: first CTC label belonging to this word
    label_end: int
    #: Mean log-probability of the frames its labels landed on; near 0 for a real match.
    fit: float = 0.0


@dataclass
class HeardWord:
    """One word of the model's own greedy transcription, with the frames it was heard on."""

    text: str
    first_frame: int
    last_frame: int


@dataclass
class Alignment:
    """Result of aligning one text window against one audio window."""

    words: list[AlignedWord]
    score: float  #: total CTC log-probability of the best path
    frames: int
    labels: int
    fit: float  #: mean log-probability of the frames the labels landed on
    audio_start: float = 0.0

    @property
    def score_per_label(self) -> float:
        return self.score / self.labels if self.labels else float("-inf")


def frame_time(frame: float, offset: float = 0.0) -> float:
    """Audio time in seconds at the start of a CTC output frame."""
    return offset + (frame + FRAME_OFFSET) / FRAME_RATE


def frame_count(num_samples: int) -> int:
    """Encoder output frames for a mono 16 kHz signal of `num_samples` samples (exact)."""
    mel_frames = 1 + num_samples // DEFAULT_HOP_LENGTH
    return (mel_frames + SUBSAMPLING - 1) // SUBSAMPLING


__all__ = [
    "FRAME_OFFSET",
    "FRAME_RATE",
    "MIN_FRAMES_PER_LABEL",
    "NEG_INF",
    "SUBSAMPLING",
    "AlignedWord",
    "Alignment",
    "HeardWord",
    "frame_count",
    "frame_time",
]
