"""The synchronization pipeline: ebook + audiobook -> word-level timings -> SMIL.

The book is consumed left to right.  Each chunk of audio is transcribed greedily, the transcription
is matched to the text at the cursor to anchor it (see `anchors`), and the words between anchors are
forced-aligned; the cursor then advances to the last word placed.  A chunk with nothing in common
with the book is reported rather than papered over.  The narration start is found from the opening
audio the same way, since it rarely begins at the epub's first paragraph.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote

from .anchors import LOOKBACK_WORDS, BookIndex, align_anchored, hear, window_start
from .audio import SAMPLE_RATE, AudioInfo, decode, probe
from .epub import Block, Epub, open_epub
from .frames import FRAME_OFFSET, FRAME_RATE, AlignedWord, Alignment, frame_count
from .onnx_ctc import OnnxParakeetAligner
from .sentences import Sentence, segment_sentences
from .smil import Par, WordTiming, write_smil

#: Audio aligned per pass; 120 s keeps the encoder near 4 GB.
DEFAULT_CHUNK_SECONDS = 120.0

#: Audio used to locate the narration start.  Spoken credits of up to a minute match nothing, so
#: the window must be long enough for the narration to outweigh them.
LOCATE_SECONDS = 180.0
#: Votes the narration start needs from what the model hears in that audio.
OPENING_VOTES = 8

#: Mean log-probability below which a word was not aligned to its audio.  Matched words sit near
#: -0.1 (unhurried) to -2 (dense); text forced onto unrelated audio sits near -6.7.
MIN_FIT = -3.0

#: A run of at least `GARBAGE_RUN` consecutive words each below `GARBAGE_FIT` is text the narration
#: does not say (a different edition or translation): it was squeezed onto unrelated audio.  Matches
#: sit near 0, a made-up name reaches about -5, and a phrase the model mishears about -7 to -11,
#: while omitted text lands at -11 to -22.
GARBAGE_FIT = -9.0
GARBAGE_RUN = 3
#: Words beside a run that fit below this are caught in it: their audio was taken by the run, and
#: they are what its edges are judged against.
EDGE_FIT = -6.0

#: Text offered to a chunk, as a multiple of the words it should hold at the current pace, plus a
#: margin.  Words past the last anchor are simply left for the next chunk.
ANCHOR_WINDOW_FACTOR = 1.6
ANCHOR_WINDOW_EXTRA = 60

#: Untimed words between two timed ones that are given a share of the time between them: at most
#: this many, when the time between them is a plausible speaking pace (seconds per word, plus slack).
FILL_WORDS = 3
FILL_MIN_SECONDS = 0.03
FILL_MAX_SECONDS = 1.0
FILL_SLACK = 0.4

#: Votes a place needs when the second pass looks for audio anywhere in the book, since nothing
#: but the match says it belongs there.
ELSEWHERE_VOTES = 6

#: Audio (seconds) a chunk must have placed to resume after its last word rather than a full chunk on.
PROGRESS_SECONDS = 5.0

#: Rough narration pace, used until a chunk has measured it.
SEED_WORDS_PER_SECOND = 2.6

#: Trailing audio shorter than this (seconds) is not worth a chunk.
TAIL_SECONDS = 1.0


@dataclass
class WordTimingRecord:
    """One aligned word, resolved back to the epub."""

    index: int  #: global ebook word index
    text: str
    start: float
    end: float
    block_index: int
    sentence_index: int


@dataclass
class Miss:
    """A chunk of audio the first pass placed nothing on."""

    start: float
    span: float
    cursor: int
    heard: str
    followed: bool = False  #: whether words were placed after it, so it was a gap and not the outro


@dataclass
class SyncReport:
    """Everything the pipeline learned, including what it could not do."""

    epub: Path
    audio: Path
    duration: float
    words: list[WordTimingRecord] = field(default_factory=list)
    start_word: int = 0
    unplaced_windows: int = 0
    chunks: int = 0
    mean_score: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return len(self.words)

    @property
    def coverage(self) -> float:
        """Share of the audio duration spanned by placed words."""
        if not self.words or self.duration <= 0:
            return 0.0
        return min(1.0, max(word.end for word in self.words) / self.duration)


@dataclass
class SyncResult:
    report: SyncReport
    pars: list[Par]


class SyncError(RuntimeError):
    """Raised when the pair cannot be synchronized at all."""


# --------------------------------------------------------------------------- text helpers


class BookText:
    """The epub's word stream plus the lookup tables the mapping stage needs."""

    def __init__(self, epub: Epub):
        self.epub = epub
        self.blocks: list[Block] = epub.blocks
        self.raw: list[str] = [w.text for w in epub.words]
        self.norm: list[str] = [w.norm for w in epub.words]
        self.block_of: list[int] = []
        for bi, block in enumerate(self.blocks):
            self.block_of.extend([bi] * block.word_count)
        if len(self.block_of) != len(self.raw):
            raise SyncError(
                f"epub word/block accounting is inconsistent "
                f"({len(self.block_of)} block slots for {len(self.raw)} words)"
            )
        self.sentences: list[Sentence] = segment_sentences(self.raw)
        self.sentence_of: list[int] = [-1] * len(self.raw)
        for si, sentence in enumerate(self.sentences):
            for i in range(sentence.start, min(sentence.end, len(self.sentence_of))):
                self.sentence_of[i] = si

    def __len__(self) -> int:
        return len(self.raw)

    def window(self, start: int, count: int) -> list[str]:
        """The normalised words of `count` words of the book from `start`."""
        return self.norm[start:start + count]


