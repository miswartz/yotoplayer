"""Generate printable cover sheets for Yoto NFC cards.

Scans locally downloaded books, extracts cover art from MP3 ID3 tags,
crops/resizes to NFC card dimensions with bleed, and tiles them onto
8.5 x 11 inch sheets ready for printing on adhesive paper.
"""

import base64
import math
import sys
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple

from mutagen.id3 import ID3, ID3NoHeaderError
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from .collection import is_collection_dir, get_collection_book_dirs

# ---------------------------------------------------------------------------
# Dimensions (millimetres)
# ---------------------------------------------------------------------------
# Standard NFC / credit card (ISO 7810 ID-1) in portrait orientation
CARD_W_MM = 53.98
CARD_H_MM = 85.60

BLEED_MM = 1.0  # overlap on each edge for trimming tolerance
BORDER_MM = 1.0  # white border around each card (cutting guide)

CARD_BLEED_W_MM = CARD_W_MM + 2 * BLEED_MM   # 55.98
CARD_BLEED_H_MM = CARD_H_MM + 2 * BLEED_MM   # 87.60

# US Letter
SHEET_W_MM = 215.9
SHEET_H_MM = 279.4

# Print resolution
DPI = 300
MM_PER_INCH = 25.4


def _mm_to_px(mm: float) -> int:
    return round(mm / MM_PER_INCH * DPI)


# Pre-computed pixel sizes
CARD_PX_W = _mm_to_px(CARD_BLEED_W_MM)
CARD_PX_H = _mm_to_px(CARD_BLEED_H_MM)
BORDER_PX = _mm_to_px(BORDER_MM)
CELL_PX_W = CARD_PX_W + 2 * BORDER_PX  # card + white border
CELL_PX_H = CARD_PX_H + 2 * BORDER_PX
SHEET_PX_W = _mm_to_px(SHEET_W_MM)
SHEET_PX_H = _mm_to_px(SHEET_H_MM)


# ---------------------------------------------------------------------------
# Cover extraction
# ---------------------------------------------------------------------------

def _extract_cover_from_mp3(mp3_path: Path) -> Optional[Image.Image]:
    """Extract the front-cover APIC frame from an MP3 and return as PIL Image."""
    try:
        tags = ID3(str(mp3_path))
    except (ID3NoHeaderError, Exception):
        return None

    for key in tags:
        if key.startswith("APIC"):
            apic = tags[key]
            if apic.data:
                return Image.open(BytesIO(apic.data))
    return None


def _find_cover_for_book(book_dir: Path) -> Optional[Image.Image]:
    """Try to get a cover for a book directory.

    Priority:
      1. .work/cover.jpg (if --keep-intermediate was used)
      2. APIC tag from the first MP3 in the directory
      3. APIC tag from first MP3 in sub-directories (multi-playlist)
    """
    # Try .work/cover.jpg first
    work_cover = book_dir / ".work" / "cover.jpg"
    if work_cover.exists():
        try:
            return Image.open(work_cover)
        except Exception:
            pass

    # Fall back to extracting from first MP3
    mp3s = sorted(book_dir.glob("*.mp3"))
    for mp3 in mp3s:
        img = _extract_cover_from_mp3(mp3)
        if img:
            return img

    # Check sub-dirs (multi-playlist books have Pt. N/ folders)
    for sub in sorted(book_dir.iterdir()):
        if sub.is_dir() and sub.name not in (".work", "icons"):
            for mp3 in sorted(sub.glob("*.mp3")):
                img = _extract_cover_from_mp3(mp3)
                if img:
                    return img

    return None


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

def _crop_cover_to_card(img: Image.Image) -> Image.Image:
    """Resize and crop a cover image to card dimensions (with bleed).

    Uses a centre-crop strategy: scale the shortest dimension to fill
    the card, then crop the centre of the longer dimension.
    """
    target_w, target_h = CARD_PX_W, CARD_PX_H
    src_w, src_h = img.size

    # Scale so the image fully covers the target rectangle
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)

    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Centre-crop
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))

    return img.convert("RGB")


