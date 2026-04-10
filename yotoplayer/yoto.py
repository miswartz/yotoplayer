"""Yoto API integration: authentication, audio upload, and card creation."""

import base64
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from .auth import get_settings_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGIN_URL = "https://login.yotoplay.com"
API_URL = "https://api.yotoplay.com"

# Public OAuth2 client ID — registered at https://dashboard.yoto.dev/
# Users can override via YOTO_CLIENT_ID env var if needed.
_DEFAULT_CLIENT_ID = os.environ.get("YOTO_CLIENT_ID", "BydWc2NUmLzL9Awm9pvyS4YvjmXYU0Dg")

TOKENS_FILE = "yoto_tokens.json"


def _get_client_id() -> str:
    cid = os.environ.get("YOTO_CLIENT_ID") or _DEFAULT_CLIENT_ID
    if not cid:
        print(
            "Error: No Yoto client ID configured.\n"
            "  Register at https://dashboard.yoto.dev/ and set YOTO_CLIENT_ID.\n"
            "  Example: set YOTO_CLIENT_ID=your_client_id",
            file=sys.stderr,
        )
        sys.exit(1)
    return cid


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _tokens_path() -> Path:
    return get_settings_dir() / TOKENS_FILE


def _load_tokens() -> Optional[Dict]:
    path = _tokens_path()
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_tokens(tokens: Dict) -> None:
    path = _tokens_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)


