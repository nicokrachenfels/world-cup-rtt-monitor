"""
Scrape all World Cup match get-in prices from the TicketData performer page.
Uses cloudscraper to bypass Cloudflare bot protection.
"""
import logging
import re
from typing import Optional

import cloudscraper

logger = logging.getLogger(__name__)

TICKETDATA_MATCHES_URL = "https://www.ticketdata.com/performer/world-cup-soccer?view=matches"


def scrape_all_matches() -> dict[str, dict]:
    """
    Scrape the TicketData World Cup matches page.
    Returns: {normalized_match_key: {"get_in": float, "teams": str, "date": str}}
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )

    try:
        logger.info("Fetching TicketData World Cup matches page...")
        resp = scraper.get(TICKETDATA_MATCHES_URL, timeout=20)
        resp.raise_for_status()
        html = resp.text
        logger.info(f"TicketData response: {resp.status_code}, {len(html)} chars")
    except Exception as e:
        logger.error(f"TicketData fetch failed: {e}")
        return {}

    # Log a preview to help diagnose
    preview = html[:300].replace("\n", " ")
    logger.info(f"TicketData HTML preview: {preview!r}")

    # Log API URLs and NEXT_DATA structure for debugging
    import json as _json
    api_urls = re.findall(r'["\'](/api/[^"\'?#]{3,60})["\']', html)
    logger.info(f"API paths found in HTML: {list(set(api_urls))[:15]}")

    # Log __NEXT_DATA__ top-level keys to find where events live
    next_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if next_match:
        try:
            nd = _json.loads(next_match.group(1))
            def _keys(obj, prefix="", depth=0):
                if depth > 3 or not isinstance(obj, dict): return
                for k, v in obj.items():
                    p = f"{prefix}.{k}" if prefix else k
                    logger.info(f"  NEXT_DATA key: {p} ({type(v).__name__}, len={len(v) if isinstance(v, (list,dict,str)) else '-'})")
                    _keys(v, p, depth+1)
            _keys(nd)
        except Exception as e:
            logger.info(f"NEXT_DATA parse error: {e}")

    # Also try calling common event API patterns directly
    for api_path in [
        "/api/events?performer=world-cup-soccer",
        "/api/performer/world-cup-soccer/events",
        "/api/events?view=matches&performer=world-cup-soccer",
    ]:
        try:
            api_resp = scraper.get(f"https://www.ticketdata.com{api_path}", timeout=10)
            if api_resp.status_code == 200 and api_resp.text.strip().startswith("{"):
                logger.info(f"API hit {api_path}: {api_resp.text[:200]}")
        except Exception:
            pass

    matches = _parse_matches_html(html)
    logger.info(f"Parsed {len(matches)} matches from TicketData")
    return matches


def _parse_matches_html(html: str) -> dict[str, dict]:
    """
    Parse match rows and get-in prices from the TicketData HTML.
    The page lists events as rows with team names, dates, and a get-in price.
    """
    matches: dict[str, dict] = {}

    # Look for JSON data embedded in the page (common for React/Next.js apps)
    json_matches = _try_extract_json(html)
    if json_matches:
        return json_matches

    # Fallback: regex-based extraction from rendered HTML
    # Pattern: find blocks containing "vs" and a "$" price nearby
    blocks = re.split(r'(?=<(?:tr|li|div)[^>]*class[^>]*(?:event|row|item|match))', html, flags=re.IGNORECASE)
    if len(blocks) <= 1:
        # Try splitting on any tag boundary near a price
        blocks = re.findall(r'<(?:tr|li|div|article)[^>]*>.*?</(?:tr|li|div|article)>', html, re.DOTALL | re.IGNORECASE)

    for block in blocks:
        text = re.sub(r'<[^>]+>', ' ', block)  # strip HTML tags
        text = re.sub(r'\s+', ' ', text).strip()

        if 'vs' not in text.lower() or '$' not in text:
            continue

        parsed = _parse_match_text(text)
        if parsed:
            key = parsed["teams"].lower().strip()
            matches[key] = parsed

    return matches


def _try_extract_json(html: str) -> dict[str, dict]:
    """Try to find embedded JSON event data (Next.js __NEXT_DATA__ or similar)."""
    import json

    # Next.js embeds page props in <script id="__NEXT_DATA__">
    next_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not next_match:
        return {}

    try:
        data = json.loads(next_match.group(1))
    except json.JSONDecodeError:
        return {}

    # Walk the JSON tree looking for event arrays
    events = _find_events_in_json(data)
    if not events:
        return {}

    matches = {}
    for event in events:
        parsed = _parse_json_event(event)
        if parsed:
            key = parsed["teams"].lower().strip()
            matches[key] = parsed
            logger.info(f"  JSON event: {parsed['teams']} | ${parsed['get_in']:,.0f}")

    return matches


def _find_events_in_json(obj, depth=0):
    """Recursively search JSON for an array of event-like objects."""
    if depth > 8:
        return []
    if isinstance(obj, list) and len(obj) > 0:
        first = obj[0]
        if isinstance(first, dict) and any(k in first for k in ("name", "title", "performers", "datetime_local")):
            return obj
    if isinstance(obj, dict):
        for val in obj.values():
            result = _find_events_in_json(val, depth + 1)
            if result:
                return result
    return []


def _parse_json_event(event: dict) -> Optional[dict]:
    """Parse a SeatGeek-style event JSON object."""
    title = event.get("title") or event.get("name") or ""
    if "world cup" not in title.lower() and "vs" not in title.lower():
        return None

    # Get-in price
    price = (
        event.get("stats", {}).get("lowest_price")
        or event.get("lowest_price")
        or event.get("min_price")
    )
    if not price:
        return None

    date = event.get("datetime_local", event.get("date", "Unknown"))

    return {
        "teams": title,
        "match_date": str(date)[:10],
        "venue": event.get("venue", {}).get("name", "Unknown") if isinstance(event.get("venue"), dict) else "Unknown",
        "get_in": float(price),
    }


def _parse_match_text(text: str) -> Optional[dict]:
    """Parse match info from a plain-text block."""
    # Extract price
    prices = [float(m.replace(",", "")) for m in re.findall(r"\$([\d,]+(?:\.\d{2})?)", text) if 10 < float(m.replace(",", "")) < 100_000]
    if not prices:
        return None

    get_in = min(prices)

    # Team names
    vs_match = re.search(
        r"([A-Z][a-zA-Z\s'\-\.]{2,30?})\s+vs\.?\s+([A-Z][a-zA-Z\s'\-\.]{2,30?})(?:\s|$|\|)",
        text,
    )
    teams = f"{vs_match.group(1).strip()} vs {vs_match.group(2).strip()}" if vs_match else "Unknown vs Unknown"

    date_match = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*[\s,]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}",
        text, re.IGNORECASE,
    )
    match_date = date_match.group(0) if date_match else "Unknown"

    return {"teams": teams, "match_date": match_date, "venue": "Unknown", "get_in": get_in}


def _normalize_team(name: str) -> str:
    replacements = {
        "united states": "usa", "u.s.a.": "usa", "u.s.": "usa",
        "türkiye": "turkey", "turkiye": "turkey",
        "côte d'ivoire": "ivory coast", "cote d'ivoire": "ivory coast",
    }
    n = name.lower().strip()
    return replacements.get(n, n)


def find_get_in_price(home_team: str, away_team: str, td_matches: dict[str, dict]) -> Optional[float]:
    """Fuzzy-match a FIFA match to a TicketData entry and return the get-in price."""
    if home_team == "TBD" or away_team == "TBD":
        return None
    if not td_matches:
        return None

    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)

    best_price: Optional[float] = None
    best_score = 0

    for key, match_data in td_matches.items():
        parts = key.split(" vs ")
        if len(parts) != 2:
            continue
        td_home, td_away = parts[0].strip(), parts[1].strip()

        score = 0
        if home_norm in td_home or td_home in home_norm:
            score += 1
        if away_norm in td_away or td_away in away_norm:
            score += 1
        if score < 2:
            # try reversed
            rev = 0
            if home_norm in td_away or td_away in home_norm:
                rev += 1
            if away_norm in td_home or td_home in away_norm:
                rev += 1
            score = max(score, rev)

        if score > best_score:
            best_score = score
            best_price = match_data["get_in"]

    if best_score >= 2:
        return best_price
    logger.debug(f"No TicketData match for {home_team} vs {away_team} (best score: {best_score})")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matches = scrape_all_matches()
    if matches:
        print(f"\n{'TEAMS':<40} {'DATE':<15} {'GET-IN':>8}")
        print("-" * 65)
        for key, m in sorted(matches.items()):
            print(f"{m['teams']:<40} {m['match_date']:<15} ${m['get_in']:>7,.0f}")
    else:
        print("No matches extracted")
