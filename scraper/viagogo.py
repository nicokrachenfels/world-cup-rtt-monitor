"""
Scrape Viagogo for the cheapest ticket price on a listing page.
Uses Playwright (headless Chromium) since Viagogo renders via JavaScript.
"""
import logging
import re
from typing import Optional

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


async def scrape_viagogo_min_price(url: str, quantity: int = 2) -> Optional[float]:
    """
    Returns the cheapest per-ticket price for ≥quantity seats on a Viagogo listing page.
    Returns None if the page can't be scraped or no prices are found.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        # Skip images/fonts for faster load
        await page.route(
            re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|eot|mp4)(\?.*)?$"),
            lambda route: route.abort(),
        )

        try:
            await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            # Give React time to render listings
            await page.wait_for_timeout(6_000)
        except Exception as e:
            logger.warning(f"Viagogo page load failed: {e}")
            await browser.close()
            return None

        prices = await _extract_prices(page)
        await browser.close()

        if not prices:
            logger.warning("Viagogo: no prices found on page — may be bot-blocked or selectors need update")
            return None

        result = min(prices)
        logger.info(f"Viagogo min price: ${result:,.0f} ({len(prices)} price(s) found)")
        return result


async def _extract_prices(page) -> list[float]:
    # Strategy 1: price-like keys in embedded <script> JSON blobs
    try:
        scripts = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('script'))
                .map(s => s.textContent || '')
                .filter(t => t.includes('price') || t.includes('Price'))
        """)
        for text in scripts:
            prices = _parse_prices_from_json_blob(text)
            if prices:
                logger.debug(f"Script-tag prices: {sorted(prices)[:5]}")
                return prices
    except Exception as e:
        logger.debug(f"Script extraction failed: {e}")

    # Strategy 2: common Viagogo DOM selectors
    selectors = [
        "[data-testid*='price']",
        "[data-qa*='price']",
        "[class*='Price']",
        "[class*='price']",
        "[class*='ticket-price']",
        ".sc-price",
    ]
    for sel in selectors:
        try:
            texts = await page.evaluate(f"""() =>
                Array.from(document.querySelectorAll('{sel}'))
                    .map(el => el.textContent.trim())
                    .filter(t => t.includes('$') || /\\d{{3,}}/.test(t))
            """)
            prices = [p for t in texts for p in [_parse_usd(t)] if p]
            if prices:
                logger.debug(f"DOM selector '{sel}': {sorted(prices)[:5]}")
                return prices
        except Exception:
            continue

    # Strategy 3: full-page text scan (last resort)
    try:
        body = await page.inner_text("body")
        prices = [p for p in _scan_text_for_prices(body) if 200 < p < 50_000]
        if prices:
            logger.debug(f"Body scan prices: {sorted(prices)[:5]}")
            return prices
    except Exception as e:
        logger.debug(f"Body scan failed: {e}")

    return []


def _parse_prices_from_json_blob(text: str) -> list[float]:
    prices = []
    pattern = re.compile(
        r'"(?:price|pricePerTicket|listingPrice|ticketPrice|amount|Price)"'
        r'\s*:\s*([\d.]+)',
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        try:
            val = float(m.group(1))
            if 200 < val < 50_000:
                prices.append(val)
        except ValueError:
            pass
    return prices


def _parse_usd(text: str) -> Optional[float]:
    m = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _scan_text_for_prices(text: str) -> list[float]:
    prices = []
    for m in re.finditer(r'\$([\d,]+(?:\.\d{1,2})?)', text):
        try:
            prices.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return prices
