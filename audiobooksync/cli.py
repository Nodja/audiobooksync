"""Command line entry point: ``abs-sync``.

Reads folders of paired ebook/audio files and writes a ``.smil`` sidecar next to each epub.  The
epub is never modified and no audiobookshelf server is needed.  The model is fetched from the
Hugging Face Hub on first use.

    abs-sync "D:\\books"                          # every book under D:\\books, skipping done ones
    abs-sync "D:\\books" --overwrite              # rewrite every sidecar, done or not
    abs-sync "D:\\books" --update                 # rewrite sidecars not made by this version
    abs-sync "D:\\books\\Author" --dry-run        # show what would be written
    abs-sync folder "D:\\books" --limit 1         # just the first pair, to check it works
    abs-sync pair book.epub book.m4b             # one explicit pair, written beside the epub

``abs-sync <path>`` is shorthand for ``abs-sync folder <path>`` (default: the current directory).
Folders are walked recursively and an ebook is only paired with audio in the same directory.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, model
from .audio import AudioError, find_audio_files, find_epub_files, have_ffmpeg
from .epub import EpubError
from .smil import sidecar_version
from .sync import (
    DEFAULT_CHUNK_SECONDS,
    SyncError,
    SyncResult,
    summarise,
    synchronize,
    write_result,
)

SUBCOMMANDS = ("pair", "folder")

#: Failures that belong to one book: reported, and the run carries on.
BOOK_ERRORS = (SyncError, AudioError, EpubError, model.ModelError)


def _sidecar_path(epub: Path) -> Path:
    """Sidecar path for an epub: same basename, ``.smil`` extension, beside it."""
    return epub.with_suffix(".smil")


def _match_pairs(epub_files: list[Path], audio_files: list[Path]) -> list[tuple[Path, Path]]:
    """Pair each epub with an audio file in the same folder, by file name."""
    pairs: list[tuple[Path, Path]] = []
    by_stem = {p.stem.lower(): p for p in audio_files}

    for epub in epub_files:
        stem = epub.stem.lower()
        if stem in by_stem:
            pairs.append((epub, by_stem[stem]))
            continue
        # Fall back to prefix similarity: "Title" vs "Title (Unabridged)".
        candidates = [
            audio for key, audio in by_stem.items()
            if key.startswith(stem[:24]) or stem.startswith(key[:24])
        ]
        if len(candidates) == 1:
            pairs.append((epub, candidates[0]))

    if not pairs and len(epub_files) == 1 and len(audio_files) >= 1:
        # One epub and several audio files: assume the largest is the full book.
        largest = max(audio_files, key=lambda p: p.stat().st_size)
        pairs.append((epub_files[0], largest))

    return pairs


def _find_pairs(root: Path) -> list[tuple[Path, Path]]:
    """Every ebook/audio pair at or below `root`, matched within each directory only."""
    folders = [root, *(p for p in sorted(root.rglob("*")) if p.is_dir())]
    pairs: list[tuple[Path, Path]] = []
    for folder in folders:
        epubs = find_epub_files(folder)
        if not epubs:
            continue
        pairs.extend(_match_pairs(epubs, find_audio_files(folder)))
    return pairs


def _progress(message: str) -> None:
    print(message, flush=True)


class _Status:
    """One stderr line that overwrites itself until the book is done."""

    def __init__(self) -> None:
        self.enabled = sys.stderr.isatty()
        self.width = 0

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        sys.stderr.write("\r" + text + " " * max(0, self.width - len(text)))
        sys.stderr.flush()
        self.width = len(text)

    def clear(self) -> None:
        if self.enabled and self.width:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()
            self.width = 0


def _line(result: SyncResult) -> str:
    """One line per book: only what was produced. Anything wrong gets its own line instead."""
    report = result.report
    parts = [f"{report.placed:>6} words", f"{len(result.pars):>4} pars",
             f"{report.coverage * 100:5.1f}% audio"]
    if report.mean_score:
        parts.append(f"{report.mean_score:+.2f}/label")
    return "  ".join(parts)


def _problems(result: SyncResult) -> None:
    """Print every warning, one per line: each names a place where audio and text disagree."""
    for problem in result.report.warnings:
        print(f"  ! {problem}", file=sys.stderr, flush=True)


def _runner(status: _Status, name: str, started: float):
    """A progress callback for one book, rendered into `status`."""
    def on_status(done: int, total: int, detail: str) -> None:
        if total <= 0:
            status.update(f"Processing {name}... {detail}")
            return
        elapsed = time.time() - started
        left = f"{(elapsed / done) * (total - done):.0f}s left" if done else "starting"
        status.update(f"Processing {name}... {done / total * 100:3.0f}%  {left}  {detail}")

    return on_status


def _build_aligner(args: argparse.Namespace, write=_progress):
    from .onnx_ctc import OnnxParakeetAligner

    return OnnxParakeetAligner(repo=args.repo, variant=args.variant, progress=write)


def _publish(
    result: SyncResult,
    epub: Path,
    out: Path,
    args: argparse.Namespace,
    started: float,
    title: str = "",
) -> bool:
    """Print one book's report and write its sidecar; True when a sidecar was written."""
    if args.verbose:
        print(summarise(result))
    if args.dry_run:
        print(f"Processing {epub.name}...would write {_line(result)}", flush=True)
        _problems(result)
        return False
    write_result(
        result,
        out,
        title=title or epub.stem,
        word_text=args.word_text,
        pretty=not args.compact,
    )
    print(f"Processing {epub.name}...Done in {time.time() - started:.0f}s  {_line(result)}", flush=True)
    _problems(result)
    return True


