"""Dependency-free EPUB (XHTML) reader used for synchronization.

Walks each content document in order and records ``blocks`` (elements that own readable text,
each with a deterministic anchor id) and ``words`` (every word in those blocks, in reading order).
The client's reader reproduces the anchor scheme (:func:`block_anchor`) so that a SMIL
``<text src="chapter.xhtml#abs-p-7"/>`` resolves against an EPUB that is never modified.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

SYNC_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "dd",
    "dt",
    "td",
    "th",
    "figcaption",
    "caption",
    "blockquote",
    "pre",
    "address",
    "summary",
    "div",
    "section",
    "article",
    "aside",
    "header",
    "footer",
    "main",
    "nav",
}

# Elements whose text is inline and must be glued to their neighbours.
INLINE_TAGS = {
    "a",
    "span",
    "em",
    "strong",
    "i",
    "b",
    "u",
    "small",
    "sub",
    "sup",
    "code",
    "q",
    "cite",
    "abbr",
    "time",
    "mark",
    "s",
    "ins",
    "del",
    "big",
    "tt",
    "font",
    "label",
    "bdi",
    "bdo",
    "ruby",
    "rt",
    "rp",
    "wbr",
}

SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "svg", "math", "template"}

VOID_TAGS = {
    "br",
    "hr",
    "img",
    "input",
    "meta",
    "link",
    "base",
    "col",
    "area",
    "source",
    "track",
    "wbr",
}


class _DocumentWalker(HTMLParser):
    """Collect one document's sync blocks.

    Mirrors `collectSyncBlocks` in the client's foliate copy: text belongs to the innermost open
    block, a block takes an anchor number only if it ends up with readable text, and an element
    with an id keeps it as its anchor.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: Blocks in document order, ordered by where their first text run starts.
        self.open_blocks: list[dict] = []
        self.counters: dict[str, int] = {}
        self.stack: list[dict] = []
        self.skip_depth = 0

    def _target(self) -> dict | None:
        for frame in reversed(self.stack):
            if frame["is_block"]:
                return frame
        return None

    def _add_text(self, text: str) -> None:
        """Attach text to the innermost open block, numbering and ordering it on its first run.

        Doing this at close would put a container after the blocks nested in it, so
        ``<li>text<ul><li>sub</li></ul></li>`` would read the sublist first.
        """
        if not text:
            return
        target = self._target()
        if target is None:
            return
        if not target["placed"]:
            target["placed"] = True
            self.counters[target["tag"]] = self.counters.get(target["tag"], 0) + 1
            target["anchor"] = target["id"] or block_anchor(
                target["tag"], self.counters[target["tag"]]
            )
            self.open_blocks.append(target)
        target["parts"].append(text)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        if tag in SKIP_TAGS:
            if not self_closing and tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            # A line break separates words, and the client splits its text the same way.
            self._add_text(" ")
            return
        if tag in VOID_TAGS or self_closing:
            return
        element_id = (dict(attrs).get("id") or "").strip() or None
        self.stack.append(
            {
                "tag": tag,
                "is_block": tag in SYNC_BLOCK_TAGS and tag not in INLINE_TAGS,
                "id": element_id,
                "parts": [],
                "placed": False,
            }
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag.lower(), attrs, False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag.lower(), attrs, True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] != tag:
                continue
            del self.stack[index:]
            return

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self._add_text(data)

    def finish(self) -> list[tuple[str, str, str]]:
        self.close()
        return [(f["anchor"], f["tag"], "".join(f["parts"])) for f in self.open_blocks]


@dataclass
class Block:
    """A sync block inside one EPUB content document."""

    anchor: str
    href: str  # relative href of the content document, e.g. "OEBPS/chapter1.xhtml"
    tag: str
    text: str
    word_start: int  # global index of the first word of this block
    word_count: int


@dataclass
class Word:
    text: str
    norm: str


@dataclass
class Doc:
    href: str  # relative to the epub root, posix style
    word_start: int
    word_count: int