# --------------------------------------------------------------------------- chunk alignment


@dataclass
class ChunkOutcome:
    alignment: Alignment | None
    cursor: int
    score: float
    window_words: int


# --------------------------------------------------------------------------- mapping


def _garbage_runs(words: list[AlignedWord]) -> list[tuple[int, int]]:
    """Half-open ranges of `words` that are runs of words forced onto audio that is not theirs.

    A run is extended over the poorly fitting words beside it, and runs that then touch are one.
    """
    runs: list[list[int]] = []
    start = None
    for i, word in enumerate([*words, None]):
        if word is not None and word.fit < GARBAGE_FIT:
            if start is None:
                start = i
            continue
        if start is not None and i - start >= GARBAGE_RUN:
            runs.append([start, i])
        start = None
    for run in runs:
        while run[0] > 0 and words[run[0] - 1].fit < EDGE_FIT:
            run[0] -= 1
        while run[1] < len(words) and words[run[1]].fit < EDGE_FIT:
            run[1] += 1
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _drop_garbage_runs(words: list[AlignedWord]) -> list[AlignedWord]:
    """Drop runs of consecutive words that were forced onto audio that is not theirs.

    These can be anywhere, so the rest of the chunk keeps its timings and only the mismatched
    stretch goes untimed; `build_pars` ends a par at the hole.
    """
    dropped = {i for start, end in _garbage_runs(words) for i in range(start, end)}
    return [word for i, word in enumerate(words) if i not in dropped]


def _frame_of(seconds: float, audio_start: float) -> int:
    """The encoder frame at `seconds` of the audio, the inverse of `frame_time`."""
    return max(0, int((seconds - audio_start) * FRAME_RATE - FRAME_OFFSET))


def _recover_edges(
    aligner: OnnxParakeetAligner,
    log_probs,
    audio_start: float,
    text: list[str],
    positions: list[int],
    first_frame: int,
    last_frame: int,
) -> list[AlignedWord]:
    """Words at either end of a garbage run that the narrator does read.

    CTC gives every label a frame, so text that is not in the audio gets squeezed onto the audio of
    the words beside it, and those are dragged down with it.  With the run taken out, the audio they
    belong to is a free gap, and the longest prefix and suffix of the run that fit it are theirs.
    """
    gap = log_probs[first_frame:last_frame]
    if gap.shape[0] < 2:
        return []
    base = audio_start + first_frame / FRAME_RATE

    def fits(chosen: list[int]) -> list[AlignedWord] | None:
        placed = aligner.align(gap, [text[p] for p in chosen], audio_start=base)
        if placed is None or len(placed.words) < len(chosen) or any(w.fit < MIN_FIT for w in placed.words):
            return None
        return placed.words

    head = 0
    while head < len(positions) and fits(positions[:head + 1]) is not None:
        head += 1
    tail = 0
    while head + tail < len(positions) and fits(positions[len(positions) - tail - 1:]) is not None:
        tail += 1
    chosen = positions[:head] + positions[len(positions) - tail:] if tail else positions[:head]
    if not chosen:
        return []
    placed = fits(chosen)
    return [replace(w, index=chosen[w.index]) for w in placed] if placed else []


