#!/usr/bin/env python3
"""List audiobook folders that contain audio files but no epub.

Usage: python missing_epub.py ./audiobookshelf-data/library/
"""
import sys
from pathlib import Path

AUDIO = {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    for folder in sorted([root, *(p for p in root.rglob("*") if p.is_dir())]):
        exts = {f.suffix.lower() for f in folder.iterdir() if f.is_file()}
        if exts & AUDIO and ".epub" not in exts:
            print(folder)


if __name__ == "__main__":
    main()