@dataclass
class Epub:
    path: Path
    docs: list[Doc] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)


class EpubError(RuntimeError):
    """The file is not an EPUB that can be walked: not a zip, or missing its package document."""


def normalize_word(token: str) -> str:
    """Fold a surface token to the key used for text<->audio matching."""
    s = token.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9'\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff]+", "", s)
    return s


def block_anchor(tag: str, counter: int) -> str:
    """Deterministic anchor id for the `counter`-th sync block of a document."""
    return f"abs-{tag}-{counter}"


#: A word, as this parser and the client's reader both count them.  Hyphens and dashes split as
#: well as whitespace, because the narration reads "well-known" as two words.
WORD_RE = re.compile(r"[^\s\u2010-\u2015\u2212-]+")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _find(node, name: str):
    for element in node.iter():
        if _local(element.tag) == name:
            return element
    return None


def _blocks_from_xhtml(
    content: str, href: str, global_word_index: int
) -> tuple[list[Block], list[Word]]:
    """Tokenize XHTML into (blocks, words) in document order."""
    walker = _DocumentWalker()
    walker.feed(content)
    blocks = [
        Block(anchor=anchor, href=href, tag=tag, text=text, word_start=-1, word_count=0)
        for anchor, tag, text in walker.finish()
    ]

    words_out: list[Word] = []
    word_index = global_word_index
    for block in blocks:
        count = 0
        for match in re.finditer(WORD_RE, block.text):
            norm = normalize_word(match.group(0))
            if not norm:
                continue
            words_out.append(Word(text=match.group(0), norm=norm))
            count += 1
        block.word_start = word_index if count else -1
        block.word_count = count
        word_index += count

    return blocks, words_out


def _resolve(base_dir: str, href: str) -> str:
    # Package documents percent-encode their hrefs, while the archive stores the literal name.
    href = unquote(href.split("#")[0])
    if not href:
        return href
    if base_dir:
        return posixpath.normpath(posixpath.join(base_dir, href))
    return posixpath.normpath(href)


def open_epub(path: str | Path) -> Epub:
    """Parse an EPUB into ordered sync blocks and words."""
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise EpubError(f"{path.name} is not a zip archive") from exc
    with zf:
        names = zf.namelist()
        container = "META-INF/container.xml"
        if container not in names:
            raise EpubError(f"{path.name}: missing META-INF/container.xml")
        container_root = ET.fromstring(zf.read(container))
        rootfile = _find(container_root, "rootfile")
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path:
            raise EpubError(f"{path.name}: no rootfile in container.xml")
        root_dir = posixpath.dirname(opf_path)
        try:
            package = ET.fromstring(zf.read(opf_path))
        except KeyError as exc:
            raise EpubError(f"{path.name}: {opf_path} is not in the archive") from exc

        manifest: dict[str, tuple[str, str]] = {}
        for item in package.iter():
            if _local(item.tag) != "item":
                continue
            item_id, href = item.get("id"), item.get("href")
            if not item_id or not href:
                continue
            manifest[item_id] = (_resolve(root_dir, href), item.get("media-type", ""))

        spine: list[str] = []
        for itemref in package.iter():
            if _local(itemref.tag) != "itemref":
                continue
            if (itemref.get("linear") or "").lower() == "no":
                continue
            idref = itemref.get("idref")
            if idref:
                spine.append(idref)

        epub = Epub(path=path)

        for idref in spine:
            href, media = manifest.get(idref, ("", ""))
            if not href:
                continue
            if media and "xhtml" not in media and "html" not in media:
                continue
            if not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            try:
                content = zf.read(href).decode("utf-8", "replace")
            except KeyError:
                continue

            blocks, words = _blocks_from_xhtml(content, href, len(epub.words))
            if not words:
                continue
            epub.docs.append(
                Doc(href=href, word_start=len(epub.words), word_count=len(words))
            )
            epub.blocks.extend(blocks)
            epub.words.extend(words)

    return epub