def _fit_cover_to_card(img: Image.Image) -> Image.Image:
    """Fit the full cover inside card dimensions with a blurred background.

    If the aspect ratio is close to the card ratio (within 10%), simply
    stretch to fill.  Otherwise the entire cover is visible and remaining
    space is filled with a mirror-padded, blurred, and darkened version.
    """
    target_w, target_h = CARD_PX_W, CARD_PX_H
    src_w, src_h = img.size

    # If aspect ratio is close enough, just stretch to fill
    src_ratio = src_w / src_h
    card_ratio = target_w / target_h
    if abs(src_ratio - card_ratio) / card_ratio < 0.10:
        return img.resize((target_w, target_h), Image.LANCZOS).convert("RGB")

    # Foreground: scale to fit entirely inside card
    fg_scale = min(target_w / src_w, target_h / src_h)
    fg_w = round(src_w * fg_scale)
    fg_h = round(src_h * fg_scale)
    fg = img.resize((fg_w, fg_h), Image.LANCZOS)

    # Background: mirror-pad the foreground to fill the card, then blur
    pad_left = (target_w - fg_w) // 2
    pad_right = target_w - fg_w - pad_left
    pad_top = (target_h - fg_h) // 2
    pad_bottom = target_h - fg_h - pad_top

    bg = Image.new("RGB", (target_w, target_h))
    bg.paste(fg, (pad_left, pad_top))

    # Mirror-fill horizontal bands
    if pad_top > 0:
        # Top strip: flip the top slice of fg
        top_strip = fg.crop((0, 0, fg_w, min(pad_top, fg_h)))
        top_strip = top_strip.transpose(Image.FLIP_TOP_BOTTOM)
        bg.paste(top_strip, (pad_left, pad_top - top_strip.size[1]))
    if pad_bottom > 0:
        bottom_strip = fg.crop((0, max(0, fg_h - pad_bottom), fg_w, fg_h))
        bottom_strip = bottom_strip.transpose(Image.FLIP_TOP_BOTTOM)
        bg.paste(bottom_strip, (pad_left, pad_top + fg_h))

    # Mirror-fill vertical bands (including corners)
    if pad_left > 0:
        left_strip = bg.crop((pad_left, 0, pad_left + min(pad_left, fg_w), target_h))
        left_strip = left_strip.transpose(Image.FLIP_LEFT_RIGHT)
        bg.paste(left_strip, (pad_left - left_strip.size[0], 0))
    if pad_right > 0:
        right_edge = pad_left + fg_w
        right_strip = bg.crop((max(pad_left, right_edge - pad_right), 0, right_edge, target_h))
        right_strip = right_strip.transpose(Image.FLIP_LEFT_RIGHT)
        bg.paste(right_strip, (right_edge, 0))

    bg = bg.filter(ImageFilter.GaussianBlur(radius=20))
    bg = ImageEnhance.Brightness(bg).enhance(0.8)

    # Paste sharp foreground on top
    card = bg.convert("RGB")
    card.paste(fg, (pad_left, pad_top))

    return card


# ---------------------------------------------------------------------------
# AI cover re-creation
# ---------------------------------------------------------------------------

