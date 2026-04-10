import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional

import urllib3
from odmpy.libby import LibbyClient

# OverDrive's sentry-read.svc.overdrive.com currently serves a cert for
# *.odrsre.overdrive.com — hostname mismatch on their end.  Suppress the
# InsecureRequestWarning until they fix it.
warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)

SETTINGS_DIR = Path.home() / ".yotoplayer"


def get_settings_dir() -> Path:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    return SETTINGS_DIR


def _patch_client_ssl(client: LibbyClient) -> None:
    """Disable SSL verification on the LibbyClient session.

    OverDrive's Libby API endpoint currently has a certificate hostname
    mismatch.  This is a server-side issue on their end.
    """
    client.libby_session.verify = False


def get_client() -> LibbyClient:
    """Return an authenticated LibbyClient, prompting for setup if needed."""
    settings_dir = get_settings_dir()
    client = LibbyClient(settings_folder=str(settings_dir), max_retries=2, timeout=30)
    _patch_client_ssl(client)

    if not client.has_chip():
        setup_auth(client)
        return client

    # We have a chip — check if it actually has cards
    try:
        synced = client.sync()
        if synced.get("result") == "synchronized" and synced.get("cards"):
            return client  # Already fully authenticated
    except Exception:
        pass

    # Chip exists but no cards — need to link via setup code
    setup_auth(client)
    return client


def setup_auth(client: Optional[LibbyClient] = None) -> LibbyClient:
    """Interactive first-time authentication flow."""
    if client is None:
        settings_dir = get_settings_dir()
        client = LibbyClient(
            settings_folder=str(settings_dir), max_retries=2, timeout=30
        )
        _patch_client_ssl(client)

    if not client.has_chip():
        print("Getting identity token from Libby...")
        client.get_chip()

    print()
    print("Choose how to connect your Libby account:")
    print("  1. Setup code (copy from your Libby phone app)")
    print("  2. Library card (enter your library card number and PIN directly)")
    print()

    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice in ("1", "2"):
            break

    if choice == "1":
        _setup_via_code(client)
    else:
        _setup_via_card(client)

    return client


def _setup_via_code(client: LibbyClient) -> None:
    """Clone account using a code from the user's Libby phone app."""
    print()
    print("On your phone, open the Libby app:")
    print("  1. Tap the menu icon (☰) at the bottom")
    print("  2. Tap 'Copy To Another Device'")
    print("  3. Libby will show you an 8-digit code")
    print()

    while True:
        code = input("Enter the 8-digit code from Libby: ").strip().replace(" ", "")
        if not code:
            return
        if LibbyClient.is_valid_sync_code(code):
            break
        print("Invalid code. Please enter the 8-digit code shown in Libby.")

    print("\nCloning account...")
    try:
        client.clone_by_code(code)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Make sure you entered the code within the time limit.", file=sys.stderr)
        sys.exit(1)

    # Refresh the chip to get an updated identity token with accounts populated
    try:
        client.get_chip(authenticated=True)
    except Exception:
        pass

    if not client.is_logged_in():
        print("Error: Could not log in with that code.", file=sys.stderr)
        print("Make sure you entered the right code within the time limit", file=sys.stderr)
        print("and have at least 1 registered library card.", file=sys.stderr)
        sys.exit(1)

    synced = client.sync()
    cards = synced.get("cards", [])
    if cards:
        lib_name = cards[0].get("library", {}).get("name", "your library")
        print(f"Authenticated with {lib_name}!")
    else:
        print("Logged in but no library cards found.")


