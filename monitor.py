"""
Main orchestration for the World Cup RTT arbitrage monitor.

Usage:
    python monitor.py              # normal run
    python monitor.py --dry-run    # print results, no email
    python monitor.py --force-alert  # send email even if no new lows
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import re

from scraper.fifa_rtt import scrape_fifa_rtt, get_min_prices_by_match
from scraper.ticketdata import scrape_all_matches, find_get_in_price
from scraper.standings import fetch_standings, is_deadwood_match
from alerts.email import send_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("monitor")

CONFIG_PATH = Path(__file__).parent / "config.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def compute_profit(rtt_price: float, get_in_price: float, cfg: dict) -> tuple[float, float]:
    """
    Returns (seller_net, profit_margin).
    seller_net = get_in / (1 + buyer_fee_markup) * (1 - seller_fee)
    profit_margin = (seller_net - rtt_price) / rtt_price
    """
    seller_net = get_in_price / (1 + cfg["buyer_fee_markup"]) * (1 - cfg["seller_fee"])
    margin = (seller_net - rtt_price) / rtt_price
    return seller_net, margin


def _round_name(match_code: str) -> str:
    try:
        n = int(match_code[1:])
    except (ValueError, IndexError):
        return ""
    if n <= 72:  return ""
    if n <= 88:  return "Round of 32"
    if n <= 96:  return "Round of 16"
    if n <= 100: return "Quarter-final"
    if n <= 102: return "Semi-final"
    if n == 103: return "3rd Place"
    if n == 104: return "Final"
    return "Knockout"


def _build_match_label(listing: dict, td_matches: dict) -> str:
    """
    Build a human-readable match label for use in email alerts.
    Falls back gracefully from full team names → round+code → generic.
    """
    home = listing.get("home_team", "TBD")
    away = listing.get("away_team", "TBD")
    venue = listing.get("venue", "")
    date = listing.get("match_date", "")

    if home != "TBD" and away != "TBD":
        return f"{home} vs {away}"

    # TBD match — try to resolve via match code
    if re.match(r'^M\d+$', venue or ""):
        td_m = next((m for m in td_matches.values() if m.get("match_code") == venue), None)
        if td_m:
            td_home = td_m.get("home_team", "TBD")
            td_away = td_m.get("away_team", "TBD")
            round_label = _round_name(venue)
            if td_home != "TBD" and td_away != "TBD":
                suffix = f" ({td_home} vs {td_away})" if round_label else f"{td_home} vs {td_away}"
                return f"{round_label}{suffix}" if round_label else suffix
        round_label = _round_name(venue)
        return f"{round_label} ({venue})" if round_label else venue

    # Try city + date lookup in td_matches
    if venue and venue != "Unknown" and date and date != "Unknown":
        try:
            from scraper.ticketdata import _normalize_city, _parse_rtt_date, _parse_td_date
            city_norm = _normalize_city(venue)
            rtt_month, rtt_day = _parse_rtt_date(date)
            if rtt_month > 0:
                for td_m in td_matches.values():
                    td_city = _normalize_city(td_m.get("city", ""))
                    td_month, td_day = _parse_td_date(td_m.get("match_date", ""))
                    if td_city == city_norm and td_month == rtt_month and td_day == rtt_day:
                        mc = td_m.get("match_code", "")
                        round_label = _round_name(mc) if mc else ""
                        td_home = td_m.get("home_team", "TBD")
                        td_away = td_m.get("away_team", "TBD")
                        if td_home != "TBD" and td_away != "TBD":
                            return f"{round_label} ({td_home} vs {td_away})" if round_label else f"{td_home} vs {td_away}"
                        elif mc:
                            return f"{round_label} ({mc})" if round_label else mc
        except Exception:
            pass

    # Last resort: use whatever venue info we have
    if venue and venue != "Unknown":
        round_label = _round_name(venue) if re.match(r'^M\d+$', venue) else ""
        return f"{round_label} ({venue})" if round_label else f"Match at {venue}"
    if date and date != "Unknown":
        return f"Knockout Match ({date})"
    return listing.get("match_key", "Knockout Match")


def _get_env(key: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(key)
    if not val and required:
        logger.error(f"Missing required environment variable: {key}")
        sys.exit(1)
    return val


async def run(dry_run: bool = False, force_alert: bool = False, test_email: bool = False) -> None:
    cfg = load_config()
    state = load_state()

    # ── 0. Test-email short-circuit ───────────────────────────────────────
    if test_email:
        sendgrid_key = _get_env("SENDGRID_API_KEY")
        from_email = _get_env("ALERT_FROM_EMAIL")
        to_email = cfg.get("alert_email") or _get_env("ALERT_EMAIL")
        send_alert(
            sendgrid_api_key=sendgrid_key,
            from_email=from_email,
            to_email=to_email,
            triggered_listings=[{
                "match_key": "TEST — credential check",
                "category": "N/A",
                "rtt_price": 0,
                "get_in_price": 0,
                "seller_net": 0,
                "profit_margin": 0,
                "profit_dollars": 0,
            }],
            all_profitable_listings=[],
        )
        logger.info("Test email sent successfully")
        return

    # ── 1. Fetch standings ────────────────────────────────────────────────
    logger.info("Fetching World Cup standings...")
    statuses = fetch_standings()
    if statuses:
        eliminated = [t for t, s in statuses.items() if s["eliminated"]]
        clinched = [t for t, s in statuses.items() if s["clinched_first"]]
        logger.info(f"Eliminated: {eliminated}")
        logger.info(f"Clinched 1st: {clinched}")
    else:
        logger.warning("Could not fetch standings — proceeding without team filtering")

    # ── 2. Scrape FIFA RTT page ───────────────────────────────────────────
    logger.info("Scraping FIFA RTT marketplace...")
    fifa_listings = await scrape_fifa_rtt(
        fifa_email=os.getenv("FIFA_EMAIL"),
        fifa_password=os.getenv("FIFA_PASSWORD"),
    )
    logger.info(f"Found {len(fifa_listings)} RTT listings")

    rtt_mins = get_min_prices_by_match(fifa_listings)
    logger.info(f"Unique match+category combinations: {len(rtt_mins)}")
    for k, v in rtt_mins.items():
        logger.info(f"  RTT: {v['match_key']} | Cat {v['category']} | ${v['min_price']:,.0f}")

    # ── 3. All listings included; deadwood flag is informational only ────
    filtered_mins = rtt_mins
    logger.info(f"Active listings: {len(filtered_mins)}")

    # ── 4. Scrape all TicketData match prices at once (sync, uses cloudscraper)
    logger.info("Scraping TicketData World Cup matches page...")
    td_matches = scrape_all_matches()
    logger.info(f"TicketData returned {len(td_matches)} matches")

    # ── 6. Detect removed/sold listings ──────────────────────────────────
    prev_keys = {k for k in state if not k.startswith("_")}
    current_rtt_keys = set(filtered_mins.keys())
    removed_keys = prev_keys - current_rtt_keys
    removed_listings = [
        {
            "match_key": state[k].get("match_label", state[k]["match_key"]),
            "category": state[k].get("category", "?"),
            "last_price": state[k]["min_price"],
        }
        for k in removed_keys
        if state[k].get("match_key")
    ]
    if removed_listings:
        logger.info(f"Removed from marketplace: {[r['match_key'] for r in removed_listings]}")

    # ── 7. Evaluate profitability ─────────────────────────────────────────
    threshold = cfg["profit_threshold"]
    triggered: list[dict] = []
    all_profitable: list[dict] = []
    new_listings: list[dict] = []  # brand-new RTTs that aren't yet profitable

    dollar_threshold = cfg.get("profit_dollar_threshold", 300)

    for composite_key, listing in filtered_mins.items():
        is_brand_new = composite_key not in prev_keys
        rtt_price = listing["min_price"]
        get_in = find_get_in_price(
            listing["home_team"], listing["away_team"], td_matches,
            venue=listing.get("venue"), date_str=listing.get("match_date"),
        )

        if get_in is None:
            logger.debug(f"No get-in price for {listing['match_key']} — skipping profit calc")
            if is_brand_new:
                new_listings.append({
                    "match_key": _build_match_label(listing, td_matches),
                    "category": listing["category"],
                    "rtt_price": rtt_price,
                    "get_in_price": None,
                    "profit_margin": None,
                })
            continue

        seller_net, margin = compute_profit(rtt_price, get_in, cfg)
        profit_dollars = seller_net - rtt_price
        deadwood = is_deadwood_match(listing["home_team"], listing["away_team"], statuses)

        result = {
            "match_key": _build_match_label(listing, td_matches),
            "_raw_match_key": listing["match_key"],
            "home_team": listing["home_team"],
            "away_team": listing["away_team"],
            "venue": listing["venue"],
            "match_date": listing["match_date"],
            "category": listing["category"],
            "rtt_price": rtt_price,
            "get_in_price": get_in,
            "seller_net": seller_net,
            "profit_margin": margin,
            "profit_dollars": profit_dollars,
            "deadwood": deadwood,
        }

        if margin >= threshold or profit_dollars >= dollar_threshold:
            all_profitable.append(result)

            # Check if this is a new price low for this match+category
            prev_min = state.get(composite_key, {}).get("min_price", float("inf"))
            is_new_low = rtt_price < prev_min

            if is_new_low or force_alert:
                triggered.append(result)
                logger.info(
                    f"ALERT: {result['match_key']} Cat {listing['category']} | "
                    f"RTT ${rtt_price:,.0f} | Get-in ${get_in:,.0f} | "
                    f"Margin {margin:.1%} (new low: ${prev_min:,.0f} → ${rtt_price:,.0f})"
                )
            else:
                logger.info(
                    f"Profitable but not new low: {listing['match_key']} {margin:.1%}"
                )
        elif is_brand_new:
            # New listing that isn't profitable yet — track supply movement
            new_listings.append({
                "match_key": _build_match_label(listing, td_matches),
                "category": listing["category"],
                "rtt_price": rtt_price,
                "get_in_price": get_in,
                "profit_margin": margin,
            })
            logger.info(
                f"New listing (not profitable): {listing['match_key']} Cat {listing['category']} | "
                f"RTT ${rtt_price:,.0f} | Margin {margin:.1%}"
            )

        # Update state — raw match_key for stability, match_label for display
        state[composite_key] = {
            "min_price": rtt_price,
            "get_in_price": get_in,
            "margin": margin,
            "match_key": listing["match_key"],
            "match_label": _build_match_label(listing, td_matches),
            "category": listing["category"],
        }


    # ── 8. Log summary ────────────────────────────────────────────────────
    logger.info(
        f"Summary: {len(all_profitable)} profitable matches, "
        f"{len(triggered)} alert(s) triggered, "
        f"{len(new_listings)} new non-profitable listings, "
        f"{len(removed_listings)} removed from marketplace"
    )

    if dry_run:
        logger.info("DRY RUN — no email sent")
        _print_summary(all_profitable, triggered)
    elif triggered or removed_listings or new_listings or force_alert:
        sendgrid_key = _get_env("SENDGRID_API_KEY")
        from_email = _get_env("ALERT_FROM_EMAIL")
        to_email = cfg.get("alert_email") or _get_env("ALERT_EMAIL")

        try:
            send_alert(
                sendgrid_api_key=sendgrid_key,
                from_email=from_email,
                to_email=to_email,
                triggered_listings=triggered,
                all_profitable_listings=all_profitable,
                removed_listings=removed_listings,
                new_listings=new_listings,
            )
        except Exception as e:
            logger.error(f"Email send failed (state still saved): {e}")
    else:
        logger.info("No alerts triggered this run")

    # Remove sold/delisted listings from state so they don't reappear as "removed" next run
    for k in removed_keys:
        state.pop(k, None)

    # ── 9. Persist state ──────────────────────────────────────────────────
    save_state(state)
    logger.info("State saved")



def _print_summary(all_profitable: list[dict], triggered: list[dict]) -> None:
    if not all_profitable:
        print("\nNo profitable matches found.")
        return

    print(f"\n{'='*70}")
    print(f"{'MATCH':<35} {'CAT':>3} {'RTT':>8} {'GET-IN':>8} {'NET':>8} {'MARGIN':>7}")
    print(f"{'='*70}")
    for t in sorted(all_profitable, key=lambda x: -x["profit_margin"]):
        flag = " <<< NEW" if t in triggered else ""
        print(
            f"{t['match_key'][:34]:<35} {t['category']:>3} "
            f"${t['rtt_price']:>7,.0f} ${t['get_in_price']:>7,.0f} "
            f"${t['seller_net']:>7,.0f} {t['profit_margin']:>6.1%}{flag}"
        )
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RTT arbitrage monitor")
    parser.add_argument("--dry-run", action="store_true", help="Print results without sending email")
    parser.add_argument("--force-alert", action="store_true", help="Send alert even if no new price low")
    parser.add_argument("--test-email", action="store_true", help="Send a test email to verify SendGrid credentials, then exit")
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run, force_alert=args.force_alert, test_email=args.test_email))
