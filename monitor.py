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

from scraper.fifa_rtt import scrape_fifa_rtt, get_min_prices_by_match
from scraper.ticketdata import scrape_all_matches, find_get_in_price
from scraper.standings import fetch_standings, is_match_worth_monitoring
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


def _get_env(key: str, required: bool = True) -> Optional[str]:
    val = os.environ.get(key)
    if not val and required:
        logger.error(f"Missing required environment variable: {key}")
        sys.exit(1)
    return val


async def run(dry_run: bool = False, force_alert: bool = False) -> None:
    cfg = load_config()
    state = load_state()

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

    # ── 3. Filter by team status ─────────────────────────────────────────
    filtered_mins = {}
    for composite_key, listing in rtt_mins.items():
        should_monitor, reason = is_match_worth_monitoring(
            listing["home_team"], listing["away_team"], statuses
        )
        if should_monitor:
            filtered_mins[composite_key] = listing
        else:
            logger.info(f"Skipping {listing['match_key']}: {reason}")

    logger.info(f"Active listings after team filter: {len(filtered_mins)}")

    # ── 4. Scrape all TicketData match prices at once ─────────────────────
    logger.info("Scraping TicketData World Cup matches page...")
    td_matches = await scrape_all_matches()
    logger.info(f"TicketData returned {len(td_matches)} matches")

    # ── 6. Evaluate profitability ─────────────────────────────────────────
    threshold = cfg["profit_threshold"]
    triggered: list[dict] = []
    all_profitable: list[dict] = []

    for composite_key, listing in filtered_mins.items():
        rtt_price = listing["min_price"]
        get_in = find_get_in_price(listing["home_team"], listing["away_team"], td_matches)

        if get_in is None:
            logger.debug(f"No get-in price for {listing['match_key']} — skipping profit calc")
            continue

        seller_net, margin = compute_profit(rtt_price, get_in, cfg)

        result = {
            "match_key": listing["match_key"],
            "home_team": listing["home_team"],
            "away_team": listing["away_team"],
            "venue": listing["venue"],
            "match_date": listing["match_date"],
            "category": listing["category"],
            "rtt_price": rtt_price,
            "get_in_price": get_in,
            "seller_net": seller_net,
            "profit_margin": margin,
        }

        if margin >= threshold:
            all_profitable.append(result)

            # Check if this is a new price low for this match+category
            prev_min = state.get(composite_key, {}).get("min_price", float("inf"))
            is_new_low = rtt_price < prev_min

            if is_new_low or force_alert:
                triggered.append(result)
                logger.info(
                    f"ALERT: {listing['match_key']} Cat {listing['category']} | "
                    f"RTT ${rtt_price:,.0f} | Get-in ${get_in:,.0f} | "
                    f"Margin {margin:.1%} (new low: ${prev_min:,.0f} → ${rtt_price:,.0f})"
                )
            else:
                logger.info(
                    f"Profitable but not new low: {listing['match_key']} {margin:.1%}"
                )

        # Update state regardless
        state[composite_key] = {
            "min_price": rtt_price,
            "get_in_price": get_in,
            "margin": margin,
            "match_key": listing["match_key"],
        }

    # ── 7. Log summary ────────────────────────────────────────────────────
    logger.info(
        f"Summary: {len(all_profitable)} profitable matches, "
        f"{len(triggered)} alert(s) triggered"
    )

    if dry_run:
        logger.info("DRY RUN — no email sent")
        _print_summary(all_profitable, triggered)
    elif triggered or force_alert:
        sendgrid_key = _get_env("SENDGRID_API_KEY")
        from_email = _get_env("ALERT_FROM_EMAIL")
        to_email = cfg.get("alert_email") or _get_env("ALERT_EMAIL")

        send_alert(
            sendgrid_api_key=sendgrid_key,
            from_email=from_email,
            to_email=to_email,
            triggered_listings=triggered,
            all_profitable_listings=all_profitable,
        )
    else:
        logger.info("No alerts triggered this run")

    # ── 8. Persist state ──────────────────────────────────────────────────
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
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run, force_alert=args.force_alert))
