"""SMIL (EPUB Media Overlays) writer with an extra word-level timestamp payload."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

from . import __version__

SMIL_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<smil xmlns="http://www.w3.org/ns/SMIL" '
    'xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">\n'
)


@dataclass
class WordTiming:
    """One aligned word, in audio-file-local seconds."""

    text: str
    start: float
    end: float


@dataclass
class Par:
    """One <par>: a text fragment and the audio span that narrates it."""

    text_src: str  # e.g. "OEBPS/chapter1.xhtml#abs-p-7"
    audio_src: str  # e.g. "file.m4b"
    clip_begin: float
    clip_end: float
    words: list[WordTiming] = field(default_factory=list)
    #: Index of this par's first word within its block.  A paragraph of several sentences becomes
    #: several pars, and a reader needs this to tell which slice of the block each one owns.
    word_offset: int = 0


def _fmt(value: float) -> str:
    return f"{max(0.0, value):.3f}".rstrip("0").rstrip(".")


def _centiseconds(value: float) -> float:
    """A time in centiseconds, with the binary noise in `value * 100` rounded off.

    Otherwise `floor(1.15 * 100)` is 114, because 1.15 has no exact binary form.
    """
    return round(max(0.0, value) * 100, 6)


def _fmt_begin(value: float) -> str:
    """``clipBegin`` rounded down to the centisecond grid the word timings use."""
    return _fmt(math.floor(_centiseconds(value)) / 100)


def _fmt_end(value: float) -> str:
    """``clipEnd`` rounded up to the centisecond grid the word timings use."""
    return _fmt(math.ceil(_centiseconds(value)) / 100)


def _word_payload(words: list[WordTiming], word_text: bool) -> str:
    """Serialize one par's word timings as JSON.

    By default these are bare ``[start, end]`` pairs: the nth pair is the nth token of the block
    that ``<text src>`` points at, so keys and text would only add size (about 3.3 MB on a novel).

    Times are quantized outward to centiseconds (starts floor, ends ceil), on the same grid as
    ``clipBegin``/``clipEnd``, so each clip is exactly the envelope of its word timings.
    """
    def start_of(word: WordTiming) -> float:
        return math.floor(_centiseconds(word.start)) / 100

    def end_of(word: WordTiming) -> float:
        return math.ceil(_centiseconds(word.end)) / 100

    if word_text:
        return json.dumps(
            [
                {"start": start_of(w), "end": end_of(w), "text": w.text}
                for w in words
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(
        [[start_of(w), end_of(w)] for w in words],
        separators=(",", ":"),
    )


def render_smil(
    pars: list[Par],
    title: str = "",
    total_duration: float | None = None,
    generator: str = "audiobooksync",
    generator_version: str = __version__,
    word_text: bool = False,
    pretty: bool = True,
) -> str:
    """Serialize `pars` into a SMIL document.

    Standard part: a ``<seq epub:textref>`` per content document, and a
    ``<par><text src/><audio src clipBegin clipEnd/></par>`` per sentence fragment.

    Non-standard part: per-word timings ride in ``data-word-timestamps`` on the ``<par>``, with
    ``data-word-offset`` giving where the par's slice starts in its block.  ``data-*`` is the one
    attribute form EPUBCheck lets through.  The offset is always written, so a reader never has to
    guess what a missing one means.

    Set ``pretty=False`` to emit one ``<par>`` per line without indentation.
    """
    out: list[str] = [SMIL_HEADER]
    if title or total_duration:
        out.append("  <head>\n    <metadata>\n")
        if title:
            out.append(f"      <meta name=\"title\" content={quoteattr(title)}/>\n")
        if total_duration:
            out.append(
                f"      <meta name=\"totalDuration\" content={quoteattr(_fmt(total_duration))}/>\n"
            )
        out.append(f"      <meta name=\"generator\" content={quoteattr(generator)}/>\n")
        out.append(f"      <meta name=\"generatorVersion\" content={quoteattr(generator_version)}/>\n")
        out.append("    </metadata>\n  </head>\n")
    out.append("  <body>\n")

    last_document: str | None = None
    for par in pars:
        document = par.text_src.split("#", 1)[0]
        if document != last_document:
            if last_document is not None:
                out.append(("    </seq>\n") if pretty else ("</seq>"))
            out.append(
                f"    <seq epub:textref={quoteattr(document)}>\n"
                if pretty
                else f"<seq epub:textref={quoteattr(document)}>"
            )
            last_document = document

        payload = _word_payload(par.words, word_text)
        par_xml = (
            "<par"
            f" data-word-offset={quoteattr(str(max(0, par.word_offset)))}"
            f" data-word-timestamps={quoteattr(payload)}>"
            f"<text src={quoteattr(par.text_src)}/>"
            f"<audio src={quoteattr(par.audio_src)} "
            f"clipBegin={quoteattr(_fmt_begin(par.clip_begin))} "
            f"clipEnd={quoteattr(_fmt_end(par.clip_end))}/>"
            f"</par>"
        )
        out.append(("      " + par_xml + "\n") if pretty else par_xml)

    if last_document is not None:
        out.append(("    </seq>\n") if pretty else ("</seq>\n"))
    out.append(("  </body>\n</smil>\n") if pretty else ("</body>\n</smil>\n"))
    return "".join(out)


def write_smil(path: str | Path, pars: list[Par], **kwargs) -> Path:
    path = Path(path)
    path.write_text(render_smil(pars, **kwargs), encoding="utf-8")
    return path


_VERSION_META = re.compile(r'<meta name="generatorVersion" content="([^"]*)"')


def sidecar_version(path: str | Path) -> str | None:
    """The version of audiobooksync that wrote a sidecar, or `None` if it does not say."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            found = _VERSION_META.search(handle.read(4096))
    except OSError:
        return None
    return found.group(1) if found else None
