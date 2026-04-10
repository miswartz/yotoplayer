import json
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from odmpy.libby import LibbyClient, LibbyMediaTypes


def find_loan_for_media(client: LibbyClient, media_id: str) -> Optional[Dict]:
    """Check if the user already has this title borrowed."""
    loans = client.get_loans()
    for loan in loans:
        if str(loan.get("id")) == str(media_id):
            return loan
    return None


def borrow_title(client: LibbyClient, media_id: str) -> Dict:
    """Borrow a title. Returns the loan dict."""
    cards = client.sync().get("cards", [])
    if not cards:
        print("Error: No library cards found.", file=sys.stderr)
        sys.exit(1)

    # Try each card until borrow succeeds
    last_error = None
    for card in cards:
        card_id = card["cardId"]
        try:
            loan = client.borrow_title(
                title_id=media_id,
                title_format=str(LibbyMediaTypes.Audiobook),
                card_id=card_id,
            )
            return loan
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"Could not borrow title {media_id}: {last_error}")


def place_hold(client: LibbyClient, media_id: str) -> None:
    """Place a hold on a title."""
    cards = client.sync().get("cards", [])
    if not cards:
        print("Error: No library cards found.", file=sys.stderr)
        sys.exit(1)

    for card in cards:
        try:
            client.create_hold(title_id=media_id, card_id=card["cardId"])
            print("Hold placed successfully.")
            return
        except Exception:
            continue

    print("Error: Could not place hold.", file=sys.stderr)


def ensure_borrowed(
    client: LibbyClient, media_id: str, available_copies: int
) -> Dict:
    """Ensure the title is borrowed, borrowing it if needed. Returns the loan."""
    loan = find_loan_for_media(client, media_id)
    if loan:
        print("Title is already borrowed.")
        return loan

    # Always try to borrow — Thunder API availability data can be stale,
    # especially for consortium libraries.
    print("Borrowing title...")
    try:
        loan = borrow_title(client, media_id)
        print("Borrowed successfully!")
        refreshed = find_loan_for_media(client, media_id)
        return refreshed or loan
    except RuntimeError:
        if available_copies <= 0:
            print("This title is not currently available.")
            ans = input("Place a hold? (y/n): ").strip().lower()
            if ans == "y":
                place_hold(client, media_id)
            sys.exit(0)
        raise


