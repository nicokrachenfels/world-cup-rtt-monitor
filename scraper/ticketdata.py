"""
Scrape all World Cup match get-in prices from the TicketData performer page.
One request gets every match — no per-event config needed.
"""
import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

TICKETDATA_MATCHES_URL = "https://www.ticketdata.com/performer/world-cup-soccer?view=matches"


async def scrape_all_matches() -> dict[str, dict]:
    """
    Scrape the TicketData World Cup matches page.
    Returns: {normalized_match_key: {"get_in": float, "teams": str, "date": str, "venue": str}}
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-platform": '"macOS"',
            },
        )
        page = await context.new_page()
        await _apply_stealth(page)

        try:
            logger.info("Loading TicketData World Cup matches page...")
            await page.goto(TICKETDATA_MATCHES_URL, wait_until="domcontentloaded", timeout=30_000)

            # Wait longer for JS to fully render
            await asyncio.sleep(5)

            # Log page title and first 500 chars to diagnose blocks/redirects
            title = await page.title()
            body_preview = await page.evaluate("document.body.innerText.slice(0, 500)")
            logger.info(f"TicketData page title: {title!r}")
            logger.info(f"TicketData body preview: {body_preview!r}")

            # Scroll to trigger lazy loading of all match rows
            await _scroll_to_bottom(page)

            # Extract all match rows
            raw_matches = await page.evaluate(_extract_matches_js())
            logger.info(f"Extracted {len(raw_matches)} match rows from TicketData")

            matches: dict[str, dict] = {}
            for raw in raw_matches:
                parsed = _parse_match_row(raw)
                if parsed:
                    key = _normalize_key(parsed["teams"])
                    matches[key] = parsed

            return matches

        except PlaywrightTimeout:
            logger.error("Timeout loading TicketData matches page")
            return {}
        except Exception as e:
            logger.error(f"TicketData scrape failed: {e}", exc_info=True)
            return {}
        finally:
            await browser.close()


async def _scroll_to_bottom(page, iterations: int = 8, delay: float = 1.5) -> None:
    for _ in range(iterations):
        prev = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(delay)
        curr = await page.evaluate("document.body.scrollHeight")
        if curr == prev:
            break


def _extract_matches_js() -> str:
    """
    JS to extract match rows from the TicketData matches listing page.
    Tries broad selectors since the DOM structure may change.
    """
    return """
    () => {
        const results = [];

        // The matches page shows rows/cards with: team names, date, venue, get-in price
        // Try common row selectors
        const rowSelectors = [
            '[class*="event-row"]', '[class*="EventRow"]',
            '[class*="match-row"]', '[class*="MatchRow"]',
            '[class*="listing-row"]', '[class*="ListingRow"]',
            'tr[class*="event"]', '[data-testid*="event"]',
            '[class*="event-item"]', '[class*="EventItem"]',
            'li[class*="event"]', 'div[class*="event"]',
        ];

        let rows = [];
        for (const sel of rowSelectors) {
            const found = document.querySelectorAll(sel);
            if (found.length > 0) {
                rows = Array.from(found);
                break;
            }
        }

        // Fallback: find any element that has both a team-like name AND a price
        if (rows.length === 0) {
            const allDivs = document.querySelectorAll('div, li, tr, article');
            rows = Array.from(allDivs).filter(el => {
                const txt = el.innerText || '';
                return txt.includes('$') && txt.includes('vs') && txt.length < 500;
            });
        }

        rows.forEach(row => {
            const text = (row.innerText || row.textContent || '').trim();
            if (!text) return;
            results.push({
                text: text,
                html: row.innerHTML,
                classes: row.className,
            });
        });

        // Deduplicate by text content
        const seen = new Set();
        return results.filter(r => {
            if (seen.has(r.text)) return false;
            seen.add(r.text);
            return true;
        });
    }
    """


def _parse_match_row(raw: dict) -> Optional[dict]:
    """Parse a raw DOM row into structured match data."""
    text = raw.get("text", "")
    if not text or len(text) > 600:
        return None

    # Must have a price signal
    if "$" not in text:
        return None

    # Extract price — get-in price is typically the lowest/first price shown
    price_matches = re.findall(r"\$([\d,]+(?:\.\d{2})?)", text)
    if not price_matches:
        return None

    prices = []
    for p in price_matches:
        try:
            prices.append(float(p.replace(",", "")))
        except ValueError:
            continue

    # Filter to realistic ticket price range
    prices = [p for p in prices if 10 < p < 100_000]
    if not prices:
        return None

    get_in = min(prices)

    # Extract team names — look for "X vs Y" pattern
    vs_match = re.search(
        r"([A-Z][a-zA-Z\s\'\-\.]+?)\s+vs\.?\s+([A-Z][a-zA-Z\s\'\-\.]+?)(?:\s*[\|\-\n\r]|$)",
        text,
    )
    if vs_match:
        home = vs_match.group(1).strip()
        away = vs_match.group(2).strip()
        teams = f"{home} vs {away}"
    else:
        teams = "Unknown vs Unknown"

    # Extract date — look for month names or date patterns
    date_match = re.search(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*[\s,]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}",
        text, re.IGNORECASE,
    )
    if not date_match:
        date_match = re.search(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", text)
    match_date = date_match.group(0) if date_match else "Unknown"

    # Venue — heuristic: any line that isn't teams, date, or price
    venue = "Unknown"
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        if (
            "$" not in line
            and "vs" not in line.lower()
            and not re.search(r"\d{1,2}/\d{1,2}", line)
            and len(line) > 4
            and len(line) < 60
        ):
            venue = line
            break

    return {
        "teams": teams,
        "match_date": match_date,
        "venue": venue,
        "get_in": get_in,
    }


def _normalize_key(teams_str: str) -> str:
    """Lowercase, strip extra spaces for fuzzy matching."""
    return teams_str.lower().strip()


def find_get_in_price(
    home_team: str,
    away_team: str,
    td_matches: dict[str, dict],
) -> Optional[float]:
    """
    Look up the get-in price for a given FIFA match in the TicketData results.
    Uses fuzzy name matching since team name formatting differs between sites.
    """
    if home_team == "TBD" or away_team == "TBD":
        return None

    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)

    best_price: Optional[float] = None
    best_score = 0

    for key, match_data in td_matches.items():
        score = _match_score(home_norm, away_norm, key)
        if score > best_score:
            best_score = score
            best_price = match_data["get_in"]

    # Only return if we're reasonably confident it's the same match
    if best_score >= 2:
        return best_price

    logger.debug(f"No TicketData match found for {home_team} vs {away_team} (best score: {best_score})")
    return None


def _normalize_team(name: str) -> str:
    """Normalize team name for fuzzy comparison."""
    replacements = {
        "united states": "usa",
        "u.s.a.": "usa",
        "u.s.": "usa",
        "türkiye": "turkey",
        "turkiye": "turkey",
        "côte d'ivoire": "ivory coast",
        "cote d'ivoire": "ivory coast",
        "congo-kinshasa": "dr congo",
        "united arab emirates": "uae",
    }
    n = name.lower().strip()
    return replacements.get(n, n)


def _match_score(home_norm: str, away_norm: str, td_key: str) -> int:
    """
    Score how well (home_norm, away_norm) matches a TicketData key.
    Score 2 = both teams matched, 1 = one team matched, 0 = no match.
    """
    score = 0
    # td_key is like "brazil vs germany"
    td_parts = td_key.split(" vs ")
    if len(td_parts) != 2:
        return 0
    td_home, td_away = td_parts[0].strip(), td_parts[1].strip()

    # Check forward match (home=home, away=away)
    if home_norm in td_home or td_home in home_norm:
        score += 1
    if away_norm in td_away or td_away in away_norm:
        score += 1

    if score == 2:
        return score

    # Check reversed (some sites flip home/away)
    rev_score = 0
    if home_norm in td_away or td_away in home_norm:
        rev_score += 1
    if away_norm in td_home or td_home in away_norm:
        rev_score += 1

    return max(score, rev_score)


async def _apply_stealth(page) -> None:
    await page.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
    """)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matches = asyncio.run(scrape_all_matches())
    if matches:
        print(f"\n{'TEAMS':<40} {'DATE':<15} {'GET-IN':>8}")
        print("-" * 65)
        for key, m in sorted(matches.items()):
            print(f"{m['teams']:<40} {m['match_date']:<15} ${m['get_in']:>7,.0f}")
    else:
        print("No matches extracted — page may have blocked the request")
