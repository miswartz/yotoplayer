"""Fetch official Yoto card cover art from us.yotoplay.com.

For each book in the library that doesn't already have a cover in _cards/,
searches the Yoto store, extracts the card cover image URL, and downloads it.
"""

import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import requests

YOTO_BASE = "https://us.yotoplay.com"
YOTO_CDN_PATTERN = re.compile(
    r"card-content\.yotoplay\.com/yoto/pub/([A-Za-z0-9_\-]+)"
)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })
    return _session


def _slugify(name: str) -> str:
    """Convert a book title to a URL slug like the Yoto store uses."""
    slug = name.lower().strip()
    slug = re.sub(r"[''']", "", slug)           # remove apostrophes
    slug = re.sub(r"[^a-z0-9]+", "-", slug)     # non-alphanum → hyphens
    slug = slug.strip("-")
    return slug


def _fetch_page(url: str) -> Optional[str]:
    """Fetch a page, return HTML text or None on error."""
    session = _get_session()
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def _extract_cover_hash(html: str) -> Optional[str]:
    """Extract the first card-content CDN hash from page HTML."""
    match = YOTO_CDN_PATTERN.search(html)
    if match:
        return match.group(1)
    return None


def _extract_product_links(html: str) -> list[str]:
    """Extract product slugs from a search results page."""
    # Pattern: href="/products/some-slug"
    matches = re.findall(r'href="/products/([a-z0-9][a-z0-9\-]*)"', html)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for slug in matches:
        if slug not in seen:
            seen.add(slug)
            result.append(slug)
    return result


def _find_best_product(book_name: str, product_slugs: list[str]) -> Optional[str]:
    """Pick the product slug that best matches the book name."""
    target = _slugify(book_name)
    # Exact match first
    if target in product_slugs:
        return target
    # Slug starts with target (handles "matilda-us", "the-bfg-us", etc.)
    for slug in product_slugs:
        if slug.startswith(target):
            return slug
    # Target starts with slug
    for slug in product_slugs:
        if target.startswith(slug):
            return slug
    # Slug contains target
    for slug in product_slugs:
        if target in slug:
            return slug
    # Target words all appear in slug
    target_words = set(target.split("-"))
    for slug in product_slugs:
        slug_words = set(slug.split("-"))
        if target_words.issubset(slug_words):
            return slug
    return None


def _download_cover(cdn_hash: str, dest: Path) -> bool:
    """Download a cover image from the Yoto CDN.

    Tries progressively smaller sizes since the CDN can 502 on large requests.
    """
    base = f"https://card-content.yotoplay.com/yoto/pub/{cdn_hash}"
    urls = [
        f"{base}?type=cover&width=1000&quality=90",
        f"{base}?type=cover&width=500&quality=70",
        f"{base}?type=cover&width=300&quality=70",
        base,
    ]
    session = _get_session()
    for url in urls:
        try:
            resp = session.get(url, timeout=120)
            if resp.status_code != 200 or len(resp.content) < 1000:
                continue
            # Detect format from magic bytes
            if resp.content[:4] == b"\x89PNG":
                ext = ".png"
            elif resp.content[:2] == b"\xff\xd8":
                ext = ".jpg"
            else:
                ext = ".png"  # default
            out = dest.with_suffix(ext)
            out.write_bytes(resp.content)
            return True
        except requests.RequestException:
            continue
    return False


def fetch_cover_for_book(book_name: str, cards_dir: Path) -> bool:
    """Try to find and download the official Yoto cover for a book.

    Returns True if a cover was downloaded.
    """
    # Already have a cover?
    for ext in IMAGE_EXTS:
        if (cards_dir / f"{book_name}{ext}").exists():
            return False

    slug = _slugify(book_name)

    # 1. Try direct product URL
    print(f"  Searching: {book_name}...", end="", flush=True)
    html = _fetch_page(f"{YOTO_BASE}/products/{slug}")
    if html:
        cdn_hash = _extract_cover_hash(html)
        if cdn_hash:
            print(" found!", flush=True)
            return _download_cover(cdn_hash, cards_dir / book_name)

    # 2. Try common slug variants
    variants = [
        f"{slug}-us",
        f"{slug}-new-edition",
        f"the-{slug}" if not slug.startswith("the-") else slug[4:],
    ]
    for variant in variants:
        html = _fetch_page(f"{YOTO_BASE}/products/{variant}")
        if html:
            cdn_hash = _extract_cover_hash(html)
            if cdn_hash:
                print(f" found! (as {variant})", flush=True)
                return _download_cover(cdn_hash, cards_dir / book_name)

    # 3. Search the store
    search_url = f"{YOTO_BASE}/collections/library?q={quote(book_name)}"
    html = _fetch_page(search_url)
    if html:
        product_slugs = _extract_product_links(html)
        # Filter out generic pages
        product_slugs = [
            s for s in product_slugs
            if s not in ("gift-certificate",)
            and not s.startswith("collections/")
        ]
        best = _find_best_product(book_name, product_slugs)
        if best:
            product_html = _fetch_page(f"{YOTO_BASE}/products/{best}")
            if product_html:
                cdn_hash = _extract_cover_hash(product_html)
                if cdn_hash:
                    print(f" found! (via search: {best})", flush=True)
                    return _download_cover(cdn_hash, cards_dir / book_name)

    print(" not found on Yoto store.", flush=True)
    return False


def fetch_all_covers(library_dir: Path) -> int:
    """Scan library and fetch missing Yoto covers.

    Returns the number of new covers downloaded.
    """
    _SKIP_DIRS = {".work", "_cards"}
    cards_dir = library_dir / "_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    book_dirs = sorted(
        d for d in library_dir.iterdir()
        if d.is_dir() and d.name not in _SKIP_DIRS
    )

    if not book_dirs:
        print("No book directories found.", file=sys.stderr)
        return 0

    print(f"Checking {len(book_dirs)} books against Yoto store...\n")

    downloaded = 0
    for book_dir in book_dirs:
        # Skip if already have a card image
        has_card = any(
            (cards_dir / f"{book_dir.name}{ext}").exists()
            for ext in IMAGE_EXTS
        )
        if has_card:
            print(f"  SKIP {book_dir.name} (already in _cards)")
            continue

        if fetch_cover_for_book(book_dir.name, cards_dir):
            downloaded += 1
            time.sleep(1)  # be polite to the Yoto server

    print(f"\nDownloaded {downloaded} new cover(s) to {cards_dir}")
    return downloaded