def repair_garbage(
    aligner: OnnxParakeetAligner,
    book: BookText,
    log_probs,
    audio_start: float,
    outcome: ChunkOutcome,
) -> ChunkOutcome:
    """Leave out the garbage runs of a chunk's alignment, keeping the words beside them that were read.

    The words either side of a run were timed correctly, so the audio between them is the gap the
    run's own words have to fit; `_recover_edges` finds which of them do.
    """
    alignment = outcome.alignment
    runs = _garbage_runs(alignment.words) if alignment is not None else []
    if not runs:
        return outcome

    text = book.window(outcome.cursor, outcome.window_words)
    words = alignment.words
    recovered: list[AlignedWord] = []
    for start, end in runs:
        before = words[start - 1] if start > 0 else None
        after = words[end] if end < len(words) else None
        recovered += _recover_edges(
            aligner, log_probs, audio_start, text, [w.index for w in words[start:end]],
            _frame_of(before.end, audio_start) if before else 0,
            _frame_of(after.start, audio_start) if after else log_probs.shape[0],
        )
    dropped = {i for start, end in runs for i in range(start, end)}
    kept = [w for i, w in enumerate(words) if i not in dropped]
    repaired = replace(alignment, words=sorted(kept + recovered, key=lambda w: w.index))
    return replace(outcome, alignment=repaired)


def _placed_words(alignment: Alignment) -> list[AlignedWord]:
    """The words of an alignment that are timed: garbage runs are left out."""
    return _drop_garbage_runs(alignment.words)


def _words_from_alignment(
    alignment: Alignment, window_start: int, book: BookText
) -> list[WordTimingRecord]:
    """Map aligned words back to global ebook indices, keeping text order.

    Words that did not match their audio are dropped (see `_placed_words`).
    """
    out: list[WordTimingRecord] = []
    last_index = -1
    for word in _placed_words(alignment):
        index = window_start + word.index
        if index < 0 or index >= len(book) or index <= last_index:
            continue
        last_index = index
        out.append(
            WordTimingRecord(
                index=index,
                text=book.raw[index],
                start=word.start,
                end=word.end,
                block_index=book.block_of[index],
                sentence_index=book.sentence_of[index],
            )
        )
    return out


def _ends(text: str, span: int = 6) -> str:
    """First and last few words of a passage, as much as fits on a warning line."""
    words = text.split()
    if len(words) <= 2 * span:
        return f'"{" ".join(words)}"'
    return f'"{" ".join(words[:span])}" ... "{" ".join(words[-span:])}"'


def _describe_span(book: BookText, first: int, last: int) -> str:
    """First and last words of a stretch of text, as a suffix for a warning."""
    if first >= last:
        return ""
    return f": {_ends(' '.join(book.raw[first:last]))}"


def _skip_warnings(book: BookText, records: list[WordTimingRecord]) -> list[str]:
    """A warning for each stretch of text between timed words that no timing covers."""
    return [
        f"{after.index - before.index - 1} ebook words skipped at {_clock(before.end)} "
        f"(words {before.index + 1}..{after.index - 1})" + _describe_span(book, before.index + 1, after.index)
        for before, after in pairwise(records)
        if after.index > before.index + 1
    ]


