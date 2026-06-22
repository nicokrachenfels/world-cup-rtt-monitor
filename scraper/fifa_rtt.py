"""
Scrape collect.fifa.com/right-to-ticket for RTT listings and prices.
Returns a list of RTTListing dicts.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

FIFA_RTT_URL = "https://collect.fifa.com/right-to-ticket"
FIFA_LOGIN_URL = "https://collect.fifa.com/en/login"


@dataclass
class RTTListing:
    match_key: str          # e.g. "Brazil vs Germany - Los Angeles - Jul 4"
    home_team: str
    away_team: str
    venue: str
    match_date: str
    category: str           # "1", "2", "3", or "Unknown"
    price: float
    currency: str
    listing_id: str         # unique ID for this listing if available


async def _login_if_needed(page, email: str, password: str):
    """Log in to FIFA Collect if credentials are provided."""
    await page.goto(FIFA_LOGIN_URL, wait_until="networkidle", timeout=30_000)
    await page.fill('input[type="email"]', email)
    await page.fill('input[type="password"]', password)
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("networkidle", timeout=15_000)
    logger.info("FIFA login completed")


async def scrape_fifa_rtt(
    fifa_email: Optional[str] = None,
    fifa_password: Optional[str] = None,
) -> list[dict]:
    """
    Scrape the FIFA RTT marketplace. Returns a list of RTTListing dicts.
    Pass fifa_email/fifa_password if the site requires login to see prices.
    """
    listings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            if fifa_email and fifa_password:
                await _login_if_needed(page, fifa_email, fifa_password)

            logger.info("Loading FIFA RTT page...")
            await page.goto(FIFA_RTT_URL, wait_until="networkidle", timeout=45_000)

            # Wait for listing cards — try multiple selectors since the DOM may vary
            card_selectors = [
                "[data-testid='listing-card']",
                ".listing-card",
                ".rtt-card",
                "[class*='ListingCard']",
                "[class*='listing']",
                "article",
            ]

            loaded = False
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=10_000)
                    loaded = True
                    logger.info(f"Found cards with selector: {selector}")
                    break
                except PlaywrightTimeout:
                    continue

            if not loaded:
                # Try scrolling and waiting for any price element
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)
                logger.warning("Could not find card selector — attempting broad extraction")

            # Scroll through the page to load all listings (lazy loading)
            prev_height = 0
            for _ in range(10):
                height = await page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
                prev_height = height

            # Extract listing data from DOM
            raw_listings = await page.evaluate(_extract_listings_js())
            logger.info(f"Extracted {len(raw_listings)} raw listings from FIFA RTT page")

            for raw in raw_listings:
                listing = _parse_raw_listing(raw)
                if listing:
                    listings.append(asdict(listing))

        except Exception as e:
            logger.error(f"FIFA RTT scrape failed: {e}", exc_info=True)
        finally:
            await browser.close()

    return listings


def _extract_listings_js() -> str:
    """
    JavaScript to extract listing data from the page DOM.
    Tries multiple selector patterns to handle Next.js hydration variants.
    """
    return """
    () => {
        const results = [];

        // Try to find any elements containing price info
        const pricePatterns = ['$', '€', '£', 'USD', 'EUR'];

        // Look for structured card elements
        const cards = document.querySelectorAll(
            '[class*="card"], [class*="Card"], [class*="listing"], [class*="Listing"], article, [data-testid]'
        );

        cards.forEach(card => {
            const text = card.innerText || card.textContent || '';
            // Skip cards without a price signal
            if (!pricePatterns.some(p => text.includes(p))) return;
            // Skip nav/header/footer noise
            if (card.closest('nav, header, footer')) return;

            results.push({
                html: card.innerHTML,
                text: text.trim(),
                classes: card.className,
                testId: card.getAttribute('data-testid') || '',
            });
        });

        return results;
    }
    """


def _parse_raw_listing(raw: dict) -> Optional[RTTListing]:
    """
    Parse a raw DOM extraction into an RTTListing.

    Actual card format (newline-separated):
        25\nJUN\n🇪🇨\nEcuador\nvs.\n🇩🇪\nGermany\nM56 · Group Stage\nNew York/New Jersey · ...\nCAT 1\nBUY NOW FOR\nUS$1,299.00\n...
    """
    import re

    text = raw.get("text", "")
    if not text:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Price ────────────────────────────────────────────────────────────────
    # Format: "US$1,299.00" or "$600.00"
    price_match = re.search(r"US?\$([\d,]+(?:\.\d{2})?)", text)
    if not price_match:
        price_match = re.search(r"([\d,]+(?:\.\d{2})?)\s*USD", text)
    if not price_match:
        return None
    try:
        price = float(price_match.group(1).replace(",", ""))
    except ValueError:
        return None

    currency = "USD"

    # ── Category ─────────────────────────────────────────────────────────────
    cat_match = re.search(r"\bCAT\s*([123])\b|\b[Cc]ategor(?:y|ia)[:\s]*([123])\b", text)
    category = (cat_match.group(1) or cat_match.group(2)) if cat_match else "Unknown"

    # ── Team names ───────────────────────────────────────────────────────────
    # Card has: ...\n{Team1}\nvs.\n{Team2}\n...
    # Use the "vs." line as an anchor — teams are the non-emoji, non-date lines around it
    home_team = "TBD"
    away_team = "TBD"

    # Strip emoji/flag characters for matching
    def _is_team_line(line: str) -> bool:
        clean = re.sub(r'[^\x00-\x7F]', '', line).strip()  # remove non-ASCII (flags)
        return bool(clean) and not re.match(r'^\d', clean) and clean not in ('vs.', 'vs', 'v')

    for i, line in enumerate(lines):
        if line.lower() in ("vs.", "vs", "v."):
            # Search backwards for home team (skip flags/dates)
            for j in range(i - 1, max(i - 4, -1), -1):
                if _is_team_line(lines[j]):
                    home_team = re.sub(r'[^\x00-\x7F]', '', lines[j]).strip()
                    break
            # Search forwards for away team
            for j in range(i + 1, min(i + 4, len(lines))):
                if _is_team_line(lines[j]):
                    away_team = re.sub(r'[^\x00-\x7F]', '', lines[j]).strip()
                    break
            break

    # ── Date ─────────────────────────────────────────────────────────────────
    # Card starts with day number + month abbreviation on separate lines e.g. "25\nJUN"
    date_str = "Unknown"
    month_abbrs = ("JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC")
    for i, line in enumerate(lines):
        if line.upper() in month_abbrs and i > 0 and lines[i - 1].isdigit():
            date_str = f"{line.upper()} {lines[i - 1]}"
            break

    # ── Venue ────────────────────────────────────────────────────────────────
    # Format: "City · Stadium Name"  e.g. "New York/New Jersey · New York New Jersey Stadium"
    venue = "Unknown"
    for line in lines:
        if "·" in line and "$" not in line and "CAT" not in line.upper():
            venue = line.split("·")[0].strip()
            break

    # If venue still Unknown, try to extract match code (e.g. "M85" from "M85 · Round of 32")
    if venue == "Unknown":
        import re as _re2
        for line in lines:
            m = _re2.match(r'^(M\d+)\b', line.strip())
            if m:
                venue = m.group(1)
                break

    match_key = f"{home_team} vs {away_team} - {venue} - {date_str}"

    return RTTListing(
        match_key=match_key,
        home_team=home_team,
        away_team=away_team,
        venue=venue,
        match_date=date_str,
        category=category,
        price=price,
        currency=currency,
        listing_id=raw.get("testId", ""),
    )


def get_min_prices_by_match(listings: list[dict]) -> dict[str, dict]:
    """
    Collapse a list of listings into minimum price per (match_key, category).
    Returns: {match_key: {"min_price": float, "category": str, "teams": (home, away), ...}}
    """
    by_match: dict[str, dict] = {}

    for listing in listings:
        key = listing["match_key"]
        cat = listing["category"]
        composite_key = f"{key}||cat{cat}"

        if composite_key not in by_match or listing["price"] < by_match[composite_key]["min_price"]:
            by_match[composite_key] = {
                "match_key": key,
                "home_team": listing["home_team"],
                "away_team": listing["away_team"],
                "venue": listing["venue"],
                "match_date": listing["match_date"],
                "category": cat,
                "min_price": listing["price"],
                "currency": listing["currency"],
            }

    return by_match


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = asyncio.run(
        scrape_fifa_rtt(
            fifa_email=os.getenv("FIFA_EMAIL"),
            fifa_password=os.getenv("FIFA_PASSWORD"),
        )
    )
    mins = get_min_prices_by_match(results)
    for k, v in mins.items():
        print(f"{v['match_key']} | Cat {v['category']} | Min: ${v['min_price']:,.2f}")
