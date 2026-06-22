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
    BBC renders each R32 card as:
        As it stands
        <Team1>
        <Team2>
        <DAY>  <HH:MM>  <DD> <MON>
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Strip non-ASCII (flag emojis etc.) from every line
    lines = [re.sub(r'[^\x00-\x7F]+', '', l).strip() for l in lines]
    lines = [l for l in lines if l]

    MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
    DAY_ABBRS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
    # Lines to skip when looking for team names
    _skip = re.compile(
        r'^\d{1,2}:\d{2}$'          # time "21:30"
        r'|^(MON|TUE|WED|THU|FRI|SAT|SUN)$'
        r'|^\d{1,2}$'               # bare day numbers
        r'|^Last \d+$'              # "Last 32"
        r'|^Scheduled$'
        r'|^W-\d+-\d+$'             # bracket ref labels like W-32-1
    )

    results = []
    i = 0
    while i < len(lines):
        if lines[i] == "As it stands":
            # Collect next two non-skippable lines as team names
            teams: list[str] = []
            j = i + 1
            while j < len(lines) and len(teams) < 2:
                ln = lines[j]
                if _skip.match(ln) or not ln or len(ln) < 2:
                    j += 1
                    continue
                teams.append(ln)
                j += 1

            # Find date: look for "DD JUN/JUL" pattern nearby
            date_str = ""
            k = j
            while k < min(j + 6, len(lines)):
                m = re.search(
                    r'(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)',
                    lines[k], re.I
                )
                if m:
                    day, month = int(m.group(1)), m.group(2).upper()
                    date_str = f"{month} {day}"
                    break
                k += 1

            if len(teams) == 2 and teams[0] and teams[1]:
                results.append({
                    "home": teams[0],
                    "away": teams[1],
                    "date": date_str,
                })
            i = j
        else:
            i += 1

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

                # Click "Knockout Stage" tab if present
                for tab_sel in [
                    'a[href*="KnockoutStage"]',
                    'button:text("Knockout Stage")',
                    '[data-testid*="knockout"]',
                ]:
                    try:
                        tab = page.locator(tab_sel)
                        if await tab.count() > 0:
                            await tab.first.click(timeout=5_000)
                            await page.wait_for_load_state("networkidle", timeout=10_000)
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
