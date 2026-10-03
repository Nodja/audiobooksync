"""Parakeet-CTC alignment over ONNX Runtime, with no PyTorch at run time.

The encoder runs through onnxruntime; everything else (tokenisation, the CTC Viterbi, the frame
grid) is numpy.  The model is never asked to transcribe: the text comes from the epub, and the
model only reports how likely each frame is for each label.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from . import model as model_source
from .frames import (
    MAX_LABEL_GAP,
    MIN_FRAMES_PER_LABEL,
    NEG_INF,
    AlignedWord,
    Alignment,
    HeardWord,
    frame_time,
)
from .mel import MelFrontEnd

#: The blank/pad id for this vocabulary.
BLANK_ID = 1024

#: Mel bins and sample rate the checkpoint was trained at.
MEL_BINS = 80
SAMPLE_RATE = 16000


def load_session(path: str | Path, providers: list[str] | None = None):
    """Open an onnxruntime session, preferring the GPU, and return it with the providers in use.

    onnxruntime falls back to the CPU silently, so the caller reports what was actually used.
    """
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3

    candidates = providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(str(path), options, providers=candidates)
    return session, session.get_providers()


class OnnxParakeetAligner:
    """Frame-synchronous CTC aligner.

    Callers may cache the log-probabilities, so the Viterbi can be repeated over many candidate
    text windows without re-running the encoder.
    """

    def __init__(
        self,
        repo: str = model_source.DEFAULT_REPO,
        variant: str = model_source.DEFAULT_VARIANT,
        providers: list[str] | None = None,
        progress=lambda _message: None,
    ):
        files = model_source.fetch(repo, variant, progress=progress)
        self._front = MelFrontEnd(n_mels=MEL_BINS, sample_rate=SAMPLE_RATE)

        # `transformers` imports torch when installed, and torch and onnxruntime both pull in
        # CUDA/cuDNN; whichever loads second can fail to find its DLLs on Windows, so the tokenizer
        # is loaded first.  Its import-time "no framework found" notice is silenced up front.
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(files.repo)
        self.blank_id = BLANK_ID
        self._word_ids: dict[str, list[int]] = {}

        self.session, self.providers = load_session(files.graph, providers)
        progress(f"model: encoding on {self.providers[0]}")
        inputs = self.session.get_inputs()
        self.input_name = inputs[0].name
        # The second input is a padding mask, all ones because every window is a complete clip.
        self.mask_name = inputs[1].name

    # ------------------------------------------------------------------ audio

    def log_probs(self, audio: np.ndarray) -> np.ndarray:
        """CTC log-probabilities for mono float32 16 kHz audio, shape (frames, vocab)."""
        features = self.features(audio)
        feed = {
            self.input_name: features,
            self.mask_name: np.ones(features.shape[:2], dtype=np.int64),
        }
        logits = self.session.run(None, feed)[0]
        return log_softmax(logits[0].astype(np.float32))

    def features(self, audio: np.ndarray) -> np.ndarray:
        """Mel spectrogram as the model wants it: (1, time, mel), float32."""
        return self._front(audio)

    # ------------------------------------------------------------------ text

    def label_ids(self, words: list[str]) -> tuple[list[int], list[int]]:
        """CTC label ids for a word list, plus the word owning each label.

        Words are tokenised individually, which gives the label-to-word mapping word timestamps
        need.  The leading space marker belongs to a word's first token, so this reproduces
        phrase-level ids.
        """
        flat: list[int] = []
        owners: list[int] = []
        for wi, word in enumerate(words):
            ids = self._word_ids.get(word)
            if ids is None:
                ids = self._word_ids[word] = self.tokenizer(word, add_special_tokens=False).input_ids
            if not ids:
                continue
            flat.extend(ids)
            owners.extend([wi] * len(ids))
        return flat, owners

    # --------------------------------------------------------------- viterbi

    def align_labels(
        self,
        log_probs: np.ndarray,
        labels: list[int],
        complete: bool = False,
    ) -> tuple[list[list[int] | None] | None, float]:
        """Viterbi decode of `labels` against `log_probs`.

        The path may stop before the last label unless `complete` asks for every label to be placed.

        Returns `(span_per_label, score)` where each span is `(first_frame, last_frame)`, or
        `(None, score)` when the label sequence cannot fit the window.
        """
        frames, n = log_probs.shape[0], len(labels)
        if n == 0 or frames < n * MIN_FRAMES_PER_LABEL:
            return None, float("-inf")

        # Extended label sequence: label k sits at state 2k+1, blanks between and at both ends.
        ext = np.empty(2 * n + 1, dtype=np.int64)
        ext[0::2] = self.blank_id
        ext[1::2] = labels

        states = 2 * n + 1
        prev = np.full(states, NEG_INF, dtype=np.float64)
        prev[0] = log_probs[0, self.blank_id]
        # A path may begin on the first label: a chunk is a hard cut, so it often starts mid-word.
        prev[1] = log_probs[0, labels[0]]
        back = np.zeros((frames, states), dtype=np.int32)

        # Label states are odd indices 2k+1.  The skip into label k (from state 2k-1) exists exactly
        # when label k differs from label k-1.
        label_arr = np.asarray(labels, dtype=np.int64)
        differs = np.ones(n, dtype=bool)
        if n > 1:
            differs[1:] = label_arr[1:] != label_arr[:-1]
        skip_targets = np.nonzero(differs)[0] * 2 + 1
        skip_targets = skip_targets[skip_targets >= 3].astype(np.intp)

        # Transition buffers are allocated once and reused every frame.
        step = np.empty(states, dtype=np.float64)
        skip = np.full(states, NEG_INF, dtype=np.float64)
        best = np.empty(states, dtype=np.float64)

        for t in range(1, frames):
            emit = log_probs[t, ext]

            step[0] = NEG_INF
            step[1:] = prev[:-1]
            choice = (step > prev).astype(np.int32)
            np.maximum(prev, step, out=best)

            if skip_targets.size:
                skip[skip_targets] = prev[skip_targets - 2]
                wins = skip > best
                np.maximum(best, skip, out=best)
                choice = np.where(wins, 2, choice)
                skip.fill(NEG_INF)

            np.add(best, emit, out=prev)
            back[t] = choice

        # The argmax is over every state, so a path may end before the last label.
        end = int(np.argmax(prev[-2:])) + states - 2 if complete else int(np.argmax(prev))
        score = float(prev[end])

        # Walk back to frame 0, recording each label's span.  Choices: 0 = stay, 1 = from s-1,
        # 2 = from s-2.
        state = end
        span_of_label: list[list[int] | None] = [None] * n
        for t in range(frames - 1, -1, -1):
            if state % 2 == 1:
                k = state // 2
                if span_of_label[k] is None:
                    span_of_label[k] = [t, t]
                else:
                    span_of_label[k][0] = t
            if t:
                choice = int(back[t, state])
                if choice == 1:
                    state -= 1
                elif choice == 2:
                    state -= 2
        return span_of_label, score

    def align(
        self,
        log_probs: np.ndarray,
        words: list[str],
        audio_start: float = 0.0,
        complete: bool = False,
    ) -> Alignment | None:
        """Align `words` against a window, returning per-word timings or `None`."""
        labels, owners = self.label_ids(words)
        spans, score = self.align_labels(log_probs, labels, complete)
        if spans is None:
            return None

        # `fit` is measured on the frames the labels landed on, since `score` also counts blank
        # frames of audio with no text.  <unk> is left out: numerals tokenise to it and the model
        # never predicts it.
        unk = self.tokenizer.unk_token_id
        by_word: dict[int, list[int]] = {}
        word_sum: dict[int, float] = {}
        word_frames: dict[int, int] = {}
        for k, owner in enumerate(owners):
            if spans[k] is None:
                continue
            low, high = spans[k]
            if labels[k] != unk:
                word_sum[owner] = word_sum.get(owner, 0.0) + float(
                    log_probs[low:high + 1, labels[k]].sum()
                )
                word_frames[owner] = word_frames.get(owner, 0) + high - low + 1
            by_word.setdefault(owner, []).append(k)
        placed = sum(word_frames.values())
        total = sum(word_sum.values())

        aligned: list[AlignedWord] = []
        for wi in sorted(by_word):
            k = by_word[wi]
            # A word ends at the first gap too wide to be inside it, so a last letter matched
            # against unrelated audio cannot stretch the word over everything between.
            last_label = k[0]
            for label in k[1:]:
                if spans[label][0] - spans[last_label][1] > MAX_LABEL_GAP:
                    break
                last_label = label
            first_label = k[0]
            first_frame = spans[first_label][0]
            last_frame = spans[last_label][1]
            aligned.append(
                AlignedWord(
                    index=wi,
                    text=words[wi],
                    start=frame_time(first_frame, audio_start),
                    end=frame_time(last_frame + 1, audio_start),
                    label_start=first_label,
                    label_end=last_label,
                    fit=word_sum[wi] / word_frames[wi] if wi in word_frames else 0.0,
                )
            )
        return Alignment(
            words=aligned,
            score=score,
            frames=int(log_probs.shape[0]),
            labels=len(labels),
            fit=total / placed if placed else float("-inf"),
            audio_start=audio_start,
        )

    def hear(self, log_probs: np.ndarray) -> list[HeardWord]:
        """Greedy transcription as words, each with the frames it was heard on."""
        tokens: list[list[int]] = []  # [id, first_frame, last_frame]
        previous = -1
        for frame, label in enumerate(log_probs.argmax(-1).tolist()):
            if label != self.blank_id and label != self.tokenizer.unk_token_id:
                if label == previous:
                    tokens[-1][2] = frame
                else:
                    tokens.append([label, frame, frame])
            previous = label
        words: list[HeardWord] = []
        pieces = self.tokenizer.convert_ids_to_tokens([token[0] for token in tokens])
        for piece, (_, first, last) in zip(pieces, tokens):
            if piece.startswith("▁") or not words:
                words.append(HeardWord(piece.lstrip("▁"), first, last))
            else:
                word = words[-1]
                words[-1] = HeardWord(word.text + piece, word.first_frame, last)
        return [word for word in words if word.text]

    def transcribe(self, log_probs: np.ndarray) -> str:
        """Greedy transcription of a chunk, to report what the narration actually says."""
        ids: list[int] = []
        previous = -1
        for frame in log_probs.argmax(-1):
            label = int(frame)
            if label != previous and label != self.blank_id:
                ids.append(label)
            previous = label
        return self.tokenizer.decode(ids)


def log_softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    """Numerically stable log_softmax (the max is subtracted first; logits reach 167)."""
    m = np.max(x, axis=axis, keepdims=True)
    z = x - m
    return z - np.log(np.sum(np.exp(z), axis=axis, keepdims=True))


__all__ = [
    "BLANK_ID",
    "MEL_BINS",
    "SAMPLE_RATE",
    "OnnxParakeetAligner",
    "load_session",
    "log_softmax",
]
