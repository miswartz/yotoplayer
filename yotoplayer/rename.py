import re
from pathlib import Path
from typing import Dict, List


# Characters invalid in Windows filenames
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in Windows/macOS/Linux filenames."""
    name = INVALID_CHARS_RE.sub("", name)
    name = name.strip(". ")
    return name or "Untitled"


def rename_chapters(
    chapter_files: List[Path],
    chapters: List[Dict],
    book_title: str,
) -> List[Path]:
    """
    Rename chapter files to the format: NN - Chapter Title.mp3

    If chapter titles are missing or generic (e.g., just numbers),
    falls back to: NN - Book Title.mp3
    """
    pad_width = max(len(str(len(chapter_files))), 2)
    renamed: List[Path] = []

    for i, (fpath, ch) in enumerate(zip(chapter_files, chapters)):
        chapter_title = ch.get("title", "").strip()

        # Determine if we have a meaningful chapter title
        if _is_meaningful_title(chapter_title, book_title):
            display_title = chapter_title
        else:
            display_title = book_title

        idx = str(i + 1).zfill(pad_width)
        new_name = sanitize_filename(f"{idx} - {display_title}") + ".mp3"
        new_path = fpath.parent / new_name

        # Handle name collisions
        if new_path.exists() and new_path != fpath:
            new_name = (
                sanitize_filename(f"{idx} - {display_title} (Part {i + 1})") + ".mp3"
            )
            new_path = fpath.parent / new_name

        fpath.rename(new_path)
        renamed.append(new_path)

    return renamed


def _is_meaningful_title(chapter_title: str, book_title: str) -> bool:
    """Check if a chapter title is meaningful (not just a number or generic label)."""
    if not chapter_title:
        return False

    # Pure numbers or "Chapter N" / "Part N" patterns aren't meaningful
    stripped = chapter_title.strip()
    if stripped.isdigit():
        return False
    if re.match(r"^(chapter|part|section)\s*\d*$", stripped, re.IGNORECASE):
        return False

    # If it's just the book title repeated, not meaningful
    if stripped.lower() == book_title.lower():
        return False

    return True
