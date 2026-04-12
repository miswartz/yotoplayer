"""Enforce Yoto playlist limits: max tracks, max track size, max playlist size.

Yoto card constraints:
  - 100 tracks maximum per playlist
  - 100 MB maximum per individual track
  - 500 MB maximum total audio per playlist

When limits are exceeded this module will:
  1. Split any single track that exceeds 100 MB.
  2. If total audio ≤ 500 MB, merge chapters from the end in pairs until
     the track count is ≤ 100  (iterating if merges create oversized tracks).
  3. If total audio > 500 MB, partition into multiple playlists (named
     "… Pt. 1", "… Pt. 2", etc.) each respecting all three limits.
"""

import math
from pathlib import Path
from typing import Dict, List, Tuple

from .process import get_audio_duration, merge_chapter_pair, split_chapter_file

# ---------------------------------------------------------------------------
# Yoto hard limits
# ---------------------------------------------------------------------------
MAX_TRACKS_PER_PLAYLIST = 100
MAX_BYTES_PER_TRACK = 100 * 1024 * 1024        # 100 MB
MAX_BYTES_PER_PLAYLIST = 500 * 1024 * 1024      # 500 MB


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enforce_playlist_limits(
    chapter_files: List[Path],
    chapters: List[Dict],
    book_title: str,
    output_dir: Path,
) -> List[Dict]:
    """Enforce Yoto playlist limits on the chapter files.

    Parameters
    ----------
    chapter_files : list of Path
        MP3 files produced by split_chapters (in order).
    chapters : list of dict
        Matching chapter metadata (must include at least ``"title"``).
    book_title : str
        Used for naming playlists when multiple are required.
    output_dir : Path
        Working directory for any intermediate merge/split files.

    Returns
    -------
    list of dict
        Each dict has keys ``"title"`` (str), ``"files"`` (list of Path),
        and ``"chapters"`` (list of dict).  A single-element list means
        everything fit in one playlist.
    """
    files = list(chapter_files)
    ch_meta = [dict(ch) for ch in chapters]

    # Step 1: split any individual track that exceeds 100 MB
    files, ch_meta = _split_oversized(files, ch_meta, output_dir)

    total_size = sum(f.stat().st_size for f in files)

    if total_size <= MAX_BYTES_PER_PLAYLIST:
        # Try to fit everything in a single playlist — merge if needed.
        files, ch_meta = _enforce_track_limits(files, ch_meta, output_dir)
        return [{"title": book_title, "files": files, "chapters": ch_meta}]

    # Total exceeds 500 MB → need multiple playlists.
    playlists = _partition_playlists(files, ch_meta, book_title)

    # Each partition must independently obey the per-track size & count limits.
    for p in playlists:
        p["files"], p["chapters"] = _enforce_track_limits(
            p["files"], p["chapters"], output_dir,
        )

    return playlists


# ---------------------------------------------------------------------------
# Merge chapters (reduce track count)
# ---------------------------------------------------------------------------

def _merge_excess(
    files: List[Path],
    chapters: List[Dict],
    output_dir: Path,
) -> Tuple[List[Path], List[Dict]]:
    """Merge chapter pairs from the end until track count ≤ MAX_TRACKS.

    Strategy: pair the *last* two chapters first, then work inward.
    If one pass isn't enough (e.g. 250 chapters), repeat.
    """
    while len(files) > MAX_TRACKS_PER_PLAYLIST:
        excess = len(files) - MAX_TRACKS_PER_PLAYLIST
        max_possible_pairs = len(files) // 2
        pairs_to_merge = min(excess, max_possible_pairs)

        keep_count = len(files) - 2 * pairs_to_merge

        keep_files = files[:keep_count]
        keep_chapters = chapters[:keep_count]

        merge_files = files[keep_count:]
        merge_chapters = chapters[keep_count:]

        new_files: List[Path] = []
        new_chapters: List[Dict] = []

        print(
            f"  Merging {pairs_to_merge} chapter pairs from the end "
            f"({len(files)} → {keep_count + pairs_to_merge} tracks)..."
        )

        for j in range(0, len(merge_files), 2):
            f1, f2 = merge_files[j], merge_files[j + 1]
            ch1, ch2 = merge_chapters[j], merge_chapters[j + 1]

            merged_name = f"merged_{f1.stem}_{f2.stem}.mp3"
            merged_path = output_dir / merged_name
            merge_chapter_pair(f1, f2, merged_path)

            # Clean up originals
            f1.unlink(missing_ok=True)
            f2.unlink(missing_ok=True)

            merged_chapter = {
                "title": f"{ch1.get('title', '')} & {ch2.get('title', '')}",
            }
            new_files.append(merged_path)
            new_chapters.append(merged_chapter)

        files = keep_files + new_files
        chapters = keep_chapters + new_chapters

    return files, chapters


