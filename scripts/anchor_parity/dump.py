"""Dump the generator's view of every epub under a folder, for `check.mjs` to compare against.

For each epub this writes the sync blocks (document, anchor, word count, text) and the content
documents themselves, so the client's walk can be run over exactly the markup the generator saw.
"""
import json
import sys
import zipfile
from pathlib import Path

from audiobooksync.epub import open_epub

out, root = Path(sys.argv[1]), Path(sys.argv[2])
books = []
for index, path in enumerate(sorted(root.rglob("*.epub"))):
    try:
        epub = open_epub(path)
    except Exception as exc:
        print(f"skip {path.name}: {exc}", file=sys.stderr)
        continue
    folder = out / f"b{index}"
    (folder / "docs").mkdir(parents=True, exist_ok=True)
    hrefs = sorted({block.href for block in epub.blocks})
    with zipfile.ZipFile(path) as archive:
        for href in hrefs:
            (folder / "docs" / href.replace("/", "__")).write_bytes(archive.read(href))
    blocks = [[b.href, b.anchor, b.word_count, b.text] for b in epub.blocks]
    (folder / "py.json").write_text(json.dumps({"blocks": blocks, "hrefs": hrefs}), encoding="utf8")
    books.append({"dir": folder.name, "name": path.name})
(out / "books.json").write_text(json.dumps(books), encoding="utf8")
