"""
Scrape BBC World Cup schedule page for 'As it stands' R32 knockout bracket projections.
Returns projected team pairs for Last 32 matches.
"""
import asyncio
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

BBC_URL = "https://www.bbc.com/sport/football/world-cup/schedule"

# Normalise common BBC team name variations to our canonical spellings
_BBC_ALIASES: dict[str, str] = {
    "united states": "usa",
    "usa": "usa",
    "czech republic": "czechia",
    "türkiye": "turkey",
    "turkiye": "turkey",
    "ivory coast": "ivory coast",
    "côte d'ivoire": "ivory coast",
    "cote d'ivoire": "ivory coast",
    "dr congo": "congo dr",
    "congo": "congo dr",
    "republic of ireland": "ireland",
    "ir iran": "iran",
    "bosnia & herzegovina": "bosnia",
    "bosnia-herzegovina": "bosnia",
    "cape verde islands": "cape verde",
    "south korea": "south korea",
    "korea republic": "south korea",
    "new zealand": "new zealand",
}


def _norm(name: str) -> str:
    n = re.sub(r'[^\x00-\x7F]', '', name).lower().strip()
    return _BBC_ALIASES.get(n, n)


def _parse_bbc_text(text: str) -> list[dict]:
    """
    Parse BBC schedule page text for R32 'As it stands' matchups.

    Actual BBC page structure (each element appears duplicated for accessibility):
        <DD>
        OF
        <JUN|JUL>
        As it stands   (× 2 — one label per team side)
        As it stands
        <Home team>    (× 2)
        <Home team>
        plays
        <Away team>    (× 2)
        <Away team>
        AT
        <HH:MM>        (× 2)
        <HH:MM>
        ON
        <MON|TUE|...>

    Strategy: anchor on "plays", look backward for home team, forward for away team.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [re.sub(r'[^\x00-\x7F]+', '', l).strip() for l in lines]
    lines = [l for l in lines if l]

    MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
    _JUNK = {
        "as it stands", "plays", "at", "on", "of", "scheduled", "(active)",
        "last 32", "last 16", "quarter-finals", "semi-finals", "final",
        "third place", "third-place play-off",
    }
    _skip = re.compile(
        r'^\d{1,2}:\d{2}$'
        r'|^(MON|TUE|WED|THU|FRI|SAT|SUN)$'
        r'|^\d{1,2}$'
        r'|^W[-.]'
        r'|^L[-.]'
    )

    def _is_junk(ln: str) -> bool:
        return _skip.match(ln) is not None or ln.lower() in _JUNK or ln.upper() in MONTHS

    results = []
    for i, line in enumerate(lines):
        if line.lower() != "plays":
            continue

        # Home team: nearest real team name scanning backward
        home = None
        for j in range(i - 1, max(i - 8, -1), -1):
            if not _is_junk(lines[j]):
                home = lines[j]
                break

        # Away team: first real team name scanning forward
        away = None
        for j in range(i + 1, min(i + 8, len(lines))):
            if not _is_junk(lines[j]):
                away = lines[j]
                break

        # Date: scan backward for month, then find day number via "DD OF MON" pattern
        date_str = ""
        for j in range(i - 1, max(i - 16, -1), -1):
            if lines[j].upper() in MONTHS:
                month = lines[j].upper()
                # Look for "OF" at j-1 and day digit at j-2
                if j >= 2 and lines[j - 1].upper() == "OF" and lines[j - 2].isdigit():
                    date_str = f"{month} {int(lines[j - 2])}"
                elif j >= 1 and lines[j - 1].isdigit():
                    date_str = f"{month} {int(lines[j - 1])}"
                break

        # Skip if teams look like bracket references (W-32-1 etc.)
        if not home or not away:
            continue
        if re.match(r'^W[-.]|^L[-.]', home) or re.match(r'^W[-.]|^L[-.]', away):
            continue
        if home == away:
            continue

        results.append({"home": home, "away": away, "date": date_str})

    return results


async def scrape_bbc_bracket() -> list[dict]:
    """
    Scrape BBC knockout stage for R32 'As it stands' projected matchups.
    Returns: [{"home": "Germany", "away": "Scotland", "date": "JUN 29"}, ...]
    Falls back to empty list on any error (caller degrades gracefully).
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        logger.warning("Playwright not installed — BBC bracket unavailable")
        return []

    results = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                locale="en-GB",
            )
            page = await ctx.new_page()

            try:
                logger.info("Loading BBC World Cup schedule...")
                await page.goto(BBC_URL, wait_until="networkidle", timeout=30_000)

                # Dismiss cookie/consent banner if present
                for btn_selector in [
                    '[data-testid="banner-accept"]',
                    'button:text("Accept")',
                    'button:text("Accept all")',
                    '#bbccookies-continue-button',
                ]:
                    try:
                        btn = page.locator(btn_selector)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=3_000)
                            await page.wait_for_load_state("networkidle", timeout=5_000)
                            break
                    except Exception:
                        pass

                # Click "Knockout Stage" tab — BBC uses text link
                await asyncio.sleep(2)
                for tab_sel in [
                    'a:text("Knockout Stage")',
                    'button:text("Knockout Stage")',
                    'a[href*="KnockoutStage"]',
                    '[data-testid*="knockout"]',
                ]:
                    try:
                        tab = page.locator(tab_sel)
                        if await tab.count() > 0:
                            await tab.first.click(timeout=5_000)
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        pass

                # Scroll to ensure all content loads
                for _ in range(4):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.2)

                raw_text = await page.evaluate("() => document.body.innerText")
                results = _parse_bbc_text(raw_text)
                logger.info(f"BBC bracket: {len(results)} R32 projections extracted")

            except Exception as e:
                logger.warning(f"BBC bracket page error: {e}")
            finally:
                await browser.close()

    except Exception as e:
        logger.warning(f"BBC bracket scrape failed: {e}")

    return results
