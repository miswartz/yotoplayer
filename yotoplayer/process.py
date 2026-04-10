import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, ID3NoHeaderError
from tqdm import tqdm

# Use up to half of available cores (ffmpeg itself can use some parallelism)
_MAX_WORKERS = min(max(os.cpu_count() // 2, 1), 16)


def check_ffmpeg() -> str:
    """Return the ffmpeg executable path, or exit if not found."""
    path = shutil.which("ffmpeg")
    if not path:
        print(
            "Error: ffmpeg not found. Install it: winget install ffmpeg",
            file=sys.stderr,
        )
        sys.exit(1)
    return path


def merge_parts(part_files: List[Path], output_file: Path) -> None:
    """Concatenate MP3 part files into a single MP3 using ffmpeg."""
    ffmpeg = check_ffmpeg()

    # Create a concat list file
    concat_list = output_file.parent / "concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for pf in sorted(part_files):
            # ffmpeg concat requires escaped single quotes in paths
            safe_path = str(pf.resolve()).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(output_file),
    ]

    print("Merging parts into single file...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    concat_list.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"Error merging parts: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def _split_one_chapter(
    ffmpeg: str,
    merged_file: str,
    start: float,
    end: float,
    out_path: str,
) -> Tuple[str, bool, str]:
    """Split a single chapter from the merged file. Returns (out_path, success, error)."""
    cmd = [
        ffmpeg, "-y", "-i", merged_file,
        "-ss", str(start), "-to", str(end),
        "-acodec", "libmp3lame", "-q:a", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return (out_path, False, result.stderr)
    p = Path(out_path)
    if not p.exists() or p.stat().st_size == 0:
        return (out_path, False, "produced empty file")
    return (out_path, True, "")


def split_chapters(
    merged_file: Path,
    chapters: List[Dict],
    output_dir: Path,
) -> List[Path]:
    """Split a merged audio file into per-chapter MP3 files (parallel)."""
    ffmpeg = check_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)

    pad_width = max(len(str(len(chapters))), 2)
    tasks = []  # (index, out_path, chapter)
    for i, ch in enumerate(chapters):
        idx = str(i + 1).zfill(pad_width)
        out_path = output_dir / f"chapter_{idx}.mp3"
        tasks.append((i, out_path, ch))

    print(f"Splitting into {len(chapters)} chapters ({_MAX_WORKERS} workers)...")
    chapter_files = [None] * len(chapters)

    with ProcessPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {}
        for i, out_path, ch in tasks:
            f = pool.submit(
                _split_one_chapter,
                ffmpeg, str(merged_file),
                ch["start"], ch["end"], str(out_path),
            )
            futures[f] = (i, out_path, ch)

        with tqdm(total=len(chapters), desc="  Splitting", unit="ch") as pbar:
            for f in as_completed(futures):
                i, out_path, ch = futures[f]
                out_str, ok, err = f.result()
                if not ok:
                    print(f"\n  Error splitting chapter {i+1}: {err}", file=sys.stderr)
                    sys.exit(1)
                chapter_files[i] = out_path
                pbar.set_postfix_str(ch.get("title", "")[:30])
                pbar.update(1)

    # Print chapter list in order
    for i, (out_path, ch) in enumerate(zip(chapter_files, chapters)):
        idx = str(i + 1).zfill(pad_width)
        print(f"  Chapter {idx}: {ch.get('title', f'Chapter {i+1}')}")

    return chapter_files


def write_id3_tags(
    chapter_files: List[Path],
    chapters: List[Dict],
    title: str,
    authors: List[str],
    cover_path: Optional[Path] = None,
) -> None:
    """Write ID3 tags to each chapter MP3."""
    cover_bytes = None
    if cover_path and cover_path.exists():
        cover_bytes = cover_path.read_bytes()

    total_tracks = len(chapter_files)
    for i, (fpath, ch) in enumerate(zip(chapter_files, chapters)):
        try:
            tags = ID3(str(fpath))
        except ID3NoHeaderError:
            tags = ID3()

        chapter_title = ch.get("title", f"Chapter {i + 1}")
        tags.add(TIT2(encoding=3, text=chapter_title))
        tags.add(TALB(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=", ".join(authors)))
        tags.add(TRCK(encoding=3, text=f"{i + 1}/{total_tracks}"))

        if cover_bytes:
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # front cover
                    desc="Cover",
                    data=cover_bytes,
                )
            )

        tags.save(str(fpath))


def normalize_volume(chapter_dir: Path) -> None:
    """Normalize volume of all MP3s in a directory.

    Tries loudgain first (album mode), falls back to ffmpeg loudnorm.
    """
    mp3_files = sorted(chapter_dir.glob("*.mp3"))
    if not mp3_files:
        return

    loudgain = shutil.which("loudgain")
    if loudgain:
        _normalize_loudgain(loudgain, mp3_files)
    else:
        print("  loudgain not found, using ffmpeg loudnorm filter...")
        _normalize_ffmpeg(mp3_files)


def _normalize_loudgain(loudgain: str, mp3_files: List[Path]) -> None:
    """Normalize using loudgain in album mode."""
    print(f"Normalizing {len(mp3_files)} files with loudgain...")
    cmd = [loudgain, "-a", "-k", "-s", "i"] + [str(f) for f in mp3_files]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  loudgain warning: {result.stderr}", file=sys.stderr)
    else:
        print("  Volume normalized (ReplayGain 2.0, album mode).")


def _normalize_one_file(
    ffmpeg: str,
    fpath: str,
) -> Tuple[str, bool, str]:
    """Normalize a single MP3 file. Returns (fpath, success, error)."""
    tmp = str(Path(fpath).with_suffix(".norm.mp3"))
    cmd = [
        ffmpeg, "-y", "-i", fpath,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-acodec", "libmp3lame", "-q:a", "2",
        tmp,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        Path(tmp).unlink(missing_ok=True)
        return (fpath, False, result.stderr)
    # Replace original with normalized version
    Path(tmp).replace(fpath)
    return (fpath, True, "")


def _normalize_ffmpeg(mp3_files: List[Path]) -> None:
    """Normalize files using ffmpeg's loudnorm filter (EBU R128), parallel."""
    ffmpeg = check_ffmpeg()

    print(f"Normalizing {len(mp3_files)} files with ffmpeg loudnorm ({_MAX_WORKERS} workers)...")

    with ProcessPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {}
        for fpath in mp3_files:
            f = pool.submit(_normalize_one_file, ffmpeg, str(fpath))
            futures[f] = fpath

        with tqdm(total=len(mp3_files), desc="  Normalizing", unit="file") as pbar:
            for f in as_completed(futures):
                fpath = futures[f]
                _, ok, err = f.result()
                if not ok:
                    print(f"\n  Warning: normalize failed for {fpath.name}", file=sys.stderr)
                pbar.set_postfix_str(fpath.name[:30])
                pbar.update(1)

    print("  Volume normalized (EBU R128, -16 LUFS).")