# ---------------------------------------------------------------------------
# Split oversized tracks
# ---------------------------------------------------------------------------

def _split_oversized(
    files: List[Path],
    chapters: List[Dict],
    output_dir: Path,
) -> Tuple[List[Path], List[Dict]]:
    """Split any chapter file that exceeds MAX_BYTES_PER_TRACK."""
    new_files: List[Path] = []
    new_chapters: List[Dict] = []
    did_split = False

    for fpath, ch in zip(files, chapters):
        size = fpath.stat().st_size
        if size > MAX_BYTES_PER_TRACK:
            num_parts = math.ceil(size / MAX_BYTES_PER_TRACK)
            if not did_split:
                print("Splitting oversized chapters (>100 MB per track)...")
                did_split = True
            print(f"  Splitting {ch.get('title', fpath.name)} into {num_parts} parts")
            parts = split_chapter_file(fpath, num_parts, output_dir)

            fpath.unlink(missing_ok=True)

            for k, part in enumerate(parts):
                split_ch = dict(ch)
                split_ch["title"] = f"{ch.get('title', '')} (Part {k + 1})"
                new_files.append(part)
                new_chapters.append(split_ch)
        else:
            new_files.append(fpath)
            new_chapters.append(ch)

    return new_files, new_chapters


# ---------------------------------------------------------------------------
# Iterative merge + split until both limits satisfied
# ---------------------------------------------------------------------------

def _enforce_track_limits(
    files: List[Path],
    chapters: List[Dict],
    output_dir: Path,
    _max_iterations: int = 10,
) -> Tuple[List[Path], List[Dict]]:
    """Iterate merge / split until both per-track size and count are OK."""
    for _ in range(_max_iterations):
        changed = False

        # Merge if too many tracks
        if len(files) > MAX_TRACKS_PER_PLAYLIST:
            files, chapters = _merge_excess(files, chapters, output_dir)
            changed = True

        # Split any track that merging made too large
        before = len(files)
        files, chapters = _split_oversized(files, chapters, output_dir)
        if len(files) != before:
            changed = True

        if not changed:
            break

    return files, chapters


# ---------------------------------------------------------------------------
# Partition into multiple playlists
# ---------------------------------------------------------------------------

def _partition_playlists(
    files: List[Path],
    chapters: List[Dict],
    book_title: str,
) -> List[Dict]:
    """Greedily partition chapters into playlists, each ≤ 500 MB / 100 tracks."""
    playlists: List[Dict] = []
    cur_files: List[Path] = []
    cur_chapters: List[Dict] = []
    cur_size = 0

    for fpath, ch in zip(files, chapters):
        fsize = fpath.stat().st_size

        would_exceed_size = cur_size + fsize > MAX_BYTES_PER_PLAYLIST
        would_exceed_tracks = len(cur_files) >= MAX_TRACKS_PER_PLAYLIST

        if cur_files and (would_exceed_size or would_exceed_tracks):
            playlists.append({"files": cur_files, "chapters": cur_chapters})
            cur_files = []
            cur_chapters = []
            cur_size = 0

        cur_files.append(fpath)
        cur_chapters.append(ch)
        cur_size += fsize

    if cur_files:
        playlists.append({"files": cur_files, "chapters": cur_chapters})

    # Assign titles
    if len(playlists) == 1:
        playlists[0]["title"] = book_title
    else:
        print(
            f"\nBook exceeds 500 MB — splitting into {len(playlists)} playlists."
        )
        for i, p in enumerate(playlists):
            p["title"] = f"{book_title} Pt. {i + 1}"

    return playlists