def _is_token_expired(access_token: str) -> bool:
    """Check JWT expiration with a 30-second buffer."""
    try:
        payload = access_token.split(".")[1]
        # Fix base64 padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded["exp"] * 1000 < (time.time() * 1000 + 30_000)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _refresh_access_token(tokens: Dict) -> Dict:
    """Use the refresh token to get new tokens."""
    client_id = _get_client_id()
    resp = requests.post(
        f"{LOGIN_URL}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not resp.ok:
        return {}
    new_tokens = resp.json()
    _save_tokens(new_tokens)
    return new_tokens


def device_code_auth() -> Dict:
    """Run the OAuth2 Device Code flow interactively. Returns token dict."""
    client_id = _get_client_id()

    # Step 1: Request device code
    resp = requests.post(
        f"{LOGIN_URL}/oauth/device/code",
        data={
            "client_id": client_id,
            "scope": "profile offline_access openid",
            "audience": API_URL,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not resp.ok:
        print(f"Error starting device auth: {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data.get("verification_uri", "https://login.yotoplay.com/activate")
    verification_uri_complete = data.get("verification_uri_complete", "")
    interval = data.get("interval", 5)
    expires_in = data.get("expires_in", 300)

    # Step 2: Display instructions
    print("\nTo authorize YotoPlayer, visit this URL and enter the code:\n")
    print(f"  URL:  {verification_uri}")
    print(f"  Code: {user_code}\n")
    if verification_uri_complete:
        print(f"  Or open directly: {verification_uri_complete}\n")
    print("Waiting for authorization...")

    # Step 3: Poll for token
    deadline = time.time() + expires_in
    poll_interval = interval

    while time.time() < deadline:
        time.sleep(poll_interval)

        token_resp = requests.post(
            f"{LOGIN_URL}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
                "audience": API_URL,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if token_resp.ok:
            tokens = token_resp.json()
            _save_tokens(tokens)
            print("Authorization successful!")
            return tokens

        body = token_resp.json()
        error = body.get("error", "")

        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            poll_interval += 5
            continue
        elif error == "expired_token":
            print("Device code expired. Please try again.", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Auth error: {body.get('error_description', error)}", file=sys.stderr)
            sys.exit(1)

    print("Authorization timed out. Please try again.", file=sys.stderr)
    sys.exit(1)


def get_yoto_session() -> Optional[requests.Session]:
    """Return an authenticated requests.Session for the Yoto API.

    Returns None if not authenticated (caller should handle gracefully).
    """
    tokens = _load_tokens()
    if not tokens:
        return None

    access_token = tokens.get("access_token", "")

    # Refresh if expired
    if _is_token_expired(access_token):
        tokens = _refresh_access_token(tokens)
        if not tokens:
            return None
        access_token = tokens.get("access_token", "")
        if not access_token:
            return None

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    })
    return session


def is_yoto_authenticated() -> bool:
    """Check if we have stored Yoto tokens."""
    return _tokens_path().exists()


# ---------------------------------------------------------------------------
# Audio upload
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _upload_one_audio(
    session: requests.Session,
    file_path: Path,
) -> Tuple[str, dict]:
    """Upload a single audio file to Yoto.

    Returns (upload_id, transcode_info) where transcode_info contains
    transcodedSha256, transcodedInfo (duration, fileSize, channels, format).
    """
    sha256 = _sha256_file(file_path)

    # Step 1: Get upload URL
    resp = session.get(
        f"{API_URL}/media/transcode/audio/uploadUrl",
        params={"sha256": sha256, "filename": file_path.name},
    )
    resp.raise_for_status()
    data = resp.json()
    upload_url = data["upload"]["uploadUrl"]
    upload_id = data["upload"]["uploadId"]

    # Step 2: Upload if needed (skip if already exists)
    if upload_url:
        file_bytes = file_path.read_bytes()
        put_resp = requests.put(
            upload_url,
            data=file_bytes,
            headers={"Content-Type": "audio/mpeg"},
        )
        put_resp.raise_for_status()

    # Step 3: Wait for transcoding
    transcode_info = _wait_for_transcode(session, upload_id)

    return upload_id, transcode_info


def _wait_for_transcode(
    session: requests.Session,
    upload_id: str,
    max_attempts: int = 300,
    poll_interval: float = 2.0,
) -> dict:
    """Poll until transcoding is complete (up to 10 minutes). Returns the transcode object."""
    for attempt in range(max_attempts):
        resp = session.get(
            f"{API_URL}/media/upload/{upload_id}/transcoded",
            params={"loudnorm": "false"},
        )
        if resp.ok:
            data = resp.json()
            transcode = data.get("transcode", {})
            if transcode.get("transcodedSha256"):
                return transcode

        time.sleep(poll_interval)

    print(f"  Warning: transcode timed out for upload {upload_id}", file=sys.stderr)
    return {}


_UPLOAD_WORKERS = 8


def upload_all_chapters(
    session: requests.Session,
    chapter_files: List[Path],
    chapter_titles: List[str],
) -> List[Dict]:
    """Upload all chapter MP3 files to Yoto in parallel.

    Returns a list of dicts (in order) with keys: title, upload_id, transcode.
    """
    n = len(chapter_files)
    print(f"Uploading {n} chapters to Yoto ({_UPLOAD_WORKERS} workers)...")

    # Pre-fill ordered results list
    results: List[Optional[Dict]] = [None] * n

    with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
        futures = {}
        for i, (fpath, title) in enumerate(zip(chapter_files, chapter_titles)):
            fut = pool.submit(_upload_one_audio, session, fpath)
            futures[fut] = (i, fpath, title)

        with tqdm(total=n, desc="  Uploading", unit="file") as pbar:
            for fut in as_completed(futures):
                i, fpath, title = futures[fut]
                try:
                    upload_id, transcode = fut.result()
                except Exception as exc:
                    print(f"\n  Error uploading {fpath.name}: {exc}", file=sys.stderr)
                    upload_id, transcode = "", {}
                results[i] = {
                    "title": title,
                    "upload_id": upload_id,
                    "transcode": transcode,
                }
                pbar.set_postfix_str(fpath.name[:30])
                pbar.update(1)

    return results


# ---------------------------------------------------------------------------
# Icon upload
# ---------------------------------------------------------------------------

def upload_icon(session: requests.Session, icon_path: Path) -> Optional[str]:
    """Upload a 16x16 chapter icon to Yoto. Returns the mediaId or None."""
    if not icon_path or not icon_path.exists():
        return None

    resp = session.post(
        f"{API_URL}/media/displayIcons/user/me/upload",
        params={"autoConvert": "true", "filename": icon_path.stem},
        data=icon_path.read_bytes(),
        headers={"Content-Type": "image/png"},
    )
    if not resp.ok:
        print(
            f"  Warning: icon upload failed for {icon_path.name} ({resp.status_code})",
            file=sys.stderr,
        )
        return None

    data = resp.json()
    return data.get("displayIcon", {}).get("mediaId")


def upload_chapter_icons(
    session: requests.Session,
    icon_paths: List[Optional[Path]],
) -> List[Optional[str]]:
    """Upload chapter icons to Yoto in parallel. Returns mediaIds in order."""
    to_upload = [(i, p) for i, p in enumerate(icon_paths) if p is not None]
    if not to_upload:
        return [None] * len(icon_paths)

    print(f"Uploading {len(to_upload)} chapter icons...")
    results: List[Optional[str]] = [None] * len(icon_paths)

    with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
        futures = {}
        for i, path in to_upload:
            fut = pool.submit(upload_icon, session, path)
            futures[fut] = i

        with tqdm(total=len(to_upload), desc="  Icons", unit="icon") as pbar:
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:
                    print(f"\n  Icon upload error: {exc}", file=sys.stderr)
                pbar.update(1)

    uploaded = sum(1 for r in results if r is not None)
    print(f"  {uploaded}/{len(to_upload)} icons uploaded.")
    return results


# ---------------------------------------------------------------------------
# Cover image upload
# ---------------------------------------------------------------------------

def upload_cover(session: requests.Session, cover_path: Path) -> Optional[str]:
    """Upload a cover image to Yoto. Returns the mediaUrl or None."""
    if not cover_path or not cover_path.exists():
        return None

    content_type = "image/jpeg"
    if cover_path.suffix.lower() == ".png":
        content_type = "image/png"

    resp = session.post(
        f"{API_URL}/media/coverImage/user/me/upload",
        params={"autoconvert": "true", "coverType": "default"},
        data=cover_path.read_bytes(),
        headers={"Content-Type": content_type},
    )
    if not resp.ok:
        print(f"  Warning: cover upload failed ({resp.status_code})", file=sys.stderr)
        return None

    data = resp.json()
    return data.get("coverImage", {}).get("mediaUrl")


# ---------------------------------------------------------------------------
# Card / playlist creation
# ---------------------------------------------------------------------------

def create_card(
    session: requests.Session,
    title: str,
    description: str,
    upload_results: List[Dict],
    cover_url: Optional[str] = None,
    icon_media_ids: Optional[List[Optional[str]]] = None,
) -> Optional[str]:
    """Create a Yoto MYO playlist card with uploaded chapters.

    Returns the cardId on success, or None.
    """
    chapters = []
    total_duration = 0
    total_size = 0

    for i, item in enumerate(upload_results):
        transcode = item.get("transcode", {})
        info = transcode.get("transcodedInfo", {})
        sha256 = transcode.get("transcodedSha256", "")

        duration = info.get("duration", 0)
        file_size = info.get("fileSize", 0)
        channels = info.get("channels", "stereo")
        fmt = info.get("format", "mp3")

        total_duration += duration
        total_size += file_size

        idx = str(i + 1).zfill(2)
        ch_title = item["title"]

        chapter_obj = {
            "key": idx,
            "title": ch_title,
            "overlayLabel": str(i + 1),
            "tracks": [
                {
                    "key": idx,
                    "title": ch_title,
                    "trackUrl": f"yoto:#{sha256}",
                    "duration": duration,
                    "fileSize": file_size,
                    "channels": channels,
                    "format": fmt,
                    "type": "audio",
                    "overlayLabel": str(i + 1),
                },
            ],
        }

        # Add custom icon if available
        if icon_media_ids and i < len(icon_media_ids) and icon_media_ids[i]:
            icon_ref = f"yoto:#{icon_media_ids[i]}"
            chapter_obj["display"] = {"icon16x16": icon_ref}
            chapter_obj["tracks"][0]["display"] = {"icon16x16": icon_ref}

        chapters.append(chapter_obj)

    content = {
        "title": title,
        "content": {
            "chapters": chapters,
        },
        "metadata": {
            "description": description[:500],
            "category": "stories",
            "media": {
                "duration": total_duration,
                "fileSize": total_size,
            },
        },
    }

    if cover_url:
        content["metadata"]["cover"] = {"imageL": cover_url}

    resp = session.post(
        f"{API_URL}/content",
        json=content,
    )

    if not resp.ok:
        print(f"Error creating Yoto card: {resp.text}", file=sys.stderr)
        return None

    card = resp.json().get("card", {})
    return card.get("cardId")


# ---------------------------------------------------------------------------
# High-level pipeline step
# ---------------------------------------------------------------------------

def upload_to_yoto(
    chapter_files: List[Path],
    chapter_titles: List[str],
    book_title: str,
    authors: List[str],
    narrators: Optional[List[str]] = None,
    cover_path: Optional[Path] = None,
    icon_paths: Optional[List[Optional[Path]]] = None,
) -> bool:
    """Upload processed audiobook chapters to Yoto as a MYO playlist.

    Returns True on success, False on failure.
    """
    session = get_yoto_session()
    if session is None:
        print(
            "\nYoto not authenticated. Run 'yotoplayer yoto-auth' to set up.",
            file=sys.stderr,
        )
        return False

    # Build description
    parts = [f'"{book_title}"']
    if authors:
        parts.append(f"by {', '.join(authors)}")
    if narrators:
        parts.append(f"Narrated by {', '.join(narrators)}")
    parts.append(f"{len(chapter_files)} chapters.")
    description = ". ".join(parts)

    # Upload cover image
    cover_url = None
    if cover_path and cover_path.exists():
        print("\nUploading cover image...")
        cover_url = upload_cover(session, cover_path)
        if cover_url:
            print("  Cover uploaded.")

    # Upload chapter icons
    icon_media_ids = None
    if icon_paths and any(p is not None for p in icon_paths):
        print()
        icon_media_ids = upload_chapter_icons(session, icon_paths)

    # Upload audio files
    print()
    upload_results = upload_all_chapters(session, chapter_files, chapter_titles)

    failed = [r for r in upload_results if not r["transcode"].get("transcodedSha256")]
    if failed:
        print(
            f"\nWarning: {len(failed)} chapter(s) failed to transcode.",
            file=sys.stderr,
        )

    # Create the card
    print("\nCreating Yoto playlist...")
    card_id = create_card(
        session, book_title, description, upload_results, cover_url, icon_media_ids
    )

    if card_id:
        print(f"  Playlist created! Card ID: {card_id}")
        print(f"  View at: https://my.yotoplay.com/card/{card_id}")
        return True
    else:
        print("  Failed to create Yoto playlist.", file=sys.stderr)
        return False
