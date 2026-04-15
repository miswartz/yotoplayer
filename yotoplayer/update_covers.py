"""Update cover art on existing Yoto playlists from local images.

Matches local book directories and _cards/ images to Yoto playlists
by title, uploads the cover image, and updates the card metadata.
"""

import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mutagen.id3 import ID3, ID3NoHeaderError
from PIL import Image

from .covers import _stamp_part_label
from .yoto import get_yoto_session, upload_cover, API_URL
from .collection import is_collection_dir

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _list_yoto_cards(session) -> List[Dict]:
    """Fetch all user-created cards from Yoto."""
    resp = session.get(f"{API_URL}/content/mine", timeout=30)
    resp.raise_for_status()
    return resp.json().get("cards", [])


def _find_local_cover(book_name: str, library_dir: Path) -> Optional[Path]:
    """Find the best local cover image for a book.

    Priority:
      1. Exact match in _cards/
      2. Base title match in _cards/ (e.g. "Title Pt. 1" matches "Title")
      3. .work/cover.jpg in the book directory
      4. First MP3 with embedded cover art
    """
    cards_dir = library_dir / "_cards"

    # Exact match
    if cards_dir.is_dir():
        for ext in _IMAGE_EXTS:
            candidate = cards_dir / f"{book_name}{ext}"
            if candidate.exists():
                return candidate

    # Strip "Pt. N" suffix for multi-part books
    base_name = re.sub(r"\s+Pt\.\s*\d+$", "", book_name)
    if base_name != book_name and cards_dir.is_dir():
        for ext in _IMAGE_EXTS:
            candidate = cards_dir / f"{base_name}{ext}"
            if candidate.exists():
                return candidate

    # Try book directory
    book_dir = library_dir / book_name
    if not book_dir.is_dir() and base_name != book_name:
        book_dir = library_dir / base_name

    # Search inside collection directories for the book
    if not book_dir.is_dir():
        _skip = {".work", "_cards"}
        for top_dir in sorted(library_dir.iterdir()):
            if top_dir.is_dir() and top_dir.name not in _skip and is_collection_dir(top_dir):
                candidate = top_dir / book_name
                if candidate.is_dir():
                    book_dir = candidate
                    break
                if base_name != book_name:
                    candidate = top_dir / base_name
                    if candidate.is_dir():
                        book_dir = candidate
                        break

    if book_dir.is_dir():
        # .work/cover.jpg
        work_cover = book_dir / ".work" / "cover.jpg"
        if work_cover.exists():
            return work_cover

        # First MP3 with APIC tag
        mp3 = _first_mp3_with_cover(book_dir)
        if mp3:
            return mp3

    return None


def _first_mp3_with_cover(book_dir: Path) -> Optional[Path]:
    """Find the first MP3 with an embedded cover image."""
    for mp3 in sorted(book_dir.glob("*.mp3")):
        try:
            tags = ID3(str(mp3))
            for key in tags:
                if key.startswith("APIC") and tags[key].data:
                    return mp3
        except (ID3NoHeaderError, Exception):
            continue
    # Check sub-dirs (multi-part)
    for sub in sorted(book_dir.iterdir()):
        if sub.is_dir() and sub.name not in (".work", "icons"):
            for mp3 in sorted(sub.glob("*.mp3")):
                try:
                    tags = ID3(str(mp3))
                    for key in tags:
                        if key.startswith("APIC") and tags[key].data:
                            return mp3
                except (ID3NoHeaderError, Exception):
                    continue
    return None


def _upload_cover_from_path(
    session, cover_path: Path, part_name: Optional[str] = None,
) -> Optional[str]:
    """Upload a cover image to Yoto, extracting from MP3 if needed.

    If part_name is given (e.g. 'Pt. 1'), stamps it on the image first.
    """
    import tempfile

    if cover_path.suffix.lower() == ".mp3":
        # Extract APIC data
        try:
            tags = ID3(str(cover_path))
            for key in tags:
                if key.startswith("APIC") and tags[key].data:
                    img = Image.open(BytesIO(tags[key].data))
                    if part_name:
                        img = _stamp_part_label(img, part_name)
                    tmp = Path(tempfile.mktemp(suffix=".png"))
                    img.save(str(tmp), "PNG")
                    try:
                        return upload_cover(session, tmp)
                    finally:
                        tmp.unlink(missing_ok=True)
        except Exception:
            return None
        return None
    else:
        if part_name:
            img = Image.open(cover_path)
            img = _stamp_part_label(img, part_name)
            tmp = Path(tempfile.mktemp(suffix=".png"))
            img.save(str(tmp), "PNG")
            try:
                return upload_cover(session, tmp)
            finally:
                tmp.unlink(missing_ok=True)
        return upload_cover(session, cover_path)


def _update_card_cover(
    session, card_id: str, cover_url: str, card_data: Dict,
) -> bool:
    """Update a Yoto card's cover image by re-posting with new metadata."""
    payload = {
        "cardId": card_id,
        "title": card_data["title"],
        "content": card_data["content"],
        "metadata": card_data["metadata"],
    }
    payload["metadata"]["cover"] = {"imageL": cover_url}

    resp = session.post(f"{API_URL}/content", json=payload, timeout=30)
    return resp.ok


def update_yoto_covers(library_dir: Path) -> int:
    """Match local covers to Yoto playlists and update their cover art.

    Returns the number of covers updated.
    """
    session = get_yoto_session()
    if session is None:
        print(
            "Yoto not authenticated. Run 'yotoplayer yoto-auth' first.",
            file=sys.stderr,
        )
        return 0

    print("Fetching Yoto playlists...")
    cards = _list_yoto_cards(session)
    if not cards:
        print("No playlists found on your Yoto account.")
        return 0

    print(f"Found {len(cards)} playlist(s). Matching to local covers...\n")

    updated = 0
    for card in cards:
        card_id = card.get("cardId", "")
        title = card.get("title", "")
        if not card_id or not title:
            continue

        cover_path = _find_local_cover(title, library_dir)
        if not cover_path:
            print(f"  SKIP {title} (no local cover)")
            continue

        print(f"  Updating {title}...", end=" ", flush=True)

        # Detect part suffix for multi-part books
        part_match = re.search(r"(Pt\.\s*\d+)$", title)
        part_name = part_match.group(1) if part_match else None

        # Upload the cover image
        cover_url = _upload_cover_from_path(session, cover_path, part_name)
        if not cover_url:
            print("upload failed")
            continue

        # Fetch full card data for the update payload
        resp = session.get(f"{API_URL}/content/{card_id}", timeout=15)
        if not resp.ok:
            print("could not fetch card data")
            continue
        card_data = resp.json().get("card", {})

        # Update the card
        if _update_card_cover(session, card_id, cover_url, card_data):
            print("done")
            updated += 1
        else:
            print("update failed")

    print(f"\nUpdated {updated} cover(s).")
    return updated
