"""Sentence segmentation over the EPUB word stream."""

from __future__ import annotations

from dataclasses import dataclass

SENTENCE_END = ".!?…"
CLOSERS = "\"'\u201d\u2019)]}"
MIN_WORDS = 3
TARGET_MAX = 220


@dataclass
class Sentence:
    """A run of consecutive global word indices."""

    start: int
    end: int  # exclusive


def _ends_sentence(words: list[str], i: int) -> bool:
    if i + 1 >= len(words):
        return True
    stripped = words[i].rstrip(CLOSERS)
    return bool(stripped) and stripped[-1] in SENTENCE_END


def segment_sentences(words: list[str], min_words: int = MIN_WORDS, max_words: int = TARGET_MAX) -> list[Sentence]:
    """Split `words` into sentence-sized spans using terminal punctuation."""
    out: list[Sentence] = []
    start = 0
    n = len(words)
    if n == 0:
        return out

    i = 0
    while i < n:
        length = i - start + 1
        boundary = _ends_sentence(words, i)
        if boundary and length >= min_words:
            out.append(Sentence(start, i + 1))
            start = i + 1
        elif length >= max_words:
            # Over-long run (missing punctuation, poetry): break at the furthest candidate.
            break_at = -1
            for j in range(i, max(start + min_words - 1, i - 40), -1):
                if _ends_sentence(words, j):
                    break_at = j
                    break
            cut = break_at + 1 if break_at > start else i + 1
            out.append(Sentence(start, cut))
            start = cut
        i += 1

    if start < n:
        if out and n - start < min_words:
            out[-1] = Sentence(out[-1].start, n)
        else:
            out.append(Sentence(start, n))
    return out