def _get_openai_client():
    """Return an OpenAI client if configured, else None."""
    from yotoplayer import config

    api_key = config.get("openai_api_key", "OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        return OpenAI(api_key=api_key)
    except ImportError:
        print(
            "Warning: openai package not installed. Run: pip install openai",
            file=sys.stderr,
        )
        return None


def _get_stability_api_key() -> Optional[str]:
    """Return the Stability AI API key if configured, else None."""
    from yotoplayer import config

    return config.get("stability_api_key", "STABILITY_API_KEY")


def _describe_cover(client, cover_image: Image.Image) -> Optional[str]:
    """Use GPT-4o vision to describe a book cover's art style."""
    buf = BytesIO()
    cover_image.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Describe this book cover's art style, color palette, "
                                "key visual elements, and composition in 2-3 sentences. "
                                "Do NOT include any text or title from the cover. "
                                "Focus only on the visual art."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        print(f"  Warning: cover description failed: {exc}", file=sys.stderr)
        return None


def _generate_ai_cover(client, book_title: str, description: str) -> Optional[Image.Image]:
    """Use DALL-E 3 to generate new cover art at card portrait ratio."""
    prompt = (
        f"Book cover illustration for \"{book_title}\". "
        f"Art style reference: {description} "
        f"Portrait orientation, suitable for a small card. "
        f"No text, no letters, no words, no title — pure illustration only."
    )
    try:
        resp = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1792",
            quality="standard",
            n=1,
        )
        import requests

        img_url = resp.data[0].url
        img_data = requests.get(img_url, timeout=60).content
        return Image.open(BytesIO(img_data))
    except Exception as exc:
        print(f"  Warning: AI cover generation failed: {exc}", file=sys.stderr)
        return None