def download_audiobook(
    client: LibbyClient, loan: Dict, output_dir: Path
) -> Tuple[List[Path], List[Dict]]:
    """
    Download audiobook parts via Libby's web player.

    Flow:
    1. Call open_loan with Edge's Bearer token to get dewey host + manifest params
    2. Load manifest in Playwright headless browser, let JS decrypt eData
    3. Extract bData (spine paths, signed cmpt values, chapters, d cookie value)
    4. Download MP3 parts with d cookie + signed cmpt params

    Returns (list of downloaded part files, list of chapter dicts).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    title_id = str(loan["id"])
    card_id = str(loan["cardId"])
    title = loan.get("title", "Unknown")

    # Step 1: Get manifest URL from open_loan
    print("Opening audiobook via Libby API...")
    edge_token = _load_edge_token()
    open_loan_data = _call_open_loan(edge_token, card_id, title_id)

    web_url = open_loan_data["urls"]["web"]
    message = open_loan_data["message"]
    manifest_url = f"{web_url}?{message}"
    dewey_host = urlparse(web_url).netloc
    base_url = f"https://{dewey_host}/"

    # Step 2: Load manifest in Playwright to decrypt eData
    print("Loading audiobook manifest...")
    bdata = _extract_bdata_via_playwright(manifest_url)

    if not bdata or "b" not in bdata:
        print("Error: Could not extract book data from manifest.", file=sys.stderr)
        sys.exit(1)

    book_data = bdata["b"]

    # Save raw book data
    with open(output_dir / "bookdata.json", "w", encoding="utf-8") as f:
        json.dump(book_data, f, indent=2)

    # Extract spine (MP3 part paths) and signed cmpt values
    spine = book_data.get("spine", [])
    cmpt_params = book_data.get("-odread-cmpt-params", [])
    if not spine or not cmpt_params:
        print("Error: No audio parts or signed params found.", file=sys.stderr)
        sys.exit(1)

    # Extract the d cookie value from bonafides
    d_cookie_value = book_data.get("-odread-bonafides-d", "")
    if not d_cookie_value:
        print("Error: No d cookie value in book data.", file=sys.stderr)
        sys.exit(1)

    # Extract creators
    creators = book_data.get("creator", [])
    authors = [c["name"] for c in creators if c.get("role") == "author"]
    if not authors:
        authors = [c["name"] for c in creators]
    narrators = [c["name"] for c in creators if c.get("role") == "narrator"]

    # Extract chapters from nav.toc
    chapters = _extract_chapters(book_data, spine)

    # Download cover
    cover_path = _download_cover(loan, output_dir)

    # Step 3: Download each MP3 part
    part_files: List[Path] = []
    print(f"Downloading {len(spine)} part(s)...")

    session = requests.Session()
    session.verify = False
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
    session.headers.update({"User-Agent": ua})
    session.cookies.set("d", d_cookie_value, domain=dewey_host, path="/")

    for i, (part, cmpt) in enumerate(zip(spine, cmpt_params)):
        part_number = i + 1
        part_path_str = part.get("path", "")
        if not part_path_str:
            continue

        part_url = f"{base_url}{part_path_str}?{cmpt}"
        part_filename = output_dir / f"part-{part_number:03d}.mp3"

        if part_filename.exists():
            existing_size = part_filename.stat().st_size
            expected_bytes = part.get("-odread-file-bytes", 0)
            if expected_bytes and existing_size >= expected_bytes:
                print(f"  Part {part_number}: already downloaded")
                part_files.append(part_filename)
                continue

        part_tmp = part_filename.with_suffix(".part")
        already = part_tmp.stat().st_size if part_tmp.exists() else 0
        headers = {}
        if already:
            headers["Range"] = f"bytes={already}-"

        try:
            resp = session.get(part_url, headers=headers, timeout=120, stream=True)
            # If resume range is invalid, restart from scratch
            if resp.status_code == 416:
                part_tmp.unlink(missing_ok=True)
                already = 0
                resp = session.get(part_url, timeout=120, stream=True)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0)) + already
            with tqdm(
                total=total or None,
                initial=already,
                unit="B",
                unit_scale=True,
                desc=f"  Part {part_number:2d}",
            ) as pbar:
                with open(part_tmp, "ab" if already else "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                        pbar.update(len(chunk))

            part_tmp.replace(part_filename)
        except requests.RequestException as e:
            print(f"  Error downloading part {part_number}: {e}", file=sys.stderr)
            sys.exit(1)

        part_files.append(part_filename)

    # Save chapter info + book metadata
    book_meta = {
        "title": title,
        "authors": authors,
        "narrators": narrators,
        "cover": str(cover_path) if cover_path else None,
        "chapters": chapters,
    }
    chapters_path = output_dir / "chapters.json"
    with open(chapters_path, "w", encoding="utf-8") as f:
        json.dump(book_meta, f, indent=2)

    print(f"Downloaded {len(part_files)} parts, {len(chapters)} chapters found.")
    return part_files, chapters


def _load_edge_token() -> str:
    """Load the Edge identity token from ~/.yotoplayer/edge_identity.txt."""
    token_path = Path.home() / ".yotoplayer" / "edge_identity.txt"
    if not token_path.exists():
        print(
            "Error: Edge identity token not found.\n"
            "To get it:\n"
            "  1. Open Edge, go to libbyapp.com and open an audiobook\n"
            "  2. Open DevTools (F12) > Application > Local Storage > libbyapp.com\n"
            '  3. Find the "dewey:sentry.identity" key\n'
            "  4. Copy the token value (starts with eyJ...)\n"
            f"  5. Save it to {token_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    return token_path.read_text().strip()


def _call_open_loan(token: str, card_id: str, title_id: str) -> Dict:
    """Call the Libby open_loan API with an Edge Bearer token."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    resp = requests.get(
        f"https://sentry.libbyapp.com/open/audiobook/card/{card_id}/title/{title_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Origin": "https://libbyapp.com",
            "Referer": "https://libbyapp.com/",
        },
        verify=False,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Error: open_loan returned {resp.status_code}", file=sys.stderr)
        try:
            err = resp.json()
            print(f"  {err}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)
    return resp.json()


def _extract_bdata_via_playwright(manifest_url: str) -> Optional[Dict]:
    """
    Load the manifest in Playwright headless, let JS decrypt eData,
    and extract the decoded bData via a JSON.parse hook.
    """
    from playwright.sync_api import sync_playwright

    bdata = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Hook JSON.parse to capture bData before rumi module consumes it
        page.add_init_script("""
            window.__capturedBData = null;
            const _origParse = JSON.parse;
            JSON.parse = function(text, reviver) {
                const result = _origParse.call(this, text, reviver);
                if (result && typeof result === 'object' &&
                    result.b && result.b.spine && result.b['-odread-cmpt-params']) {
                    window.__capturedBData = _origParse(JSON.stringify(result));
                }
                return result;
            };
        """)

        page.goto(manifest_url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_function(
                "window.__capturedBData !== null",
                timeout=30000,
            )
            bdata = page.evaluate("window.__capturedBData")
        except Exception as e:
            print(f"  Warning: bData capture timed out: {e}", file=sys.stderr)
            # Fallback: try reading from BIF.map directly
            try:
                page.wait_for_function(
                    "typeof BIF !== 'undefined' && BIF.map && BIF.map.spine",
                    timeout=10000,
                )
                bdata = {"b": page.evaluate("""
                    (() => {
                        var m = {};
                        for (var k in BIF.map) {
                            if (typeof BIF.map[k] !== 'function') m[k] = BIF.map[k];
                        }
                        return m;
                    })()
                """)}
            except Exception:
                pass

        browser.close()

    return bdata


def _extract_chapters(book_data: Dict, spine: List) -> List[Dict]:
    """Extract chapter info from book_data nav.toc."""
    from urllib.parse import unquote

    chapters = []
    nav = book_data.get("nav", {})
    toc = nav.get("toc", [])

    # Build a mapping from spine path to cumulative offset
    # Spine paths may be URL-encoded, toc paths may not — normalize both
    spine_offsets = {}
    cumulative = 0.0
    for i, part in enumerate(spine):
        raw_path = part.get("path", "")
        decoded_path = unquote(raw_path)
        spine_offsets[decoded_path] = cumulative
        spine_offsets[raw_path] = cumulative  # also store encoded version
        duration = part.get("audio-duration", 0)
        cumulative += duration

    def process_toc_entries(entries, depth=0):
        for entry in entries:
            title = entry.get("title", "")
            path = entry.get("path", "")

            # Parse the path to get spine reference and time offset
            if "#" in path:
                spine_ref, fragment = path.split("#", 1)
            else:
                spine_ref = path
                fragment = ""

            # Parse time from fragment (could be just seconds or t=seconds)
            time_offset = 0.0
            if fragment:
                time_match = re.search(r"(?:t=)?([\d.]+)", fragment)
                if time_match:
                    time_offset = float(time_match.group(1))

            # Find matching spine entry (try both encoded and decoded)
            base_offset = None
            decoded_ref = unquote(spine_ref)
            for sp_path, offset in spine_offsets.items():
                if sp_path == spine_ref or sp_path == decoded_ref:
                    base_offset = offset
                    break

            if base_offset is None:
                # Fallback: partial match
                for sp_path, offset in spine_offsets.items():
                    if spine_ref in sp_path or sp_path in spine_ref:
                        base_offset = offset
                        break

            if base_offset is None:
                base_offset = 0.0

            start = base_offset + time_offset
            chapters.append({"title": title, "start": start, "end": 0.0})

            # Process nested chapters
            if "contents" in entry:
                process_toc_entries(entry["contents"], depth + 1)

    process_toc_entries(toc)

    # Fill in end times
    for i in range(len(chapters) - 1):
        chapters[i]["end"] = chapters[i + 1]["start"]
    if chapters:
        chapters[-1]["end"] = cumulative

    return chapters


def _download_cover(loan: Dict, output_dir: Path) -> Optional[Path]:
    """Download cover image from loan metadata."""
    cover_url = _get_cover_url(loan)
    if not cover_url:
        return None
    try:
        resp = requests.get(cover_url, timeout=15)
        resp.raise_for_status()
        cover_path = output_dir / "cover.jpg"
        cover_path.write_bytes(resp.content)
        return cover_path
    except requests.RequestException:
        return None


def _get_cover_url(loan: Dict) -> str:
    covers = loan.get("covers", {})
    if isinstance(covers, dict):
        for key in ("cover300Wide", "cover150Wide", "cover"):
            entry = covers.get(key)
            if isinstance(entry, dict):
                return entry.get("href", "")
            elif isinstance(entry, str):
                return entry
    return ""
