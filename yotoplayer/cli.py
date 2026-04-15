import json
import sys
from pathlib import Path

import click

from .auth import get_client, setup_auth, print_libraries, get_library_keys
from .search import search_audiobooks, display_results
from .download import ensure_borrowed, download_audiobook, return_loan
from .process import (
    merge_parts,
    split_chapters,
    write_id3_tags,
    normalize_volume,
)
from .rename import rename_chapters, _is_meaningful_title
from .yoto import device_code_auth, is_yoto_authenticated, upload_to_yoto
from .icons import generate_chapter_icons
from .playlist import enforce_playlist_limits
from .covers import generate_cover_sheets
from .fetch_covers import fetch_all_covers, fetch_cover_for_book
from .update_covers import update_yoto_covers
from .preflight import check_pipeline_ready, is_setup_complete, mark_setup_complete
from .collection import split_collection, find_book_cover


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


@main.command()
def setup():
    """One-time setup: install dependencies, browsers, and authenticate."""
    import shutil
    import subprocess

    settings_dir = Path.home() / ".yotoplayer"
    settings_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  YotoPlayer Setup")
    print("=" * 60)

    # -- 1. ffmpeg ---------------------------------------------------------
    print("\n[1/4] Checking ffmpeg...")
    if shutil.which("ffmpeg"):
        print("  ffmpeg found.")
    else:
        print("  ffmpeg not found.")
        if sys.platform == "win32":
            ok = input("  Install ffmpeg via winget? [Y/n]: ").strip().lower()
            if ok != "n":
                subprocess.run(["winget", "install", "ffmpeg"], check=False)
                if shutil.which("ffmpeg"):
                    print("  ffmpeg installed.")
                else:
                    print("  ffmpeg installed but not in PATH yet.")
                    print("  Restart your terminal after setup completes.")
            else:
                print("  Skipped. Install manually: winget install ffmpeg")
        elif sys.platform == "darwin":
            print("  Install with: brew install ffmpeg")
        else:
            print("  Install with: sudo apt install ffmpeg  (or your package manager)")

    # -- 2. Playwright Chromium --------------------------------------------
    print("\n[2/4] Installing Playwright Chromium browser...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )
    if result.returncode == 0:
        print("  Chromium browser ready.")
    else:
        print("  Warning: Playwright browser install failed.")
        print("  Try manually: python -m playwright install chromium")

    # -- 3. Libby auth -----------------------------------------------------
    print("\n[3/4] Libby authentication...")
    edge_token = settings_dir / "edge_identity.txt"
    if edge_token.exists():
        print("  Edge identity token found.")
    else:
        print("  Edge identity token needed for audiobook downloads.")
        print("  To get it:")
        print("    1. Open Edge → libbyapp.com → open any audiobook")
        print("    2. DevTools (F12) → Application → Local Storage → libbyapp.com")
        print('    3. Copy the "dewey:sentry.identity" value (starts with eyJ…)')
        token = input("  Paste your token here (or press Enter to skip): ").strip()
        if token:
            edge_token.write_text(token, encoding="utf-8")
            print("  Token saved.")
        else:
            print(f"  Skipped. Save it later to: {edge_token}")

    chip_path = settings_dir / "chip.json"
    if chip_path.exists():
        print("  Libby account already linked.")
    else:
        ok = input("  Link your Libby account now? [Y/n]: ").strip().lower()
        if ok != "n":
            setup_auth()
        else:
            print("  Skipped. Run 'yotoplayer auth' later.")

    # -- 4. Yoto auth ------------------------------------------------------
    print("\n[4/4] Yoto authentication...")
    if is_yoto_authenticated():
        print("  Yoto account already linked.")
    else:
        ok = input("  Link your Yoto account now? [Y/n]: ").strip().lower()
        if ok != "n":
            device_code_auth()
            print("  Yoto authentication complete.")
        else:
            print("  Skipped. Run 'yotoplayer yoto-auth' later.")

    # -- Done --------------------------------------------------------------
    mark_setup_complete()
    print("\n" + "=" * 60)
    print("  Setup complete!")
    print("  Run 'yotoplayer get <query>' to download your first audiobook.")
    print("=" * 60)


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
@click.option(
    "--collection",
    is_flag=True,
    default=False,
    help="Treat audiobook as a collection of smaller books, splitting by chapter titles.",
)
def get_audiobook(query, output, keep_intermediate, normalize, no_upload, icons, collection):
    """Search for an audiobook and download + process it.

    QUERY is the search term (e.g., "dinosaurs before dark").
    """
    # 0. Pre-flight check
    check_pipeline_ready(upload=not no_upload, icons=icons)

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

    # Return the book on Libby now that all parts are downloaded
    return_loan(client, loan)

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

    # ---- Collection mode: split into individual books ----
    if collection:
        _process_collection(
            chapter_files, chapter_list, book_meta, output_dir, work_dir,
            authors, narrators, cover_path, normalize, no_upload, icons,
            keep_intermediate,
        )
        return

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
            has_meaningful = any(
                _is_meaningful_title(t, book_title) for t in chapter_titles
            )
            if icons and not has_meaningful:
                print("Skipping icon generation (no meaningful chapter names).")
            elif icons:
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

    # 16. Regenerate cover sheets
    library_dir = output_dir.parent
    covers_pdf = library_dir / "covers.pdf"
    print("\nRegenerating cover sheets...")
    generate_cover_sheets(library_dir, covers_pdf, mode="fit")

    # Summary
    total_chapters = sum(len(p["final_files"]) for p in playlists)
    if multi:
        print(
            f"\nDone! {total_chapters} chapters across {len(playlists)} "
            f"playlists saved to:\n  {output_dir}"
        )
    else:
        print(f"\nDone! {total_chapters} chapters saved to:\n  {output_dir}")


def _process_collection(
    chapter_files, chapter_list, book_meta, output_dir, work_dir,
    authors, narrators, cover_path, normalize, no_upload, icons,
    keep_intermediate,
):
    """Process a collection audiobook: split into individual books and handle each."""
    import shutil

    collection_title = book_meta["title"]
    book_description = book_meta.get("description", "")

    # Split into individual books
    books = split_collection(chapter_list, chapter_files)

    # The collection root is output_dir (named after the collection title).
    # Each book gets its own sub-folder under the collection root.
    # The library root is one level up from the collection folder.
    library_dir = output_dir.parent
    cards_dir = library_dir / "_cards"

    # Fetch covers from Yoto store for each book in the collection
    cards_dir.mkdir(parents=True, exist_ok=True)
    print("\nSearching for individual book covers on Yoto store...")
    for book in books:
        fetch_cover_for_book(book["title"], cards_dir)

    total_chapters = 0

    for bi, book in enumerate(books):
        bk_title = book["title"]
        bk_chapters = book["chapters"]
        bk_files = book["files"]

        print(f"\n{'=' * 60}")
        print(f"  Book {bi + 1}/{len(books)}: {bk_title}")
        print(f"{'=' * 60}")

        # Create sub-folder for this book under the collection folder
        book_dir = output_dir / _safe_dirname(bk_title)
        book_dir.mkdir(parents=True, exist_ok=True)

        # Move chapter files into book directory
        moved_files = []
        for f in bk_files:
            dest = book_dir / f.name
            f.rename(dest)
            moved_files.append(dest)
        bk_files = moved_files

        # Find cover: prefer _cards/ match, fall back to collection cover
        bk_cover = find_book_cover(bk_title, cards_dir, cover_path)

        # Enforce Yoto playlist limits per-book
        print("\nChecking Yoto playlist limits...")
        playlists = enforce_playlist_limits(
            bk_files, bk_chapters, bk_title, book_dir,
        )
        multi = len(playlists) > 1

        for pi, playlist in enumerate(playlists):
            pl_title = playlist["title"]
            pl_files = playlist["files"]
            pl_chapters = playlist["chapters"]

            if multi:
                pl_dir = book_dir / _safe_dirname(f"Pt. {pi + 1}")
                pl_dir.mkdir(parents=True, exist_ok=True)
                moved = []
                for f in pl_files:
                    dest = pl_dir / f.name
                    f.rename(dest)
                    moved.append(dest)
                pl_files = moved
                playlist["files"] = pl_files
            else:
                pl_dir = book_dir

            # Write ID3 tags
            print(f"\nWriting ID3 tags{f' ({pl_title})' if multi else ''}...")
            write_id3_tags(pl_files, pl_chapters, pl_title, authors, bk_cover)

            # Normalize volume (opt-in)
            if normalize:
                print()
                normalize_volume(pl_dir)

            # Rename files
            print(f"\nRenaming files{f' ({pl_title})' if multi else ''}...")
            final_files = rename_chapters(pl_files, pl_chapters, pl_title)
            for f in final_files:
                print(f"  {f.name}")
            playlist["final_files"] = final_files

        if not normalize:
            print("\nSkipping volume normalization (use --normalize to enable).")

        # Upload each book as its own Yoto playlist
        if not no_upload:
            for playlist in playlists:
                pl_title = playlist["title"]
                pl_chapters = playlist["chapters"]
                final_files = playlist["final_files"]
                chapter_titles = [
                    ch.get("title", f"Chapter {i+1}")
                    for i, ch in enumerate(pl_chapters)
                ]

                icon_paths = None
                has_meaningful = any(
                    _is_meaningful_title(t, bk_title) for t in chapter_titles
                )
                if icons and not has_meaningful:
                    print("Skipping icon generation (no meaningful chapter names).")
                elif icons:
                    if len(chapter_titles) > 20:
                        ok = input(
                            f"\nGenerate icons for {len(chapter_titles)} chapters of "
                            f"{pl_title}? (~${len(chapter_titles) * 0.023:.2f}) [y/N]: "
                        ).strip().lower()
                        if ok != "y":
                            print("Skipping icon generation.")
                            icon_paths = None
                        else:
                            icon_paths = generate_chapter_icons(
                                chapter_titles, pl_title, book_description,
                                final_files[0].parent,
                            )
                    else:
                        icon_paths = generate_chapter_icons(
                            chapter_titles, pl_title, book_description,
                            final_files[0].parent,
                        )

                upload_to_yoto(
                    final_files, chapter_titles, pl_title, authors,
                    narrators=narrators, cover_path=bk_cover, icon_paths=icon_paths,
                )
        else:
            print("\nSkipping Yoto upload (--no-upload).")

        total_chapters += sum(len(p["final_files"]) for p in playlists)

    # Clean up
    if not keep_intermediate:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Regenerate cover sheets
    covers_pdf = library_dir / "covers.pdf"
    print("\nRegenerating cover sheets...")
    generate_cover_sheets(library_dir, covers_pdf, mode="fit")

    # Summary
    print(
        f"\nDone! Collection \"{collection_title}\" — {len(books)} books, "
        f"{total_chapters} total chapters saved to:\n  {output_dir}"
    )


def _safe_dirname(name: str) -> str:
    """Create a safe directory name from a book title."""
    import re

    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip(". ") or "Untitled"


@main.command(name="print-covers")
@click.option(
    "--library",
    "-l",
    type=click.Path(),
    default=None,
    help="Library directory (default: ~/YotoPlayer)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Output PDF path (default: ~/YotoPlayer/covers.pdf)",
)
@click.option(
    "--fit/--crop",
    default=True,
    help="Fit full cover with blurred background (default) or centre-crop.",
)
@click.option(
    "--ai-covers",
    is_flag=True,
    default=False,
    help="AI-generated covers via GPT-4o Vision + DALL-E 3 (~$0.08/cover).",
)
@click.option(
    "--outpaint",
    is_flag=True,
    default=False,
    help="AI extends original cover art to fill card (Stability AI, ~$0.04/cover).",
)
def print_covers(library, output, fit, ai_covers, outpaint):
    """Generate printable NFC card cover sheets from local books."""
    library_dir = Path(library) if library else Path.home() / "YotoPlayer"
    output_path = Path(output) if output else library_dir / "covers.pdf"

    if ai_covers:
        mode = "ai"
    elif outpaint:
        mode = "outpaint"
    elif fit:
        mode = "fit"
    else:
        mode = "crop"  # --crop flag

    generate_cover_sheets(library_dir, output_path, mode=mode)


@main.command(name="fetch-covers")
@click.option(
    "--library",
    "-l",
    type=click.Path(),
    default=None,
    help="Library directory (default: ~/YotoPlayer)",
)
def fetch_covers(library):
    """Fetch official Yoto card art from us.yotoplay.com for local books."""
    library_dir = Path(library) if library else Path.home() / "YotoPlayer"
    if not library_dir.exists():
        print(f"Error: Library directory not found: {library_dir}", file=sys.stderr)
        sys.exit(1)
    fetch_all_covers(library_dir)


@main.command(name="update-covers")
@click.option(
    "--library",
    "-l",
    type=click.Path(),
    default=None,
    help="Library directory (default: ~/YotoPlayer)",
)
def update_covers(library):
    """Update cover art on existing Yoto playlists from local images."""
    library_dir = Path(library) if library else Path.home() / "YotoPlayer"
    if not library_dir.exists():
        print(f"Error: Library directory not found: {library_dir}", file=sys.stderr)
        sys.exit(1)
    update_yoto_covers(library_dir)


if __name__ == "__main__":
    main()
