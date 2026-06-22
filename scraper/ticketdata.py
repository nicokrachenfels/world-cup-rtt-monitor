"""
Scrape all World Cup match get-in prices from the TicketData internal API.
Uses cloudscraper to bypass Cloudflare bot protection.
API: https://data.ticketdata.com/api/search?performer_slug=world-cup-soccer
"""
import logging
import re
from datetime import datetime
from typing import Optional

import cloudscraper

logger = logging.getLogger(__name__)

TICKETDATA_API_URL = "https://data.ticketdata.com/api/search?performer_slug=world-cup-soccer"

# Maps various spellings of FIFA WC 2026 venue cities → canonical form for TBD matching
_CITY_ALIASES: dict[str, str] = {
    # MetLife Stadium (East Rutherford, NJ)
    "new york/new jersey": "metlife",
    "new york": "metlife",
    "new jersey": "metlife",
    "east rutherford": "metlife",
    # SoFi Stadium (Inglewood, CA)
    "los angeles": "la",
    "inglewood": "la",
    # Hard Rock Stadium (Miami Gardens, FL)
    "miami": "miami",
    "miami gardens": "miami",
    # AT&T Stadium (Arlington, TX)
    "dallas": "dallas",
    "arlington": "dallas",
    # Gillette Stadium (Foxborough, MA)
    "boston/new england": "foxborough",
    "foxborough": "foxborough",
    "boston": "foxborough",
    # Levi's Stadium (Santa Clara, CA)
    "san francisco bay area": "santa clara",
    "santa clara": "santa clara",
    "san francisco": "santa clara",
}


def _normalize_city(city: str) -> str:
    c = city.lower().strip()
    return _CITY_ALIASES.get(c, c)


def _parse_rtt_date(date_str: str) -> tuple[int, int]:
    """Parse RTT date like "JUL 4" → (month=7, day=4)."""
    try:
        dt = datetime.strptime(date_str.strip(), "%b %d")
        return dt.month, dt.day
    except ValueError:
        return 0, 0


def _parse_td_date(date_str: str) -> tuple[int, int]:
    """Parse TicketData date like "2026-07-04" → (month=7, day=4)."""
    try:
        parts = date_str.split("-")
        return int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return 0, 0


_TEAM_ALIASES: dict[str, str] = {
    "united states": "usa",
    "united states of america": "usa",
    "u.s.a.": "usa",
    "u.s.": "usa",
    "türkiye": "turkey",
    "turkiye": "turkey",
    "côte d'ivoire": "ivory coast",
    "cote d'ivoire": "ivory coast",
    "cote divoire": "ivory coast",
    "drc": "congo dr",
    "dr congo": "congo dr",
    "republic of ireland": "ireland",
    "ir iran": "iran",
    "bosnia-herzegovina": "bosnia",
    "bosnia & herzegovina": "bosnia",
    "cabo verde": "cape verde",
    "czech republic": "czechia",
}


def _normalize_team(name: str) -> str:
    n = name.lower().strip()
    return _TEAM_ALIASES.get(n, n)


def _parse_title(title: str) -> tuple[str, str, Optional[str]]:
    """
    Parse a TicketData event title into (home_team, away_team, match_code).
    Format: "[M43] Argentina v Austria (Group J - World Cup)"
    Returns match_code like "M43", or None if not present.
    """
    code_match = re.match(r"^\[M(\d+)\]", title)
    match_code = f"M{code_match.group(1)}" if code_match else None
    clean = re.sub(r"^\[M\d+\]\s*", "", title)
    clean = re.sub(r"\s*\([^)]+\)\s*$", "", clean).strip()
    parts = re.split(r"\s+v\s+", clean, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), match_code
    return clean, "TBD", match_code


