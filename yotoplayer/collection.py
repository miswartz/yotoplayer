"""Split a collection audiobook into individual books using chapter titles.

A collection audiobook has chapters like:
    "Tonight on the Titanic, Chapter 1"
    "Tonight on the Titanic, Chapter 2"
    "Buffalo Before Breakfast, Chapter 1"
    "Buffalo Before Breakfast, Chapter 2"

The first chapter of each book is identified by ", Chapter 1" (case-insensitive).
Everything before that comma is the book title. Subsequent chapters belong to
that book until the next "..., Chapter 1" appears.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Matches ", Chapter 1" at the end of a chapter title (case-insensitive)
_CHAPTER_ONE_RE = re.compile(r",\s*Chapter\s+1\s*$", re.IGNORECASE)

# Matches ", Chapter N" at the end — used to extract the book title portion
_CHAPTER_NUM_RE = re.compile(r",\s*Chapter\s+\d+\s*$", re.IGNORECASE)


def _extract_book_title(chapter_title: str) -> Optional[str]:
    """Extract the book title from a chapter title like 'Book Name, Chapter 1'.

    Returns the book title portion, or None if the pattern doesn't match.
    """
    match = _CHAPTER_NUM_RE.search(chapter_title)
    if match:
        return chapter_title[: match.start()].strip()
    return None


def split_collection(
    chapters: List[Dict],
    chapter_files: List[Path],
) -> List[Dict]:
    """Split chapters and files into individual books based on chapter titles.

    Parameters
    ----------
    chapters : list of dict
        Chapter metadata (must include "title", "start", "end").
    chapter_files : list of Path
        Corresponding MP3 files from split_chapters.

    Returns
    -------
    list of dict
        Each dict has:
        - "title": str — the individual book title
        - "chapters": list of dict — chapter metadata for this book
        - "files": list of Path — MP3 files for this book

    Raises
    ------
    ValueError
        If no "Chapter 1" boundaries are found in the chapter titles.
    """
    if len(chapters) != len(chapter_files):
        raise ValueError(
            f"Chapter count mismatch: {len(chapters)} chapters vs "
            f"{len(chapter_files)} files"
        )

    # Find boundaries: indices where a new book starts
    boundaries: List[Tuple[int, str]] = []
    for i, ch in enumerate(chapters):
        title = ch.get("title", "")
        if _CHAPTER_ONE_RE.search(title):
            book_title = _extract_book_title(title)
            if book_title:
                boundaries.append((i, book_title))

    if not boundaries:
        raise ValueError(
            "No collection boundaries found. Chapter titles must follow the "
            'pattern "[Book Title], Chapter 1" to identify book boundaries.'
        )

    books: List[Dict] = []
    for bi, (start_idx, book_title) in enumerate(boundaries):
        # End index is the start of the next book, or end of all chapters
        if bi + 1 < len(boundaries):
            end_idx = boundaries[bi + 1][0]
        else:
            end_idx = len(chapters)

        book_chapters = chapters[start_idx:end_idx]
        book_files = chapter_files[start_idx:end_idx]

        books.append({
            "title": book_title,
            "chapters": book_chapters,
            "files": book_files,
        })

    # If there are chapters before the first "Chapter 1", include them
    # as a preamble in the first book
    if boundaries and boundaries[0][0] > 0:
        preamble_chapters = chapters[: boundaries[0][0]]
        preamble_files = chapter_files[: boundaries[0][0]]
        books[0]["chapters"] = preamble_chapters + books[0]["chapters"]
        books[0]["files"] = preamble_files + books[0]["files"]

    print(f"\nCollection split into {len(books)} books:")
    for book in books:
        print(f"  • {book['title']} ({len(book['chapters'])} chapters)")

    return books


def find_book_cover(
    book_title: str,
    cards_dir: Path,
    fallback_cover: Optional[Path] = None,
) -> Optional[Path]:
    """Find a cover image for an individual book in the _cards directory.

    Checks _cards/ for an image file whose stem matches the book title.
    Falls back to the collection-level cover from the audiobook metadata.

    Parameters
    ----------
    book_title : str
        The individual book title to search for.
    cards_dir : Path
        The _cards/ directory under the YotoPlayer library.
    fallback_cover : Path or None
        The collection-level cover.jpg from the audiobook download.

    Returns
    -------
    Path or None
        Path to the best matching cover image.
    """
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

    if cards_dir.is_dir():
        # Exact match
        for ext in IMAGE_EXTS:
            candidate = cards_dir / f"{book_title}{ext}"
            if candidate.exists():
                return candidate

        # Case-insensitive match
        lower_title = book_title.lower()
        for f in cards_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                if f.stem.lower() == lower_title:
                    return f

    # Fall back to collection cover
    if fallback_cover and fallback_cover.exists():
        return fallback_cover

    return None


def is_collection_dir(d: Path) -> bool:
    """Check if a directory is a collection of books (vs a single book).

    A collection directory has NO .mp3 files at its root but has subdirectories
    (not .work/icons/_cards/Pt.*) that contain .mp3 files.
    """
    _SKIP = {".work", "icons", "_cards"}
    if any(d.glob("*.mp3")):
        return False
    for sub in d.iterdir():
        if sub.is_dir() and sub.name not in _SKIP and not sub.name.startswith("Pt. "):
            if any(sub.glob("*.mp3")):
                return True
    return False


def get_collection_book_dirs(d: Path) -> List[Path]:
    """Return the individual book subdirectories of a collection directory."""
    _SKIP = {".work", "icons", "_cards"}
    return sorted(
        sub for sub in d.iterdir()
        if sub.is_dir() and sub.name not in _SKIP and not sub.name.startswith("Pt. ")
    )