def _fill_short_gaps(book: BookText, records: list[WordTimingRecord]) -> list[WordTimingRecord]:
    """Give the few words left untimed between two timed ones a share of the time between them.

    A word or two the aligner dropped sits between neighbours that were heard, so its place in the
    audio is known to within a word.  Text the narrator really skips leaves almost no time between
    its neighbours, and a caption leaves a long one, so only a gap that fits the pace is filled,
    and only for words of one block.
    """
    out: list[WordTimingRecord] = []
    for before, after in pairwise([None, *records, None]):
        if after is None:
            break
        if before is not None and 1 < after.index - before.index <= FILL_WORDS + 1:
            missing = range(before.index + 1, after.index)
            seconds = after.start - before.end
            if (
                book.block_of[missing[0]] == book.block_of[missing[-1]]
                and FILL_MIN_SECONDS * len(missing) <= seconds <= FILL_MAX_SECONDS * len(missing) + FILL_SLACK
            ):
                weights = [len(book.norm[i]) for i in missing]
                at = before.end
                for i, weight in zip(missing, weights):
                    end = at + seconds * weight / sum(weights)
                    out.append(WordTimingRecord(i, book.raw[i], at, end, book.block_of[i], book.sentence_of[i]))
                    at = end
        out.append(after)
    return out


def build_pars(
    book: BookText,
    records: list[WordTimingRecord],
    audio_name: str,
) -> list[Par]:
    """Group timings into ``<par>`` elements, one per (sentence, block) run of consecutive words."""
    pars: list[Par] = []
    current: list[WordTimingRecord] = []

    def flush() -> None:
        if not current:
            return
        first, last = current[0], current[-1]
        block = book.blocks[first.block_index]
        word_offset = first.index - block.word_start if block.word_start >= 0 else 0
        pars.append(
            Par(
                text_src=_text_src(block),
                audio_src=audio_name,
                clip_begin=first.start,
                clip_end=max(last.end, first.start + 0.001),
                words=[
                    WordTiming(text=rec.text, start=rec.start, end=rec.end)
                    for rec in current
                ],
                word_offset=max(0, word_offset),
            )
        )
        current.clear()

    for record in records:
        if current and (
            record.block_index != current[-1].block_index
            or record.sentence_index != current[-1].sentence_index
            # Readers map the nth timing to the nth token after the offset, so a hole would shift the rest.
            or record.index != current[-1].index + 1
        ):
            flush()
        current.append(record)
    flush()
    return pars


def _text_src(block: Block) -> str:
    """``<text src>`` for a block: the percent-encoded OCF path plus its synthetic anchor.

    The anchor is ``abs-<tag>-<n>`` (see ``epub.block_anchor``) rather than a document id, because
    inserting ids would mean rewriting the epub.  The epub's filename is left out: it is the
    sidecar's own basename, which the client already knows.
    """
    return f"{quote(block.href)}#{block.anchor}"


# --------------------------------------------------------------------------- driver


