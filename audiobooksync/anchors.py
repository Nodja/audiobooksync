"""Finding where narration sits in the book from what the model hears, and aligning around it.

The model's own greedy transcription is cheap (the log-probabilities are already computed) and
mostly right.  Runs of words it shares with the book are anchors: they say where in the book a chunk
is without searching, and only the text between anchors is left for forced alignment.  A different
edition, a skipped caption or a made-up name then only costs the words around it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from .epub import normalize_word
from .frames import FRAME_RATE, AlignedWord, Alignment, HeardWord
from .onnx_ctc import OnnxParakeetAligner

#: Words in the phrases looked up in the book, and the most places one may occur before it is too
#: common to say anything ("and then he").
NGRAM = 3
MAX_HITS = 8
#: Phrase positions are voted into bins this many words wide, so a few extra or missing words
#: along a chunk still vote for the same place.
VOTE_BIN = 16
#: Votes a place needs: three phrases are five consecutive words.
MIN_VOTES = 3
#: The cursor is kept unless the audio is further ahead than this many words, and the window then
#: starts a little before where the audio was found.
FAR_WORDS = 40
LOOKBACK_WORDS = 10

#: Shortest run of shared words that anchors a stretch of text to a stretch of audio.
MIN_ANCHOR = 4
#: Share of the heard words the anchors must cover for the chunk to be aligned from them.
MIN_ANCHOR_SHARE = 0.25
#: A short anchor whose offset between heard and text jumps by more than `JUMP_WORDS` from the
#: anchor before it, with no anchor after it on its level, is a repeated phrase matched in the wrong
#: place: narration that repeats ("lilac and gooseberries") can be anchored to a later copy,
#: which would leave the text between as unspoken.  Anchors of `SURE_ANCHOR` words are kept.
JUMP_WORDS = 30
SURE_ANCHOR = 10
#: Frames an anchor's segment begins before the frame its first word was heard on.
SEGMENT_MARGIN = 2
#: Unmatched text is only aligned to unmatched audio when the two are about as long.
GAP_SLACK_WORDS = 3
GAP_RATIO = 2
#: Words heard after the last anchor beyond which the chunk ends in text that does not match: it is
#: left for the next chunk, which starts at the last anchor, instead of being forced onto that audio.
TAIL_HEARD_WORDS = 16


@dataclass
class Heard:
    """A chunk's transcription, normalised the way the book's words are."""

    words: list[str]
    frames: list[HeardWord]


def hear(aligner: OnnxParakeetAligner, log_probs) -> Heard:
    frames = [
        replace(word, text=normalize_word(word.text))
        for word in aligner.hear(log_probs)
        if normalize_word(word.text)
    ]
    return Heard([word.text for word in frames], frames)


class BookIndex:
    """Where every phrase of the book occurs."""

    def __init__(self, norm: list[str]):
        self.table: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for i in range(len(norm) - NGRAM + 1):
            self.table[tuple(norm[i:i + NGRAM])].append(i)

    def places(self, heard: list[str], floor: int = 0) -> list[tuple[int, int]]:
        """Each place in the book the heard words agree with, as (book word of the first heard word, votes).

        Every heard phrase found in the book votes for the start it implies; a place is a bin of
        starts with at least `MIN_VOTES` votes.  Phrases before `floor` are ignored.  A chunk can
        have more than one place when the book has text the narrator left out.
        """
        starts: dict[int, list[int]] = defaultdict(list)
        for i in range(len(heard) - NGRAM + 1):
            hits = self.table.get(tuple(heard[i:i + NGRAM]))
            if not hits or len(hits) > MAX_HITS:
                continue
            for position in hits:
                if position >= floor:
                    starts[(position - i) // VOTE_BIN].append(position - i)

        places = []
        for bin_ in sorted(starts):
            around = sorted(starts[bin_] + starts.get(bin_ + 1, []))
            strength = len(around)
            if strength >= MIN_VOTES and strength >= len(starts.get(bin_ - 1, [])) + len(starts[bin_]):
                places.append((around[len(around) // 2], strength))
        return places

    def locate_opening(self, heard: list[str]) -> tuple[int, int] | None:
        """The book word where narration begins, and the votes for it.

        Spoken credits match nothing, so the start is the first word of the first phrase that
        agrees with the strongest place rather than the first heard word.
        """
        places = self.places(heard)
        if not places:
            return None
        origin, votes = max(places, key=lambda place: place[1])
        for i in range(len(heard) - NGRAM + 1):
            hits = self.table.get(tuple(heard[i:i + NGRAM]))
            if hits and len(hits) <= MAX_HITS:
                for position in hits:
                    if abs(position - i - origin) <= VOTE_BIN:
                        return position, votes
        return None


def window_start(places: list[tuple[int, int]], cursor: int) -> int:
    """Where to start the text window for a chunk whose audio agrees with the book at `places`.

    The cursor stands unless nothing agrees with the text near it, which means the narration has
    jumped ahead (it skipped front matter); the window then starts a little before the strongest
    place.
    """
    if not places or any(origin <= cursor + FAR_WORDS for origin, _ in places):
        return cursor
    return max(places, key=lambda place: place[1])[0] - LOOKBACK_WORDS


def _consistent(anchors: list) -> list:
    """The anchors without short ones stranded off the offset their neighbours agree on."""
    kept: list = []
    for k, block in enumerate(anchors):
        offset = block.b - block.a
        stranded = (
            block.size < SURE_ANCHOR
            and kept
            and abs(offset - (kept[-1].b - kept[-1].a)) > JUMP_WORDS
            and not (k + 1 < len(anchors) and abs(anchors[k + 1].b - anchors[k + 1].a - offset) <= JUMP_WORDS)
        )
        if not stranded:
            kept.append(block)
    return kept


def align_anchored(
    aligner: OnnxParakeetAligner,
    log_probs,
    audio_start: float,
    heard: Heard,
    text: list[str],
) -> Alignment | None:
    """Align `text` (the book words from the window start) using the words the model heard.

    The runs of words that heard and text share cut the chunk into segments: each starts at an
    anchor and runs to the next, and is forced-aligned on its own, so the text and audio between
    anchors cannot disturb their neighbours.  Word indices in the result are positions in `text`.
    Returns `None` when too little of the chunk is anchored, and the caller aligns it whole.
    """
    anchors = [
        block
        for block in SequenceMatcher(None, heard.words, text, autojunk=False).get_matching_blocks()
        if block.size >= MIN_ANCHOR
    ]
    anchors = _consistent(anchors)
    if sum(block.size for block in anchors) < MIN_ANCHOR_SHARE * len(heard.words):
        return None

    frames = log_probs.shape[0]
    cuts = [max(0, heard.frames[block.a].first_frame - SEGMENT_MARGIN) for block in anchors]

    # (first text word, text words, text words without the gap, first frame, end frame)
    segments: list[tuple[int, int, int, int, int]] = []
    first = anchors[0]
    lead_text = min(first.b, int(first.a * GAP_RATIO) + GAP_SLACK_WORDS) if first.a else 0
    if lead_text and cuts[0] > 0:
        segments.append((first.b - lead_text, lead_text, lead_text, 0, cuts[0]))
    for k, block in enumerate(anchors):
        end_a, end_b = block.a + block.size, block.b + block.size
        if k + 1 < len(anchors):
            gap_text, gap_heard = anchors[k + 1].b - end_b, anchors[k + 1].a - end_a
            last_frame = cuts[k + 1]
        else:
            gap_text = gap_heard = len(heard.words) - end_a
            gap_text = min(gap_text, len(text) - end_b) if gap_heard <= TAIL_HEARD_WORDS else 0
            last_frame = frames
        # Text far longer than what was heard in its place was not narrated: leave it out.
        if gap_text > GAP_RATIO * gap_heard + GAP_SLACK_WORDS:
            gap_text = 0
        segments.append((block.b, block.size + gap_text, block.size, cuts[k], last_frame))

    words: list[AlignedWord] = []
    score = 0.0
    labels = 0
    for start, count, core, low, high in segments:
        placed = None
        # A gap that cannot fit its audio is left out, so the anchor still gets timed.
        for size in dict.fromkeys((count, core)):
            if high > low:
                placed = aligner.align(
                    log_probs[low:high], text[start:start + size],
                    audio_start=audio_start + low / FRAME_RATE, complete=True,
                )
            if placed is not None:
                break
        if placed is None:
            continue
        words.extend(replace(word, index=start + word.index) for word in placed.words)
        score += placed.score
        labels += placed.labels
    if not words:
        return None
    return Alignment(
        words=words,
        score=score,
        frames=frames,
        labels=labels,
        fit=sum(word.fit for word in words) / len(words),
        audio_start=audio_start,
    )

