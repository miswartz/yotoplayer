from dataclasses import dataclass
from typing import List

import requests

THUNDER_API_URL = "https://thunder.api.overdrive.com/v2/"
CLIENT_ID = "dewey"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.0.2 Safari/605.1.15"
)


@dataclass
class SearchResult:
    title: str
    author: str
    narrator: str
    media_id: str
    available_copies: int
    owned_copies: int
    holds_count: int
    estimated_wait_days: int
    cover_url: str
    type_id: str


def search_audiobooks(
    query: str, library_keys: List[str], limit: int = 20
) -> List[SearchResult]:
    """Search for audiobooks across one or more libraries via the Thunder API."""
    results: List[SearchResult] = []
    seen_ids: set[str] = set()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": "https://libbyapp.com/",
            "Origin": "https://libbyapp.com",
        }
    )

    for library_key in library_keys:
        if len(results) >= limit:
            break

        params = {
            "query": query,
            "format": "audiobook-mp3",
            "perPage": min(limit - len(results), 24),
            "page": 1,
            "sort": "relevance",
            "x-client-id": CLIENT_ID,
        }

        try:
            resp = session.get(
                f"{THUNDER_API_URL}libraries/{library_key}/media",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Warning: search failed for library '{library_key}': {e}")
            continue

        data = resp.json()

        # The Thunder API returns items in various possible structures.
        # Try common response shapes.
        items = data if isinstance(data, list) else data.get("items", [])

        for item in items:
            media_id = str(item.get("id", ""))
            if not media_id or media_id in seen_ids:
                continue
            seen_ids.add(media_id)

            # Extract creators
            creators = item.get("creators", [])
            author = next(
                (c["name"] for c in creators if c.get("role", "").lower() == "author"),
                "",
            )
            narrator = next(
                (
                    c["name"]
                    for c in creators
                    if c.get("role", "").lower() == "narrator"
                ),
                "",
            )

            # Extract availability (nested or flat)
            avail = item.get("availability", {})

            # Extract cover URL
            covers = item.get("covers", {})
            cover_url = ""
            if isinstance(covers, dict):
                for key in ("cover300Wide", "cover150Wide", "cover"):
                    cover_entry = covers.get(key)
                    if isinstance(cover_entry, dict):
                        cover_url = cover_entry.get("href", "")
                        break
                    elif isinstance(cover_entry, str):
                        cover_url = cover_entry
                        break

            results.append(
                SearchResult(
                    title=item.get("title", "Unknown"),
                    author=author,
                    narrator=narrator,
                    media_id=media_id,
                    available_copies=avail.get("availableCopies", 0),
                    owned_copies=avail.get("ownedCopies", 0),
                    holds_count=avail.get("holdsCount", 0),
                    estimated_wait_days=avail.get("estimatedWaitDays", 0),
                    cover_url=cover_url,
                    type_id=item.get("type", {}).get("id", "audiobook"),
                )
            )

    return results[:limit]


def display_results(results: List[SearchResult]) -> None:
    """Print search results in a numbered list."""
    if not results:
        print("No results found.")
        return

    max_num_width = len(str(len(results)))
    for i, r in enumerate(results, 1):
        availability = (
            f"Available ({r.available_copies})"
            if r.available_copies > 0
            else f"Wait ~{r.estimated_wait_days}d ({r.holds_count} holds)"
        )
        narrator_str = f" / {r.narrator}" if r.narrator else ""
        print(
            f"  {i:>{max_num_width}}. {r.title} — {r.author}{narrator_str}  [{availability}]"
        )
