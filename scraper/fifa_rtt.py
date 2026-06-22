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
    This is deliberately lenient — the FIFA page DOM changes often.
    """
    import re

    text = raw.get("text", "")
    if not text:
        return None

    # Extract price — look for patterns like "$1,234", "1234 USD", "€999"
    price_match = re.search(r"[\$€£][\s]?([\d,]+(?:\.\d{2})?)", text)
    if not price_match:
        price_match = re.search(r"([\d,]+(?:\.\d{2})?)\s*(?:USD|EUR|GBP)", text)
    if not price_match:
        return None

    try:
        price = float(price_match.group(1).replace(",", ""))
    except ValueError:
        return None

    currency = "USD"
    if "€" in text:
        currency = "EUR"
    elif "£" in text:
        currency = "GBP"

    # Extract category
    cat_match = re.search(r"[Cc]ategor(?:y|ia)[:\s]*([123])", text)
    category = cat_match.group(1) if cat_match else "Unknown"

    # Try to extract team names — look for "vs" or "v." patterns
    vs_match = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s+(?:vs?\.?|–|-)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)", text)
    home_team = vs_match.group(1) if vs_match else "TBD"
    away_team = vs_match.group(2) if vs_match else "TBD"

    # Venue — look for city names after known patterns
    venue_match = re.search(r"(?:at|@|venue|stadium)[:\s]+([A-Z][^\n,]+)", text, re.IGNORECASE)
    venue = venue_match.group(1).strip() if venue_match else "Unknown"

    # Date — look for date-like strings
    date_match = re.search(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?",
        text,
        re.IGNORECASE,
    )
    match_date = date_match.group(0) if date_match else "Unknown"

    match_key = f"{home_team} vs {away_team} - {venue} - {match_date}"

    return RTTListing(
        match_key=match_key,
        home_team=home_team,
        away_team=away_team,
        venue=venue,
        match_date=match_date,
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