def _load_title_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a good title font, with fallbacks."""
    candidates = [
        "C:/Windows/Fonts/GEORGIAB.TTF",   # Georgia Bold
        "C:/Windows/Fonts/GEORGIA.TTF",     # Georgia
        "C:/Windows/Fonts/segoeuib.ttf",    # Segoe UI Bold
        "C:/Windows/Fonts/arial.ttf",       # Arial
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _render_title_on_cover(card: Image.Image, title: str) -> Image.Image:
    """Render book title at the bottom of a card image with a dark overlay."""
    card = card.copy()
    draw = ImageDraw.Draw(card)
    card_w, card_h = card.size

    padding_x = card_w // 10
    max_text_w = card_w - 2 * padding_x

    # Auto-size font to fit width
    font_size = card_h // 8
    font = _load_title_font(font_size)
    while font_size > 10:
        bbox = draw.textbbox((0, 0), title, font=font)
        text_w = bbox[2] - bbox[0]
        if text_w <= max_text_w:
            break
        font_size -= 2
        font = _load_title_font(font_size)

    bbox = draw.textbbox((0, 0), title, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Dark overlay band at bottom
    band_h = text_h + card_h // 12
    band_top = card_h - band_h
    overlay = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(
        [(0, band_top), (card_w, card_h)],
        fill=(0, 0, 0, 160),
    )
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")

    # Draw title text centred in the band
    draw = ImageDraw.Draw(card)
    text_x = (card_w - text_w) // 2
    text_y = band_top + (band_h - text_h) // 2
    # Shadow
    draw.text((text_x + 1, text_y + 1), title, fill=(0, 0, 0), font=font)
    # Main text
    draw.text((text_x, text_y), title, fill=(255, 255, 255), font=font)

    return card


def _stamp_part_label(card: Image.Image, part_name: str) -> Image.Image:
    """Stamp a part label (e.g. 'Pt. 1') in the bottom-right corner."""
    card = card.copy()
    card_w, card_h = card.size

    font_size = max(16, card_h // 18)
    font = _load_title_font(font_size)

    draw = ImageDraw.Draw(card)
    bbox = draw.textbbox((0, 0), part_name, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    padding = font_size // 2
    # Keep label clear of the bleed/trim zone (1mm ≈ 12px @ 300 DPI) + safe margin
    bleed_px = round(BLEED_MM / 25.4 * DPI)
    margin = bleed_px + font_size // 2

    # Semi-transparent pill background
    x1 = card_w - text_w - padding * 2 - margin
    y1 = card_h - text_h - padding * 2 - margin
    x2 = card_w - margin
    y2 = card_h - margin

    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [(x1, y1), (x2, y2)], radius=font_size // 3, fill=(0, 0, 0, 180),
    )
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")

    # Draw text
    draw = ImageDraw.Draw(card)
    text_x = x1 + padding
    text_y = y1 + padding
    draw.text((text_x + 1, text_y + 1), part_name, fill=(0, 0, 0), font=font)
    draw.text((text_x, text_y), part_name, fill=(255, 255, 255), font=font)

    return card


def _ai_cover_to_card(
    client, cover_image: Image.Image, book_title: str,
) -> Optional[Image.Image]:
    """Generate an AI cover for a book. Returns None on failure."""
    # Step 1: Describe the original cover
    desc = _describe_cover(client, cover_image)
    if not desc:
        return None

    # Step 2: Generate new art
    ai_img = _generate_ai_cover(client, book_title, desc)
    if not ai_img:
        return None

    # Step 3: Crop to exact card dimensions
    card = _crop_cover_to_card(ai_img)

    # Step 4: Render title
    card = _render_title_on_cover(card, book_title)

    return card


# ---------------------------------------------------------------------------
# AI outpaint (extend cover to fill card)
# ---------------------------------------------------------------------------

CACHE_FILENAME_OUTPAINT = "_ai_outpaint.png"
CACHE_FILENAME_AI_COVER = "_ai_cover.png"


def _get_cached_card(book_dir: Path, cache_name: str) -> Optional[Image.Image]:
    """Load a previously cached AI card image if it exists."""
    cached = book_dir / cache_name
    if cached.exists():
        try:
            return Image.open(cached).convert("RGB")
        except Exception:
            pass
    return None


def _save_cached_card(book_dir: Path, cache_name: str, card: Image.Image) -> None:
    """Save an AI card image to the book directory for reuse."""
    cached = book_dir / cache_name
    card.convert("RGB").save(str(cached), "PNG")


def _outpaint_cover_to_card(
    openai_client, cover_image: Image.Image, book_title: str,
) -> Optional[Image.Image]:
    """Use Stability AI outpaint to extend the cover to card dimensions.

    Sends the cover to Stability AI's outpaint endpoint with the number of
    pixels to add top/bottom (or left/right) so the result matches the
    card aspect ratio.  The AI fills only the new strips; original art is
    preserved by the model architecture.

    Falls back to None on failure (caller handles fallback).
    """
    import httpx

    stability_key = _get_stability_api_key()
    if not stability_key:
        print("  Warning: Stability API key not configured.", file=sys.stderr)
        return None

    src_w, src_h = cover_image.size
    cover_ratio = src_w / src_h
    card_ratio = CARD_PX_W / CARD_PX_H  # ~0.631

    # Decide whether we need to extend top/bottom or left/right.
    # Most book covers are taller than cards are wide, so typically we
    # need to extend up+down (cover is wider relative to its height
    # than the card) or left+right (cover is narrower).
    up = down = left = right = 0

    if cover_ratio > card_ratio:
        # Cover is wider than card ratio — need to add height (up+down)
        target_h = round(src_w / card_ratio)
        pad = target_h - src_h
        up = max(0, pad // 2)
        down = max(0, pad - up)
    else:
        # Cover is narrower than card ratio — need to add width (left+right)
        target_w = round(src_h * card_ratio)
        pad = target_w - src_w
        left = max(0, pad // 2)
        right = max(0, pad - left)

    # Clamp each direction to API max of 2000
    up = min(up, 2000)
    down = min(down, 2000)
    left = min(left, 2000)
    right = min(right, 2000)

    if up + down + left + right == 0:
        # Already perfect ratio — just resize
        return cover_image.resize((CARD_PX_W, CARD_PX_H), Image.LANCZOS).convert("RGB")

    # Prepare image bytes (JPEG to keep under 10MiB)
    img_buf = BytesIO()
    cover_image.convert("RGB").save(img_buf, format="PNG")
    img_buf.seek(0)

    # Build optional prompt using GPT-4o vision description
    prompt = ""
    if openai_client:
        desc = _describe_cover(openai_client, cover_image)
        if desc:
            prompt = (
                f"Seamlessly extend this book cover artwork. {desc} "
                f"No text, no letters, no words, only illustration."
            )

    # Build form data
    data: dict = {"output_format": "png", "creativity": "0.3"}
    if up:
        data["up"] = str(up)
    if down:
        data["down"] = str(down)
    if left:
        data["left"] = str(left)
    if right:
        data["right"] = str(right)
    if prompt:
        data["prompt"] = prompt

    try:
        resp = httpx.post(
            "https://api.stability.ai/v2beta/stable-image/edit/outpaint",
            headers={
                "authorization": f"Bearer {stability_key}",
                "accept": "image/*",
            },
            data=data,
            files={"image": ("cover.png", img_buf, "image/png")},
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400] if exc.response else ""
        print(f"  Warning: outpaint failed ({exc.response.status_code}): {body}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  Warning: outpaint failed: {exc}", file=sys.stderr)
        return None

    try:
        result = Image.open(BytesIO(resp.content)).convert("RGB")

        # Paste the original cover back on top of the outpainted result
        # so the AI only contributes the new strips — the original art,
        # title, author text etc. stay completely pixel-perfect.
        # The outpainted result is (src_w + left + right) x (src_h + up + down).
        result.paste(cover_image.convert("RGB"), (left, up))

        result = result.resize((CARD_PX_W, CARD_PX_H), Image.LANCZOS)
        return result.convert("RGB")
    except Exception as exc:
        print(f"  Warning: outpaint decode failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Sheet layout
# ---------------------------------------------------------------------------

def _layout_cards_on_sheets(
    covers: List[Tuple[str, Image.Image]],
) -> List[Image.Image]:
    """Tile card images onto 8.5 x 11 sheets.

    Returns a list of sheet images (one per page).
    """
    cols = SHEET_PX_W // CELL_PX_W
    rows = SHEET_PX_H // CELL_PX_H
    per_page = cols * rows

    # Centre the grid on the sheet
    grid_w = cols * CELL_PX_W
    grid_h = rows * CELL_PX_H
    margin_x = (SHEET_PX_W - grid_w) // 2
    margin_y = (SHEET_PX_H - grid_h) // 2

    sheets: List[Image.Image] = []

    for page_start in range(0, len(covers), per_page):
        page_covers = covers[page_start : page_start + per_page]
        sheet = Image.new("RGB", (SHEET_PX_W, SHEET_PX_H), (255, 255, 255))

        for idx, (_title, card_img) in enumerate(page_covers):
            col = idx % cols
            row = idx // cols
            # Paste card inside the cell, offset by the border
            x = margin_x + col * CELL_PX_W + BORDER_PX
            y = margin_y + row * CELL_PX_H + BORDER_PX
            sheet.paste(card_img, (x, y))

        sheets.append(sheet)

    return sheets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_cover_sheets(
    library_dir: Path,
    output_path: Path,
    mode: str = "crop",
) -> Path:
    """Scan local books, generate printable cover sheets.

    Parameters
    ----------
    library_dir : Path
        Root directory containing book folders (default ~/YotoPlayer).
    output_path : Path
        Where to save the output PDF.
    mode : str
        "crop" (default centre-crop), "fit" (blur background, no clipping),
        "ai" (AI-generated covers with title overlay), or
        "outpaint" (AI extends original cover to fill card).

    Returns
    -------
    Path to the generated PDF.
    """
    if not library_dir.exists():
        print(f"Error: Library directory not found: {library_dir}", file=sys.stderr)
        sys.exit(1)

    # Collect raw cover images, book names, and book directories
    _SKIP_DIRS = {".work", "_cards"}
    raw_covers: List[Tuple[str, Image.Image, Path]] = []
    top_dirs = sorted(
        d for d in library_dir.iterdir()
        if d.is_dir() and d.name not in _SKIP_DIRS
    )
    # Expand collection directories into individual book subdirectories
    book_dirs = []
    for d in top_dirs:
        if is_collection_dir(d):
            book_dirs.extend(get_collection_book_dirs(d))
        else:
            book_dirs.append(d)

    cols = SHEET_PX_W // CELL_PX_W
    rows = SHEET_PX_H // CELL_PX_H

    mode_label = {
        "crop": "centre-crop",
        "fit": "fit + blur",
        "ai": "AI re-creation",
        "outpaint": "AI outpaint",
    }
    print(f"Scanning {library_dir}...")
    print(f"Card size: {CARD_W_MM} x {CARD_H_MM}mm (+{BLEED_MM}mm bleed)")
    print(f"Sheet layout: {cols} across x {rows} down = {cols * rows} per page")
    print(f"Mode: {mode_label.get(mode, mode)}")
    print()

    # Build a lookup of official card images from _cards/ directory
    cards_dir = library_dir / "_cards"
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    cards_lookup: dict[str, Path] = {}
    if cards_dir.is_dir():
        for f in cards_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
                cards_lookup[f.stem] = f

    for book_dir in book_dirs:
        # Detect multi-part books (Pt. 1, Pt. 2, … subdirectories)
        part_dirs = sorted(
            d for d in book_dir.iterdir()
            if d.is_dir() and d.name.startswith("Pt. ")
        )
        is_multi = len(part_dirs) >= 2

        # Check if an official card cover exists in _cards/
        if book_dir.name in cards_lookup:
            try:
                card_img = Image.open(cards_lookup[book_dir.name])
                if is_multi:
                    for pd in part_dirs:
                        label = f"{book_dir.name} ({pd.name})"
                        raw_covers.append((label, card_img, cards_dir, pd.name))
                        print(f"  + {label} (official card)")
                else:
                    raw_covers.append((book_dir.name, card_img, cards_dir, None))
                    print(f"  + {book_dir.name} (official card)")
                continue
            except Exception:
                pass  # fall through to MP3 extraction

        img = _find_cover_for_book(book_dir)
        if img:
            if is_multi:
                for pd in part_dirs:
                    label = f"{book_dir.name} ({pd.name})"
                    raw_covers.append((label, img, book_dir, pd.name))
                    print(f"  + {label}")
            else:
                raw_covers.append((book_dir.name, img, book_dir, None))
                print(f"  + {book_dir.name}")
        else:
            print(f"  - {book_dir.name} (no cover found)")

    # Standalone card images from _cards/ (ones not already matched to a book dir)
    book_dir_names = {d.name for d in book_dirs}
    if cards_dir.is_dir():
        card_images = sorted(
            f for f in cards_dir.iterdir()
            if f.is_file() and f.suffix.lower() in _IMAGE_EXTS
        )
        for card_file in card_images:
            if card_file.stem in book_dir_names:
                continue  # already used as official cover above
            try:
                img = Image.open(card_file)
                title = card_file.stem
                raw_covers.append((title, img, cards_dir, None))
                print(f"  + {title} (standalone card)")
            except Exception:
                print(f"  - {card_file.name} (could not load image)")

    if not raw_covers:
        print("\nNo covers found.", file=sys.stderr)
        sys.exit(1)

    # AI modes: set up client & confirm cost
    openai_client = None
    if mode in ("ai", "outpaint"):
        cache_name = CACHE_FILENAME_AI_COVER if mode == "ai" else CACHE_FILENAME_OUTPAINT
        cached_count = sum(
            1 for _, _, bd, _ in raw_covers if _get_cached_card(bd, cache_name) is not None
        )
        uncached = len(raw_covers) - cached_count

        if mode == "outpaint":
            # Outpaint uses Stability AI; OpenAI is optional (for prompt)
            stability_key = _get_stability_api_key()
            if not stability_key and uncached > 0:
                print(
                    "\nStability API key not configured. Falling back to fit mode.",
                    file=sys.stderr,
                )
                mode = "fit"
            else:
                openai_client = _get_openai_client()  # optional for prompt
                if uncached > 0:
                    cost = uncached * 0.04  # 4 credits @ $0.01/credit
                    cached_msg = f" ({cached_count} cached)" if cached_count else ""
                    ok = input(
                        f"\nOutpaint {uncached} cover(s) via Stability AI?"
                        f"{cached_msg} (~${cost:.2f}) [y/N]: "
                    ).strip().lower()
                    if ok != "y":
                        print("Falling back to fit mode.")
                        mode = "fit"
                else:
                    print(f"  All {cached_count} covers cached — no API calls needed.")
        else:
            # AI re-creation mode uses OpenAI
            openai_client = _get_openai_client()
            if not openai_client and uncached > 0:
                print(
                    "\nOpenAI API key not configured. Falling back to fit mode.",
                    file=sys.stderr,
                )
                mode = "fit"
            elif uncached > 0:
                cost = uncached * 0.08
                cached_msg = f" ({cached_count} cached)" if cached_count else ""
                ok = input(
                    f"\nGenerate AI covers for {uncached} book(s)?{cached_msg} "
                    f"(~${cost:.2f}) [y/N]: "
                ).strip().lower()
                if ok != "y":
                    print("Falling back to fit mode.")
                    mode = "fit"
            else:
                print(f"  All {cached_count} covers cached — no API calls needed.")

    # Process covers
    covers: List[Tuple[str, Image.Image]] = []
    for title, img, book_dir, part_name in raw_covers:
        # Images sourced from _cards/ are already official card art —
        # just crop/resize to exact card dimensions, skip fit/AI operations.
        is_card_image = (book_dir == cards_dir)
        if is_card_image:
            card = _crop_cover_to_card(img)
        elif mode == "ai":
            cached = _get_cached_card(book_dir, CACHE_FILENAME_AI_COVER)
            if cached:
                print(f"  Using cached AI cover for {title}")
                card = cached
            else:
                print(f"  Generating AI cover for {title}...")
                card = _ai_cover_to_card(openai_client, img, title)
                if card:
                    _save_cached_card(book_dir, CACHE_FILENAME_AI_COVER, card)
                else:
                    print(f"    Falling back to fit mode for {title}.")
                    card = _fit_cover_to_card(img)
        elif mode == "outpaint":
            cached = _get_cached_card(book_dir, CACHE_FILENAME_OUTPAINT)
            if cached:
                print(f"  Using cached outpaint for {title}")
                card = cached
            else:
                print(f"  Outpainting cover for {title}...")
                card = _outpaint_cover_to_card(openai_client, img, title)
                if card:
                    _save_cached_card(book_dir, CACHE_FILENAME_OUTPAINT, card)
                else:
                    print(f"    Falling back to fit mode for {title}.")
                    card = _fit_cover_to_card(img)
        elif mode == "fit":
            card = _fit_cover_to_card(img)
        else:
            card = _crop_cover_to_card(img)
        # Stamp part label (e.g. "Pt. 1") in the bottom-right corner
        if part_name:
            card = _stamp_part_label(card, part_name)

        covers.append((title, card))

    if not covers:
        print("\nNo covers found.", file=sys.stderr)
        sys.exit(1)

    # Layout and save
    sheets = _layout_cards_on_sheets(covers)
    num_pages = len(sheets)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if num_pages == 1:
        sheets[0].save(str(output_path), "PDF", resolution=DPI)
    else:
        sheets[0].save(
            str(output_path),
            "PDF",
            resolution=DPI,
            save_all=True,
            append_images=sheets[1:],
        )

    print(f"\nGenerated {num_pages} page(s) with {len(covers)} cover(s)")
    print(f"Saved to: {output_path}")
    return output_path