def _clock(seconds: float) -> str:
    """Seconds of audio as a position in the book, which is how a warning gets read."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _unreached_by_document(book: BookText, start_word: int, cursor: int, timed: set[int]) -> str:
    """Untimed text outside the walked stretch, by document: the front matter and the tail."""
    lines = []
    for document in book.epub.docs:
        left = [
            i for i in range(document.word_start, document.word_start + document.word_count)
            if (i < start_word or i >= cursor) and i not in timed
        ]
        if left:
            name = document.href.rsplit("/", 1)[-1]
            lines.append(f"\n      {name}  {len(left)} words  {_ends(' '.join(book.raw[left[0]:left[-1] + 1]))}")
    return "".join(lines)


def _merge_spans(misses: list[Miss]) -> list[tuple[float, float]]:
    """The audio the misses cover, with neighbouring chunks joined."""
    spans: list[list[float]] = []
    for miss in misses:
        if spans and miss.start <= spans[-1][1] + 1e-6:
            spans[-1][1] = miss.start + miss.span
        else:
            spans.append([miss.start, miss.start + miss.span])
    return [(start, end) for start, end in spans]


def synchronize(
    epub_path: str | Path,
    audio_path: str | Path,
    aligner: OnnxParakeetAligner | None = None,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    start_hint: int | None = None,
    progress=None,
    on_status=None,
) -> SyncResult:
    """Align one epub against one audio file.

    `start_hint` skips the search for the narration start.  `progress` receives short status
    strings.  `on_status(done, total, detail)` is called after every chunk, with `total=0` for
    phases of unknown size.
    """
    epub_path = Path(epub_path)
    audio_path = Path(audio_path)

    say = progress if progress is not None else (lambda _message: None)

    epub = open_epub(epub_path)
    book = BookText(epub)
    if len(book) == 0:
        raise SyncError(f"{epub_path.name} contains no readable text")
    if start_hint is not None and not 0 <= start_hint < len(book):
        raise SyncError(
            f"start word {start_hint} is outside {epub_path.name} (0..{len(book) - 1})"
        )

    info: AudioInfo = probe(audio_path)

    if aligner is None:
        aligner = OnnxParakeetAligner(progress=say)

    index = BookIndex(book.norm)
    report = SyncReport(epub=epub_path, audio=audio_path, duration=info.duration)
    scores: list[float] = []

    def step(pos: float, detail: str = "") -> None:
        if on_status is not None:
            on_status(pos / chunk_seconds, info.duration / chunk_seconds, detail)

    # ------------------------------------------------------------ locate
    if start_hint is None:
        step(0.0, "finding the narration start")
        opening = decode(audio_path, 0.0, min(LOCATE_SECONDS, info.duration))
        opening_lp = aligner.log_probs(opening)
        found = index.locate_opening(hear(aligner, opening_lp).words)
        if found is None or found[1] < OPENING_VOTES:
            raise SyncError(f"could not find where the narration of {epub_path.name} starts")
        cursor = found[0]
    else:
        cursor = start_hint
    report.start_word = cursor

    # ------------------------------------------------------------ walk
    pace = SEED_WORDS_PER_SECOND
    timed: set[int] = set()
    misses: list[Miss] = []

    def walk(pos: float, end: float, cursor: int, second_pass: bool = False) -> int:
        """Align the audio from `pos` to `end`, and return the cursor after the last word placed.

        The first pass walks the book from `cursor`.  The second pass is given audio the first could
        not place and looks for it anywhere in the text that has no timings yet.
        """
        nonlocal pace
        anywhere = second_pass
        while pos < end - TAIL_SECONDS and cursor < len(book):
            span = min(chunk_seconds, end - pos)
            audio = decode(audio_path, pos, span)
            if audio.size < SAMPLE_RATE // 2:
                break
            log_probs = aligner.log_probs(audio)
            expected = frame_count(audio.size)
            if log_probs.shape[0] != expected:
                report.warnings.append(
                    f"encoder produced {log_probs.shape[0]} frames for {audio.size} samples "
                    f"at {_clock(pos)}, expected {expected}"
                )

            outcome = None
            heard = hear(aligner, log_probs)
            if heard.words:
                places = index.places(heard.words, floor=0 if anywhere else cursor)
                if anywhere:
                    places = [place for place in places if place[1] >= ELSEWHERE_VOTES and place[0] not in timed]
                    lo = max(0, max(places, key=lambda place: place[1])[0] - LOOKBACK_WORDS) if places else None
                else:
                    lo = window_start(places, cursor)
                count = int(pace * span * ANCHOR_WINDOW_FACTOR) + ANCHOR_WINDOW_EXTRA
                text = book.window(lo, count) if lo is not None else []
                alignment = align_anchored(aligner, log_probs, pos, heard, text) if text else None
                if alignment is not None:
                    outcome = repair_garbage(
                        aligner, book, log_probs, pos,
                        ChunkOutcome(alignment, lo, alignment.score_per_label, count),
                    )
            report.chunks += 1

            records = _words_from_alignment(outcome.alignment, outcome.cursor, book) if outcome else []
            records = [record for record in records if record.index not in timed]

            if not records:
                if not second_pass:
                    said = aligner.transcribe(log_probs)
                    misses.append(Miss(pos, span, cursor, said))
                pos += span
                step(pos, f"{len(report.words)} words")
                continue

            # Placing words means audio and text still correspond, so earlier misses were a real gap.
            for miss in misses:
                miss.followed = True

            report.words.extend(records)
            timed.update(record.index for record in records)
            scores.append(outcome.score)
            cursor = records[-1].index + 1
            anywhere = False
            if records[-1].end - pos >= span / 2:
                pace = len(records) / (records[-1].end - records[0].start)
            # Resume where the last word ended rather than on the fixed grid, so a word the cut would
            # have split is heard whole.  A chunk that placed next to nothing moves on by its full span.
            resume = records[-1].end
            last_chunk = pos + span >= end
            pos = resume if not last_chunk and resume - pos >= PROGRESS_SECONDS else pos + span
            step(pos, f"{len(report.words)} words, {outcome.score:+.2f}/label")
        return cursor

    cursor = walk(0.0, info.duration, cursor)

    # Audio the first pass could not place may be text it had already passed (an acknowledgements
    # chapter read last, say), so it is searched for again among the words still untimed.
    first_pass_words = len(report.words)
    for start, end in _merge_spans(misses):
        walk(start, end, 0, second_pass=True)
    recovered = report.words[first_pass_words:]
    report.words = _fill_short_gaps(book, sorted(report.words, key=lambda record: record.index))
    report.warnings.extend(_skip_warnings(book, report.words))
    for miss in misses:
        if miss.followed and not any(r.start < miss.start + miss.span and r.end > miss.start for r in recovered):
            report.unplaced_windows += 1
            report.warnings.append(
                f"no match at {_clock(miss.start)} (from word {miss.cursor})"
                + (f"\n      heard: {_ends(miss.heard)}" if miss.heard.strip() else "")
            )

    # ---------------------------------------------------------------- report
    if scores:
        report.mean_score = sum(scores) / len(scores)
    if not report.words:
        report.warnings.append("nothing was aligned")
    # Text before the located start counts as unreached, alongside whatever the walk never got to.
    unreached = len(book) - (cursor - report.start_word) - sum(
        1 for i in timed if i < report.start_word or i >= cursor
    )
    if unreached:
        report.warnings.append(
            f"{unreached} ebook words were never reached ({len(book) - unreached}/{len(book)} consumed)"
            + _unreached_by_document(book, report.start_word, cursor, timed)
        )

    pars = build_pars(book, report.words, audio_path.name)
    return SyncResult(report=report, pars=pars)


def write_result(
    result: SyncResult,
    out_path: str | Path,
    title: str = "",
    word_text: bool = False,
    pretty: bool = True,
) -> Path:
    """Write the SMIL sidecar."""
    return write_smil(
        out_path,
        result.pars,
        title=title or result.report.epub.stem,
        total_duration=result.report.duration,
        word_text=word_text,
        pretty=pretty,
    )


def summarise(result: SyncResult) -> str:
    """Human-readable one-screen summary of a synchronization run."""
    report = result.report
    lines = [
        f"epub    {report.epub.name}",
        f"audio   {report.audio.name}  ({report.duration / 60:.1f} min)",
        f"chunks  {report.chunks}  ({report.unplaced_windows} unplaced)",
        f"words   {report.placed} placed, starting at ebook word {report.start_word}",
        f"pars    {len(result.pars)}",
        f"audio covered to {report.coverage * 100:.1f}%",
    ]
    if report.mean_score:
        lines.append(f"score   {report.mean_score:+.3f}/label mean")
    if result.pars:
        first = result.pars[0]
        lines.append(
            f"first   {first.clip_begin:.2f}s  {len(first.words)} words  {first.text_src}"
        )
        last = result.pars[-1]
        lines.append(
            f"last    {last.clip_begin:.2f}s  {len(last.words)} words  {last.text_src}"
        )
    if report.warnings:
        lines.append("warnings:")
        for warning in report.warnings[:10]:
            lines.append(f"  - {warning}")
        if len(report.warnings) > 10:
            lines.append(f"  ... and {len(report.warnings) - 10} more")
    return "\n".join(lines)