def _setup_via_card(client: LibbyClient) -> None:
    """Link account by entering library card credentials directly."""
    from odmpy.overdrive import OverDriveClient

    print()
    print("Enter your library name or short key (e.g., 'smdl', 'seattle', 'lapl').")
    print()
    library_input = input("Library name or key: ").strip()
    if not library_input:
        print("Error: Library name required.", file=sys.stderr)
        sys.exit(1)

    # Resolve library key to numeric websiteId
    website_id = _resolve_website_id(library_input)
    if not website_id:
        print(f"Error: Could not find library '{library_input}'.", file=sys.stderr)
        sys.exit(1)

    # Get the auth form to find available ILS branches
    ils = "default"
    try:
        auth_info = client.auth_form(website_id)
        forms = auth_info.get("forms", [])
        if len(forms) > 1:
            print(f"\nThis library has {len(forms)} branches. Enter a search term")
            print("to find yours (e.g., 'lincoln', 'paw paw', 'niles'):")
            search = input("Branch search: ").strip().lower()
            matches = [f for f in forms if search in f.get("displayName", "").lower()]
            if not matches:
                print("No matching branches found.", file=sys.stderr)
                sys.exit(1)
            elif len(matches) == 1:
                ils = matches[0].get("ilsName", "default")
                print(f"  Using: {matches[0].get('displayName')} (ils: {ils})")
            else:
                print()
                for i, m in enumerate(matches[:20], 1):
                    print(f"  {i}. {m.get('displayName', '?')}")
                choice = input("Enter number: ").strip()
                idx = int(choice) - 1
                ils = matches[idx].get("ilsName", "default")
                print(f"  Using ils: {ils}")
        elif forms:
            ils = forms[0].get("ilsName", "default")
    except Exception:
        pass

    print()
    username = input("Library card number (or username): ").strip()
    password = input("PIN (or password): ").strip()

    if not username:
        print("Error: Card number required.", file=sys.stderr)
        sys.exit(1)

    print("\nLinking library card...")
    try:
        result = client.link_card(website_id, username, password, ils)
        cards = result.get("cards", [])
        if cards:
            lib_name = cards[0].get("library", {}).get("name", website_id)
            print(f"Library card linked: {lib_name}")
            client.save_settings({"__libby_sync_code": "card_linked"})
        else:
            print("Card linked but no card data returned. Checking sync...")
            client.save_settings({"__libby_sync_code": "card_linked"})
            _verify_login(client)
    except Exception as e:
        print(f"Error linking card: {e}", file=sys.stderr)
        print("Check your website ID, card number, and PIN.", file=sys.stderr)
        sys.exit(1)


def _verify_login(client: LibbyClient) -> None:
    """Verify login by checking sync for cards, with retries."""
    import time

    for attempt in range(5):
        try:
            synced = client.sync()
            if synced.get("result") == "synchronized" and synced.get("cards"):
                cards = synced["cards"]
                lib_name = cards[0].get("library", {}).get("name", "your library")
                print(f"Authenticated with {lib_name}!")
                return
            elif attempt < 4:
                print(f"  Waiting for sync... (attempt {attempt + 1}/5)")
                time.sleep(3)
            else:
                print("Error: No library cards found after syncing.", file=sys.stderr)
                print("Try option 2 (library card) to link directly.", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            if attempt < 4:
                print(f"  Retrying... ({e})")
                time.sleep(3)
            else:
                print(f"Error: Could not verify login: {e}", file=sys.stderr)
                sys.exit(1)


def get_cards(client: LibbyClient) -> List[Dict]:
    """Get the user's library cards from a sync."""
    synced = client.sync()
    return synced.get("cards", [])


def get_library_keys(client: LibbyClient) -> List[str]:
    """Get advantageKey values for all linked library cards."""
    cards = get_cards(client)
    keys = []
    for card in cards:
        key = card.get("advantageKey", "")
        if key and key not in keys:
            keys.append(key)
    return keys


def print_libraries(client: LibbyClient) -> None:
    """Print linked library info."""
    cards = get_cards(client)
    if not cards:
        print("No library cards linked.")
        return
    print(f"Linked libraries ({len(cards)}):")
    for card in cards:
        lib_name = card.get("library", {}).get("name", "Unknown")
        advantage_key = card.get("advantageKey", "?")
        print(f"  - {lib_name} ({advantage_key})")


def _resolve_website_id(library_input: str) -> Optional[str]:
    """Resolve a library key/name to its numeric websiteId.

    Tries direct lookup by key first, then returns the numeric ID as a string.
    """
    import requests

    # If it's already numeric, return as-is
    if library_input.isdigit():
        return library_input

    # Try direct lookup by key
    try:
        r = requests.get(
            f"https://thunder.api.overdrive.com/v2/libraries/{library_input}",
            params={"x-client-id": "dewey"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            wid = data.get("websiteId")
            name = data.get("name", library_input)
            if wid:
                print(f"  Found: {name} (websiteId: {wid})")
                return str(wid)
    except requests.RequestException:
        pass

    print(f"  Could not resolve '{library_input}' to a library.")
    return None
