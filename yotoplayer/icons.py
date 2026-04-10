"""Chapter icon generation using GPT-4o-mini + Retro Diffusion.

Step 1: GPT-4o-mini picks a single iconic object for the chapter.
Step 2: Retro Diffusion generates a native 16x16 pixel art sprite.

Requires openai_api_key and retro_diffusion_api_key in
~/.yotoplayer/config.json (or OPENAI_API_KEY / RETRO_DIFFUSION_API_KEY env vars).
"""

import base64
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from tqdm import tqdm

from yotoplayer import config

_ICON_SIZE = 16
_ICON_WORKERS = 2

_RD_API_URL = "https://api.retrodiffusion.ai/v1/inferences"

# ---------------------------------------------------------------------------
# Step 1: Text LLM picks the object
# ---------------------------------------------------------------------------

_OBJECT_SYSTEM_PROMPT = """\
You are an icon designer for audiobook chapters displayed on a tiny 16x16 pixel LED screen.
Given a chapter name, book title, and book description, pick ONE simple, concrete, \
instantly recognizable physical object that best represents the chapter.

Rules:
- Output ONLY the object name (1-3 words). No explanation, no punctuation.
- Pick something with a strong, distinctive silhouette (umbrella, dragon, crown, bird, key, etc.)
- Avoid abstract concepts — pick a tangible thing that can be drawn as a sprite.
- Avoid faces/people — simple objects read better at 16x16.
- Each chapter should get a unique object if possible."""


def _get_openai_client():
    """Return an OpenAI client if openai_api_key is configured, else None."""
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


def _pick_object(
    client,
    book_title: str,
    book_description: str,
    chapter_name: str,
) -> Optional[str]:
    """Use GPT-4o-mini to pick a single iconic object for a chapter."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _OBJECT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f'Book: "{book_title}"\n'
                        f"Description: {book_description[:300]}\n"
                        f'Chapter: "{chapter_name}"\n\n'
                        f"Object:"
                    ),
                },
            ],
            temperature=0.7,
            max_tokens=20,
        )
        obj = resp.choices[0].message.content.strip().strip('"').strip(".")
        return obj if obj else None
    except Exception as exc:
        print(f"\n  Error picking object for '{chapter_name}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Step 2: Retro Diffusion generates the pixel art sprite
# ---------------------------------------------------------------------------


def _generate_sprite_rd(rd_api_key: str, object_name: str) -> Optional[bytes]:
    """Generate a 16x16 pixel art sprite via Retro Diffusion API."""
    prompt = (
        f"A {object_name}, simple icon, clear silhouette, "
        f"bold colors, centered"
    )
    try:
        resp = requests.post(
            _RD_API_URL,
            headers={"X-RD-Token": rd_api_key},
            json={
                "prompt": prompt,
                "width": _ICON_SIZE,
                "height": _ICON_SIZE,
                "num_images": 1,
                "prompt_style": "rd_plus__low_res",
                "remove_bg": True,
                "upscale_output_factor": 1,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        images = data.get("base64_images", [])
        if not images:
            print(f"\n  No image returned for '{object_name}'", file=sys.stderr)
            return None
        return base64.b64decode(images[0])
    except Exception as exc:
        print(f"\n  Error generating sprite for '{object_name}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Combined pipeline for one chapter
# ---------------------------------------------------------------------------


def _generate_one_icon(
    openai_client,
    rd_api_key: str,
    book_title: str,
    book_description: str,
    chapter_name: str,
    output_path: Path,
) -> Tuple[Optional[Path], Optional[str]]:
    """Pick an object then generate a pixel art sprite. Returns (path, object_name)."""
    # Step 1: pick the object
    obj = _pick_object(openai_client, book_title, book_description, chapter_name)
    if not obj:
        return None, None

    # Step 2: generate the sprite via Retro Diffusion
    image_bytes = _generate_sprite_rd(rd_api_key, obj)
    if not image_bytes:
        return None, obj

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    return output_path, obj


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_chapter_icons(
    chapter_titles: List[str],
    book_title: str,
    book_description: str,
    output_dir: Path,
) -> List[Optional[Path]]:
    """Generate 16x16 pixel art icons for each chapter.

    Two-step pipeline:
      1. GPT-4o-mini picks a single iconic object per chapter.
      2. Retro Diffusion generates a native 16x16 pixel art sprite.

    Requires OPENAI_API_KEY and RETRO_DIFFUSION_API_KEY env vars.
    Returns a list of Paths (or None for failures) in chapter order.
    """
    openai_client = _get_openai_client()
    if openai_client is None:
        print(
            "\nSkipping icon generation (set openai_api_key in "
            "~/.yotoplayer/config.json or OPENAI_API_KEY env var).",
            file=sys.stderr,
        )
        return [None] * len(chapter_titles)

    rd_api_key = config.get("retro_diffusion_api_key", "RETRO_DIFFUSION_API_KEY")
    if not rd_api_key:
        print(
            "\nSkipping icon generation (set retro_diffusion_api_key in "
            "~/.yotoplayer/config.json or RETRO_DIFFUSION_API_KEY env var).",
            file=sys.stderr,
        )
        return [None] * len(chapter_titles)

    icons_dir = output_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    n = len(chapter_titles)
    print(f"\nGenerating {n} chapter icons ({_ICON_WORKERS} workers)...")

    results: List[Optional[Path]] = [None] * n
    objects: List[Optional[str]] = [None] * n

    with ThreadPoolExecutor(max_workers=_ICON_WORKERS) as pool:
        futures = {}
        for i, title in enumerate(chapter_titles):
            idx = str(i + 1).zfill(2)
            out_path = icons_dir / f"icon_{idx}.png"

            # Skip if already generated (re-run friendly)
            if out_path.exists():
                results[i] = out_path
                continue

            fut = pool.submit(
                _generate_one_icon,
                openai_client,
                rd_api_key,
                book_title,
                book_description,
                title,
                out_path,
            )
            futures[fut] = i

        if not futures:
            print("  All icons already cached.")
            return results

        with tqdm(total=len(futures), desc="  Generating", unit="icon") as pbar:
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    path, obj = fut.result()
                    results[i] = path
                    objects[i] = obj
                    if obj:
                        pbar.set_postfix_str(f"{obj}")
                except Exception as exc:
                    print(
                        f"\n  Error for chapter {i+1}: {exc}",
                        file=sys.stderr,
                    )
                pbar.update(1)

    # Print summary
    generated = sum(1 for r in results if r is not None)
    print(f"  {generated}/{n} icons ready.")
    for i, (title, obj) in enumerate(zip(chapter_titles, objects)):
        if obj:
            print(f"    Ch {i+1}: {title} -> {obj}")

    return results
