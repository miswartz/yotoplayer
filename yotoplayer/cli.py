import json
import sys
from pathlib import Path

import click

from .auth import get_client, setup_auth, print_libraries, get_library_keys
from .search import search_audiobooks, display_results
from .download import ensure_borrowed, download_audiobook
from .process import (
    merge_parts,
    split_chapters,
    write_id3_tags,
    normalize_volume,
)
from .rename import rename_chapters
from .yoto import device_code_auth, is_yoto_authenticated, upload_to_yoto
from .icons import generate_chapter_icons
from .playlist import enforce_playlist_limits


@click.group()
def main():
    """YotoPlayer — Search, download, and process Libby audiobooks."""
    pass


@main.command()
def auth():
    """Set up or verify Libby authentication."""
    client = setup_auth()
    print()
    print_libraries(client)


@main.command(name="yoto-auth")
def yoto_auth():
    """Set up Yoto account authentication (OAuth2 device code flow)."""
    device_code_auth()
    print("\nYoto authentication complete. You can now upload playlists.")


@main.command(name="get")
@click.argument("query")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Output directory (default: ~/YotoPlayer/<Title>)",
)
@click.option(
    "--keep-intermediate",
    is_flag=True,
    default=False,
    help="Keep intermediate files (merged audio, parts, etc.)",
)
@click.option(
    "--normalize",
    is_flag=True,
    default=False,
    help="Normalize volume across chapters (EBU R128, -16 LUFS).",
)
@click.option(
    "--no-upload",
    is_flag=True,
    default=False,
    help="Skip uploading to Yoto.",
)
@click.option(
    "--icons",
    is_flag=True,
    default=False,
    help="Generate AI pixel art chapter icons (requires OPENAI_API_KEY and RETRO_DIFFUSION_API_KEY).",
)
def get_audiobook(query, output, keep_intermediate, normalize, no_upload, icons):
    """Search for an audiobook and download + process it.

    QUERY is the search term (e.g., "dinosaurs before dark").
    """
    # 1. Authenticate
    client = get_client()
    library_keys = get_library_keys(client)
    if not library_keys:
        print("Error: No library cards found. Run 'yotoplayer auth' first.", file=sys.stderr)
        sys.exit(1)

    # 2. Search
    print(f'\nSearching for "{query}"...\n')
    results = search_audiobooks(query, library_keys, limit=20)
    display_results(results)

    if not results:
        sys.exit(0)

    # 3. Prompt for selection
    print()
    while True:
        choice = input("Enter number to download (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            sys.exit(0)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                break
            print(f"Please enter a number between 1 and {len(results)}.")
        except ValueError:
            print("Invalid input.")

    selected = results[idx]
    print(f'\nSelected: {selected.title} — {selected.author}\n')

    # 4. Borrow if needed
    loan = ensure_borrowed(client, selected.media_id, selected.available_copies)

    # 5. Set up output directory
    if output:
        output_dir = Path(output)
    else:
        output_dir = Path.home() / "YotoPlayer" / _safe_dirname(selected.title)
    work_dir = output_dir / ".work"

    # 6. Download
    print()
    part_files, chapters = download_audiobook(client, loan, work_dir)

    if not chapters:
        print("Warning: No chapter metadata found. The file will not be split.")

    # 7. Merge parts into single file
    merged_file = work_dir / "merged.mp3"
    if not merged_file.exists():
        merge_parts(part_files, merged_file)
    else:
        print("Merged file already exists, skipping merge.")

    # 8. Load full metadata
    chapters_json = work_dir / "chapters.json"
    with open(chapters_json, "r", encoding="utf-8") as f:
        book_meta = json.load(f)

    book_title = book_meta["title"]
    authors = book_meta["authors"]
    chapter_list = book_meta["chapters"]
    cover_path = Path(book_meta["cover"]) if book_meta.get("cover") else None
    narrators = book_meta.get("narrators", [])

    # 9. Split into per-chapter MP3s
    print()
    chapter_files = split_chapters(merged_file, chapter_list, output_dir)

    # 10. Enforce Yoto playlist limits (100 tracks, 100 MB/track, 500 MB total)
    print("\nChecking Yoto playlist limits...")
    playlists = enforce_playlist_limits(
        chapter_files, chapter_list, book_title, output_dir,
    )
    multi = len(playlists) > 1

    # ---- per-playlist processing ----
    for pi, playlist in enumerate(playlists):
        pl_title = playlist["title"]
        pl_files = playlist["files"]
        pl_chapters = playlist["chapters"]

        # Set up directory (sub-folder per part when multiple playlists)
        if multi:
            pl_dir = output_dir / _safe_dirname(f"Pt. {pi + 1}")
            pl_dir.mkdir(parents=True, exist_ok=True)
            moved = []
            for f in pl_files:
                dest = pl_dir / f.name
                f.rename(dest)
                moved.append(dest)
            pl_files = moved
            playlist["files"] = pl_files
        else:
            pl_dir = output_dir

        # 11. Write ID3 tags
        print(f"\nWriting ID3 tags{f' ({pl_title})' if multi else ''}...")
        write_id3_tags(pl_files, pl_chapters, pl_title, authors, cover_path)

        # 12. Normalize volume (opt-in)
        if normalize:
            print()
            normalize_volume(pl_dir)

        # 13. Rename files
        print(f"\nRenaming files{f' ({pl_title})' if multi else ''}...")
        final_files = rename_chapters(pl_files, pl_chapters, pl_title)
        for f in final_files:
            print(f"  {f.name}")
        playlist["final_files"] = final_files

    if not normalize:
        print("\nSkipping volume normalization (use --normalize to enable).")

    # 14. Upload to Yoto
    if not no_upload:
        for playlist in playlists:
            pl_title = playlist["title"]
            pl_chapters = playlist["chapters"]
            final_files = playlist["final_files"]
            chapter_titles = [
                ch.get("title", f"Chapter {i+1}")
                for i, ch in enumerate(pl_chapters)
            ]

            # Generate chapter icons (opt-in)
            icon_paths = None
            if icons:
                if len(chapter_titles) > 20:
                    ok = input(
                        f"\nGenerate icons for {len(chapter_titles)} chapters of "
                        f"{pl_title}? (~${len(chapter_titles) * 0.023:.2f}) [y/N]: "
                    ).strip().lower()
                    if ok != "y":
                        print("Skipping icon generation.")
                        icon_paths = None
                    else:
                        book_description = book_meta.get("description", "")
                        icon_paths = generate_chapter_icons(
                            chapter_titles, pl_title, book_description,
                            final_files[0].parent,
                        )
                else:
                    book_description = book_meta.get("description", "")
                    icon_paths = generate_chapter_icons(
                        chapter_titles, pl_title, book_description,
                        final_files[0].parent,
                    )

            upload_to_yoto(
                final_files, chapter_titles, pl_title, authors,
                narrators=narrators, cover_path=cover_path, icon_paths=icon_paths,
            )
    else:
        print("\nSkipping Yoto upload (--no-upload).")

    # 15. Clean up
    if not keep_intermediate:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    # Summary
    total_chapters = sum(len(p["final_files"]) for p in playlists)
    if multi:
        print(
            f"\nDone! {total_chapters} chapters across {len(playlists)} "
            f"playlists saved to:\n  {output_dir}"
        )
    else:
        print(f"\nDone! {total_chapters} chapters saved to:\n  {output_dir}")


def _safe_dirname(name: str) -> str:
    """Create a safe directory name from a book title."""
    import re

    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(". ") or "Untitled"


if __name__ == "__main__":
    main()
