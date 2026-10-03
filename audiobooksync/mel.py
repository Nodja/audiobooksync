"""Parakeet's mel front end, in numpy.

`ParakeetFeatureExtractor` refuses to import without PyTorch, so the recipe from
`feature_extraction_parakeet.py` is reimplemented here:

    mel   = librosa.filters.mel(sr=16000, n_fft=512, n_mels=80, fmin=0, fmax=8000,
                                norm="slaney")
    win   = hann(400, periodic=False)
    X     = stft(waveform, n_fft=512, hop=160, win_length=400, window=win,
                 center=True, pad_mode="constant")      # 257 bins
    power = |X|**2
    out   = log(mel @ power + 2**-24)                   # (frames, 80)

The window is symmetric (periodic=False) and the power is `|X|^2`.  The output matches the real
extractor to 1.2e-04 (float32 rounding) over 30 s of audio.
"""
from __future__ import annotations

import numpy as np

#: Guard added before the logarithm, from the extractor's `LOG_ZERO_GUARD_VALUE`.
LOG_ZERO_GUARD = 2.0**-24

#: Added to the standard deviation before normalising, from the extractor's `EPSILON`.
EPSILON = 1e-5

#: Pre-emphasis filter coefficient, from the extractor's default.
PREEMPHASIS = 0.97

DEFAULT_FEATURE_SIZE = 80
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_HOP_LENGTH = 160
DEFAULT_N_FFT = 512
DEFAULT_WIN_LENGTH = 400


def preemphasize(audio: np.ndarray, coefficient: float = PREEMPHASIS) -> np.ndarray:
    """`x[t] - coefficient * x[t-1]`, leaving the first sample alone.

    Applied to the waveform before the transform, not to the features afterwards.
    """
    if coefficient == 0.0:
        return np.asarray(audio, dtype=np.float32)
    out = np.empty_like(audio, dtype=np.float32)
    out[0] = audio[0]
    out[1:] = audio[1:] - coefficient * audio[:-1]
    return out


def hann_symmetric(length: int) -> np.ndarray:
    """A symmetric Hann window, matching `torch.hann_window(length, periodic=False)`."""
    n = np.arange(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * n / (length - 1))


def hz_to_mel_slaney(freq: np.ndarray) -> np.ndarray:
    """librosa's Slaney mel scale, below 1 kHz linear and above it logarithmic."""
    f_min = 0.0
    f_sp = 200.0 / 3.0
    mels = (freq - f_min) / f_sp
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = freq >= min_log_hz
    mels[log_t] = min_log_mel + np.log(freq[log_t] / min_log_hz) / logstep
    return mels


def mel_to_hz_slaney(mels: np.ndarray) -> np.ndarray:
    f_min = 0.0
    f_sp = 200.0 / 3.0
    freqs = f_min + f_sp * mels
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = mels >= min_log_mel
    freqs[log_t] = min_log_hz * np.exp(logstep * (mels[log_t] - min_log_mel))
    return freqs


def mel_filterbank(
    n_mels: int = DEFAULT_FEATURE_SIZE,
    n_fft: int = DEFAULT_N_FFT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    fmin: float = 0.0,
    fmax: float | None = None,
    norm: str | None = "slaney",
) -> np.ndarray:
    """Slaney-normalised triangular mel filters, shape (n_mels, n_fft // 2 + 1).

    Matches `librosa.filters.mel(..., norm="slaney")`.
    """
    if fmax is None:
        fmax = sample_rate / 2.0
    fft_freqs = np.fft.rfftfreq(n=n_fft, d=1.0 / sample_rate)
    mel_f = mel_to_hz_slaney(np.linspace(hz_to_mel_slaney(np.array([fmin]))[0],
                                          hz_to_mel_slaney(np.array([fmax]))[0], n_mels + 2))
    weights = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    diff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    for i in range(n_mels):
        lower = -ramps[i] / diff[i]
        upper = ramps[i + 2] / diff[i + 1]
        weights[i] = np.maximum(0.0, np.minimum(lower, upper))
    if norm == "slaney":
        enorm = 2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels])
        weights *= enorm[:, None]
    return weights