def cmd_pair(args: argparse.Namespace) -> int:
    epub, audio = Path(args.epub), Path(args.audio)
    for path in (epub, audio):
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 2

    out = Path(args.out) if args.out else _sidecar_path(epub)
    started = time.time()
    status = _Status()

    try:
        result = synchronize(
            epub,
            audio,
            aligner=_build_aligner(args),
            chunk_seconds=args.chunk,
            start_hint=args.start_word,
            progress=_progress if args.verbose else None,
            on_status=_runner(status, epub.name, started),
        )
    except BOOK_ERRORS as exc:
        status.clear()
        print(f"Processing {epub.name}...FAILED: {exc}", file=sys.stderr)
        return 1

    status.clear()
    _publish(result, epub, out, args, started, title=args.title)
    return 0


def _needs_sidecar(sidecar: Path, args: argparse.Namespace) -> bool:
    """Whether a book is written: always with --overwrite, never over a sidecar already there
    unless --update finds it was made by a different version."""
    if not sidecar.exists() or args.force:
        return True
    return args.update and sidecar_version(sidecar) != __version__


def cmd_folder(args: argparse.Namespace) -> int:
    root = Path(args.folder)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    pairs = _find_pairs(root)
    if not pairs:
        print(f"error: no ebook/audio pairs at or below {root}", file=sys.stderr)
        return 2

    selected = pairs[: args.limit] if args.limit else pairs
    todo = [
        (epub, audio, _sidecar_path(epub))
        for epub, audio in selected
        if _needs_sidecar(_sidecar_path(epub), args)
    ]
    print(f"{len(pairs)} pair(s) at or below {root}: {len(selected) - len(todo)} already done, "
          f"{len(todo)} to write")
    if not todo:
        return 0

    aligner = None
    failures = 0
    written = 0
    status = _Status()
    for epub, audio, out in todo:
        name = epub.name
        started = time.time()
        status.update(f"Processing {name}... ")
        try:
            if aligner is None:
                aligner = _build_aligner(args)
            result = synchronize(
                epub,
                audio,
                aligner=aligner,
                chunk_seconds=args.chunk,
                progress=_progress if args.verbose else None,
                on_status=_runner(status, name, started),
            )
        except BOOK_ERRORS as exc:
            status.clear()
            print(f"Processing {name}...FAILED: {exc}", file=sys.stderr, flush=True)
            failures += 1
            continue

        status.clear()
        written += _publish(result, epub, out, args, started)

    if failures:
        done = len(selected) - len(todo)
        tally = f"{written} written" if not args.dry_run else f"{len(todo)} would be written"
        parts = [tally]
        if done:
            parts.append(f"{done} already done")
        parts.append(f"{failures} FAILED")
        print("  " + ", ".join(parts), file=sys.stderr, flush=True)
    return 1 if failures else 0