def scrape_all_matches() -> dict[str, dict]:
    """
    Fetch World Cup events from TicketData's internal API.
    Returns: {normalized_match_key: {"get_in": float, "teams": str, "date": str, "venue": str}}
    The match key is "{home_normalized} v {away_normalized}".
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin", "mobile": False}
    )

    try:
        logger.info("Fetching TicketData World Cup events from API...")
        resp = scraper.get(TICKETDATA_API_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"TicketData API fetch failed: {e}")
        return {}

    try:
        events = data["data"]["events"]["upcoming"]
    except (KeyError, TypeError) as e:
        logger.error(f"Unexpected TicketData API response structure: {e}")
        return {}

    matches: dict[str, dict] = {}
    for event in events:
        get_in = event.get("get_in_price")
        if not get_in:
            continue

        title = event.get("title", "")
        home, away, match_code = _parse_title(title)
        home_norm = _normalize_team(home)
        away_norm = _normalize_team(away)
        key = f"{home_norm} v {away_norm}"

        # Parse date to YYYY-MM-DD
        raw_date = event.get("event_datetime", "")
        match_date = raw_date[:10] if raw_date else event.get("date", "Unknown")[:10]

        matches[key] = {
            "teams": f"{home} v {away}",
            "home_team": home,
            "away_team": away,
            "match_date": match_date,
            "venue": event.get("venue", "Unknown"),
            "city": event.get("city", ""),
            "get_in": float(get_in),
            "event_id": event.get("id"),
            "match_code": match_code,
        }
        logger.debug(f"  {home} v {away} | ${get_in:,} get-in | {match_date}")

    logger.info(f"TicketData API returned {len(matches)} World Cup events")
    return matches


def find_get_in_price(
    home_team: str,
    away_team: str,
    td_matches: dict[str, dict],
    venue: Optional[str] = None,
    date_str: Optional[str] = None,
) -> Optional[float]:
    """
    Fuzzy-match a FIFA match to a TicketData entry and return the get-in price.
    For TBD matches, falls back to city+date matching using venue and date_str.
    """
    if not td_matches:
        return None

    # TBD teams: match by match code (venue="M82") or by city+date
    if home_team == "TBD" or away_team == "TBD":
        if not venue:
            return None
        # Primary: venue is a match code like "M82"
        if re.match(r"^M\d+$", venue.strip()):
            for match_data in td_matches.values():
                if match_data.get("match_code") == venue.strip():
                    logger.debug(f"TBD match code {venue} → ${match_data['get_in']:,}")
                    return match_data["get_in"]
        # Fallback: city + date matching
        if date_str:
            city_norm = _normalize_city(venue)
            rtt_month, rtt_day = _parse_rtt_date(date_str)
            if rtt_month > 0:
                for match_data in td_matches.values():
                    td_city = _normalize_city(match_data.get("city", ""))
                    td_month, td_day = _parse_td_date(match_data.get("match_date", ""))
                    if td_city == city_norm and td_month == rtt_month and td_day == rtt_day:
                        logger.debug(f"TBD city+date {venue}/{date_str} → ${match_data['get_in']:,}")
                        return match_data["get_in"]
        logger.debug(f"No TBD match for venue={venue!r} date={date_str!r}")
        return None

    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)

    best_price: Optional[float] = None
    best_score = 0

    for key, match_data in td_matches.items():
        td_home = _normalize_team(match_data.get("home_team", ""))
        td_away = _normalize_team(match_data.get("away_team", ""))

        def _match_score(a: str, b: str) -> int:
            return 1 if (a in b or b in a) and min(len(a), len(b)) >= 3 else 0

        score = _match_score(home_norm, td_home) + _match_score(away_norm, td_away)
        score_rev = _match_score(home_norm, td_away) + _match_score(away_norm, td_home)
        score = max(score, score_rev)

        if score > best_score:
            best_score = score
            best_price = match_data["get_in"]

    if best_score >= 2:
        logger.debug(f"Matched {home_team} vs {away_team} → ${best_price:,} (score {best_score})")
        return best_price

    logger.debug(f"No TicketData match for {home_team} vs {away_team} (best score: {best_score})")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matches = scrape_all_matches()
    if matches:
        print(f"\n{'TEAMS':<45} {'DATE':<12} {'GET-IN':>8}")
        print("-" * 68)
        for key, m in sorted(matches.items(), key=lambda x: x[1]["match_date"]):
            print(f"{m['teams']:<45} {m['match_date']:<12} ${m['get_in']:>7,.0f}")
    else:
        print("No matches extracted")