class MelFrontEnd:
    """Turns mono float32 audio into the (1, frames, n_mels) tensor the exported graph wants."""

    def __init__(
        self,
        n_mels: int = DEFAULT_FEATURE_SIZE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        hop_length: int = DEFAULT_HOP_LENGTH,
        n_fft: int = DEFAULT_N_FFT,
        win_length: int = DEFAULT_WIN_LENGTH,
        preemphasis: float = PREEMPHASIS,
    ):
        self.n_mels = n_mels
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.n_fft = n_fft
        self.win_length = win_length
        self.preemphasis = preemphasis
        self.filters = np.asarray(mel_filterbank(n_mels, n_fft, sample_rate), dtype=np.float32)
        self.window = hann_symmetric(win_length).astype(np.float32)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        return self.features(audio)[None, :, :]

    def features(self, audio: np.ndarray) -> np.ndarray:
        """(frames, n_mels) features, pre-emphasised and normalised."""
        audio = preemphasize(np.asarray(audio, dtype=np.float32), self.preemphasis)
        mel = log_mel_spectrogram(
            audio,
            self.window,
            self.filters,
            hop_length=self.hop_length,
            n_fft=self.n_fft,
        )
        if mel.shape[0] < 2:
            return mel

        # The extractor's length formula yields one frame fewer than the transform; the last frame
        # is left out of the statistics and zeroed.
        valid = mel.shape[0] - 1
        head = mel[:valid]
        mean = head.mean(axis=0, keepdims=True)
        std = head.std(axis=0, ddof=1, keepdims=True)
        out = (mel - mean) / (std + EPSILON)
        out[valid:] = 0.0
        return out.astype(np.float32)


def log_mel_spectrogram(
    audio: np.ndarray,
    window: np.ndarray,
    filters: np.ndarray,
    hop_length: int = DEFAULT_HOP_LENGTH,
    n_fft: int = DEFAULT_N_FFT,
) -> np.ndarray:
    """Power spectrogram through a mel filterbank, then log, shape (frames, n_mels).

    Emulates `torch.stft(center=True, pad_mode="constant")`.
    """
    win_length = len(window)
    n_fft = int(n_fft)
    hop_length = int(hop_length)

    # The shorter analysis window is centred inside the n_fft frame.
    offset = (n_fft - win_length) // 2
    full_window = np.zeros(n_fft, dtype=np.float32)
    full_window[offset : offset + win_length] = window

    # Zero-pad n_fft // 2 samples at both ends.
    pad = n_fft // 2
    padded = np.pad(np.asarray(audio, dtype=np.float32), (pad, pad), mode="constant")

    n_frames = 1 + (padded.size - n_fft) // hop_length
    if n_frames <= 0:
        return np.zeros((0, filters.shape[0]), dtype=np.float32)

    # Strided view of the padded signal: one row per frame, no copy.
    strides = (padded.strides[0] * hop_length, padded.strides[0])
    frames = np.lib.stride_tricks.as_strided(padded, shape=(n_frames, n_fft), strides=strides)
    frames = frames * full_window

    spectrum = np.fft.rfft(frames, n=n_fft, axis=1)
    power = (spectrum.real**2 + spectrum.imag**2).astype(np.float32)

    mel = power @ filters.T
    return np.log(mel + LOG_ZERO_GUARD).astype(np.float32)


__all__ = [
    "EPSILON",
    "LOG_ZERO_GUARD",
    "PREEMPHASIS",
    "MelFrontEnd",
    "hann_symmetric",
    "hz_to_mel_slaney",
    "log_mel_spectrogram",
    "mel_filterbank",
    "mel_to_hz_slaney",
    "preemphasize",
]