def _add_shared(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Options that mean the same thing for a single pair and for a whole folder."""
    parser.add_argument(
        "--repo",
        default=model.DEFAULT_REPO,
        help=f"Hub repository publishing the ONNX graph (default: {model.DEFAULT_REPO})",
    )
    parser.add_argument(
        "--variant",
        choices=model.available_variants(),
        default=model.DEFAULT_VARIANT,
        help="graph precision to fetch; q4 is 3.8x smaller than fp32 and aligned identically "
             "on the test book (default: %(default)s)",
    )
    parser.add_argument(
        "--chunk",
        type=float,
        default=DEFAULT_CHUNK_SECONDS,
        help=f"seconds of audio per alignment pass (default: {DEFAULT_CHUNK_SECONDS:.0f})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written without writing anything",
    )
    parser.add_argument(
        "--word-text",
        action="store_true",
        help="include each word's text in word_timestamps; roughly 15%% bigger, but the payload "
             "can be read without the epub beside it",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit one <par> per line instead of indenting each element (a few %% smaller)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print the full per-book report instead of one line each",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abs-sync",
        description=(
            f"abs-sync {__version__}. Align audiobooks against their epub and write SMIL media overlays with "
            "sentence- and word-level timestamps. The epub is never modified."
        ),
        epilog=(
            "examples:\n"
            "  abs-sync D:\\books                    sync every book under a folder\n"
            "  abs-sync D:\\books --dry-run          list what would be written\n"
            "  abs-sync folder D:\\books --limit 1    sync the first pair only\n"
            "  abs-sync pair book.epub book.m4b      sync one explicit pair\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--version", action="version", version=f"abs-sync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    pair = _add_shared(sub.add_parser("pair", help="synchronize one epub with one audio file"))
    pair.add_argument("epub", help="path to the .epub")
    pair.add_argument("audio", help="path to the audio file")
    pair.add_argument("--out", help="where to write the .smil (default: beside the epub)")
    pair.add_argument("--title", help="override the title recorded in the SMIL head")
    pair.add_argument(
        "--start-word",
        type=int,
        help="skip the narration-start search and begin at this ebook word index",
    )
    pair.set_defaults(func=cmd_pair)

    folder = _add_shared(sub.add_parser("folder", help="synchronize every pair at or below a folder"))
    folder.add_argument(
        "folder", nargs="?", default=".", help="folder to walk (default: the current directory)"
    )
    folder.add_argument(
        "--limit", type=int, help="process at most this many pairs (useful for a trial run)"
    )
    rewrite = folder.add_mutually_exclusive_group()
    rewrite.add_argument(
        "--overwrite",
        "--force",
        dest="force",
        action="store_true",
        help="write every sidecar even when one is already there (alias: --force)",
    )
    rewrite.add_argument(
        "--update",
        action="store_true",
        help=f"like --overwrite, but leave sidecars already written by this version "
             f"({__version__}) alone",
    )
    folder.set_defaults(func=cmd_folder)

    return parser


def _normalise_argv(argv: list[str]) -> list[str]:
    """``abs-sync [options] [path]`` into ``abs-sync folder [options] [path]``.

    Done before argparse because argparse resets a parent's namespace from a subparser's defaults,
    which would drop options given before the subcommand name.
    """
    if argv and argv[0] in SUBCOMMANDS:
        return argv
    if argv and argv[0] in ("-h", "--help", "--version"):
        return argv  # top-level help, not the folder parser's
    return ["folder", *argv] if argv else ["folder", "."]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_normalise_argv(list(sys.argv[1:] if argv is None else argv)))

    if not have_ffmpeg():
        print(
            "error: ffmpeg and ffprobe are required and were not found on PATH",
            file=sys.stderr,
        )
        return 2

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
