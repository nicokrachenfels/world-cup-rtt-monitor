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

import datetime
import re

from scraper.fifa_rtt import scrape_fifa_rtt, get_min_prices_by_match
from scraper.viagogo import scrape_viagogo_min_price
from scraper.ticketdata import scrape_all_matches, find_get_in_price, find_td_teams, find_td_inventory
from scraper.standings import fetch_standings, is_deadwood_match, get_group_rankings
from scraper.bbc_bracket import scrape_bbc_bracket
from alerts.email import send_alert
from analyze import _team_label, _resolve_group_code, _round_name, apply_bbc_projections

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


def _was_profitable(entry: dict, margin_threshold: float, dollar_threshold: float) -> bool:
    margin = entry.get("margin")
    if margin is None:
        return False
    if margin >= margin_threshold:
        return True
    gip = entry.get("get_in_price")
    rtt = entry.get("min_price", float("inf"))
    if gip:
        return (gip * 0.725 - rtt) >= dollar_threshold
    return False


def compute_profit(rtt_price: float, get_in_price: float, cfg: dict) -> tuple[float, float]:
    """
    Returns (seller_net, profit_margin).
    seller_net = get_in / (1 + buyer_fee_markup) * (1 - seller_fee)
    profit_margin = (seller_net - rtt_price) / rtt_price
    """
    seller_net = get_in_price / (1 + cfg["buyer_fee_markup"]) * (1 - cfg["seller_fee"])
    margin = (seller_net - rtt_price) / rtt_price
    return seller_net, margin


def _build_match_label(
    listing: dict,
    td_matches: dict,
    rankings: dict,
    statuses: dict,
    bbc_proj: dict,
) -> str:
    home = listing.get("home_team", "TBD")
    away = listing.get("away_team", "TBD")
    if home != "TBD" and away != "TBD":
        return f"{home} vs {away}"

    venue_code = listing.get("venue", "")
    round_label = _round_name(venue_code)
    _td_m = next((m for m in td_matches.values() if m.get("match_code") == venue_code), None)
    _h_code = _td_m.get("home_team", "TBD").strip("() ") if _td_m else "TBD"
    _a_code = _td_m.get("away_team", "TBD").strip("() ") if _td_m else "TBD"
    if re.match(r'^World Cup Match\b', _h_code, re.I): _h_code = "TBD"
    if re.match(r'^World Cup Match\b', _a_code, re.I): _a_code = "TBD"

    def _fmt(name, raw):
        return _team_label(name, raw, statuses, rankings)

    # 1. BBC bracket projection
    bbc_teams = bbc_proj.get(venue_code)
    if bbc_teams:
        h_bbc, a_bbc = bbc_teams
        teams_str = f"{_fmt(h_bbc, _h_code)} vs {_fmt(a_bbc, _a_code)}"
        return f"{round_label}: {teams_str}" if round_label else teams_str

    # 2. TicketData both teams resolved
    td_teams = find_td_teams(venue_code, td_matches) if venue_code else None
    if td_teams:
        h, a = td_teams
        h_proj = _resolve_group_code(h, rankings)
        a_proj = _resolve_group_code(a, rankings)
        if h_proj and a_proj:
            teams_str = f"{_fmt(h_proj, h)} vs {_fmt(a_proj, a)}"
        else:
            teams_str = f"{_fmt(h.strip('() '), h)} vs {_fmt(a.strip('() '), a)}"
        return f"{round_label}: {teams_str}" if round_label else teams_str

    # 3. Partial resolution
    if _td_m and (_h_code != "TBD" or _a_code != "TBD"):
        h_resolved = _resolve_group_code(_h_code, rankings) if _h_code != "TBD" else None
        a_resolved = _resolve_group_code(_a_code, rankings) if _a_code != "TBD" else None
        h_name = h_resolved or (_h_code if _h_code != "TBD" else None)
        a_name = a_resolved or (_a_code if _a_code != "TBD" else None)
        h_str = _fmt(h_name, _h_code) if h_name else "TBD"
        a_str = _fmt(a_name, _a_code) if a_name else "TBD"
        teams_str = f"{h_str} vs {a_str}"
        return f"{round_label}: {teams_str}" if round_label else teams_str

    # 4. Fallback
    return f"{round_label}: {venue_code}" if (round_label and venue_code) else (round_label or venue_code or listing.get("match_key", "Knockout Match"))


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
    rankings = get_group_rankings(statuses) if statuses else {}

    # ── 2. Scrape FIFA RTT page + BBC bracket concurrently ───────────────
    logger.info("Scraping FIFA RTT marketplace and BBC bracket...")
    bbc_pairs, fifa_listings = await asyncio.gather(
        scrape_bbc_bracket(),
        scrape_fifa_rtt(
            fifa_email=os.getenv("FIFA_EMAIL"),
            fifa_password=os.getenv("FIFA_PASSWORD"),
        ),
    )
    logger.info(f"Found {len(fifa_listings)} RTT listings, {len(bbc_pairs)} BBC projections")

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
    bbc_proj = apply_bbc_projections(td_matches, bbc_pairs, rankings)

    # ── 5. Viagogo personal ticket watch ─────────────────────────────────────
    viagogo_url = cfg.get("viagogo_url")
    vg_threshold = cfg.get("viagogo_price_threshold", 2600)
    viagogo_drops: list[dict] = []

    if viagogo_url:
        logger.info("Checking Viagogo listing for Match 95 (Argentina)...")
        vg_price = await scrape_viagogo_min_price(viagogo_url)
        if vg_price is not None:
            vg_state = state.get("_viagogo_m95", {})
            last_alert = vg_state.get("last_alert_price")
            is_new_low = last_alert is None or vg_price < last_alert

            if vg_price < vg_threshold and is_new_low:
                viagogo_drops.append({
                    "match_name": "W86 vs W88 – Match 95 (Jul 7, Atlanta)",
                    "current_price": vg_price,
                    "previous_price": last_alert,
                    "threshold": vg_threshold,
                    "url": viagogo_url,
                })
                state["_viagogo_m95"] = {
                    **vg_state,
                    "last_alert_price": vg_price,
                    "min_price": min(vg_state.get("min_price", vg_price), vg_price),
                }
                logger.info(
                    f"ARGENTINA ALERT: ${vg_price:,.0f} < threshold ${vg_threshold:,.0f}"
                    + (f" (prev alert ${last_alert:,.0f})" if last_alert else " (first alert)")
                )
            else:
                prev_min = vg_state.get("min_price", vg_price)
                state["_viagogo_m95"] = {**vg_state, "min_price": min(prev_min, vg_price)}
                logger.info(
                    f"Viagogo M95: ${vg_price:,.0f} "
                    + (f"(above threshold ${vg_threshold:,.0f})" if vg_price >= vg_threshold
                       else f"(no new low — last alert ${last_alert:,.0f})")
                )

    # ── 6. Detect removed/sold listings ──────────────────────────────────
    prev_keys = {k for k in state if not k.startswith("_")}
    current_rtt_keys = set(filtered_mins.keys())

    # Sanity guard: if the scraper returned near-empty results while state has many
    # entries, assume a partial scrape and skip removed/new detection to prevent
    # oscillation spam (all listings cycle "removed → new → removed" every run).
    scrape_suspect = len(prev_keys) > 5 and len(current_rtt_keys) < 3
    if scrape_suspect:
        logger.warning(
            f"Scrape returned only {len(current_rtt_keys)} listings vs {len(prev_keys)} in state "
            "— skipping removed/new detection (likely partial scrape result)"
        )

    removed_keys = (prev_keys - current_rtt_keys) if not scrape_suspect else set()
    _margin_thresh = cfg["profit_threshold"]
    _dollar_thresh = cfg.get("profit_dollar_threshold", 300)

    # Only report removals for listings that were previously profitable —
    # non-profitable listings appear/disappear constantly and are just noise.
    removed_listings = [
        {
            "match_key": state[k].get("match_label", state[k]["match_key"]),
            "category": state[k].get("category", "?"),
            "last_price": state[k]["min_price"],
        }
        for k in removed_keys
        if state[k].get("match_key") and _was_profitable(state[k], _margin_thresh, _dollar_thresh)
    ]
    silent_removed = len(removed_keys) - len(removed_listings)
    if removed_listings:
        logger.info(f"Profitable listings removed: {[r['match_key'] for r in removed_listings]}")
    if silent_removed:
        logger.info(f"Non-profitable listings removed (no email): {silent_removed}")

    # ── 7. Evaluate profitability ─────────────────────────────────────────
    threshold = cfg["profit_threshold"]
    triggered: list[dict] = []
    all_profitable: list[dict] = []
    new_listings: list[dict] = []  # brand-new RTTs that aren't yet profitable

    dollar_threshold = cfg.get("profit_dollar_threshold", 300)

    supply_dumps: list[dict] = []
    now_ts = datetime.datetime.utcnow().isoformat()
    cutoff_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).isoformat()

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
                    "match_key": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
                    "category": listing["category"],
                    "rtt_price": rtt_price,
                    "get_in_price": None,
                    "profit_margin": None,
                })
            _prev_hist = state.get(composite_key, {}).get("price_history", [])
            _history = [e for e in _prev_hist if e["ts"] >= cutoff_ts]
            _history.append({"ts": now_ts, "rtt": rtt_price})
            state[composite_key] = {
                "min_price": rtt_price,
                "get_in_price": None,
                "margin": None,
                "match_key": listing["match_key"],
                "match_label": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
                "category": listing["category"],
                "price_history": _history,
            }
            continue

        seller_net, margin = compute_profit(rtt_price, get_in, cfg)
        profit_dollars = seller_net - rtt_price
        deadwood = is_deadwood_match(listing["home_team"], listing["away_team"], statuses)

        # Inventory tracking + supply dump detection
        tickets_available, tickets_status = find_td_inventory(
            listing["home_team"], listing["away_team"], td_matches,
            venue=listing.get("venue"), date_str=listing.get("match_date"),
        )
        prev_state = state.get(composite_key, {})
        prev_inventory = prev_state.get("tickets_available")
        inventory_delta = (
            (tickets_available - prev_inventory)
            if (prev_inventory is not None and tickets_available is not None)
            else None
        )
        supply_dump = (
            inventory_delta is not None and inventory_delta >= 500
        )

        # 24h RTT price history
        prev_history = prev_state.get("price_history", [])
        history = [e for e in prev_history if e["ts"] >= cutoff_ts]
        history.append({"ts": now_ts, "rtt": rtt_price, "get_in": get_in})
        one_hour_ago = (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)).isoformat()
        oldest = history[0] if history else None
        _oldest_get_in = oldest.get("get_in") if oldest else None
        price_change_24h = (
            round((get_in - _oldest_get_in) / _oldest_get_in * 100, 1)
            if (oldest and oldest["ts"] < one_hour_ago and _oldest_get_in)
            else None
        )

        result = {
            "match_key": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
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
            "price_change_24h": price_change_24h,
            "tickets_available": tickets_available,
            "supply_dump": supply_dump,
            "inventory_delta": inventory_delta,
            "listings_at_min": listing.get("listings_at_min", 1),
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
                "match_key": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
                "category": listing["category"],
                "rtt_price": rtt_price,
                "get_in_price": get_in,
                "profit_margin": margin,
            })
            logger.info(
                f"New listing (not profitable): {listing['match_key']} Cat {listing['category']} | "
                f"RTT ${rtt_price:,.0f} | Margin {margin:.1%}"
            )

        if supply_dump:
            supply_dumps.append({
                "match_key": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
                "tickets_available": tickets_available,
                "prev_inventory": prev_inventory,
                "inventory_delta": inventory_delta,
            })
            logger.warning(
                f"SUPPLY DUMP: {listing['match_key']} — FIFA inventory +{inventory_delta:,} "
                f"({prev_inventory:,} → {tickets_available:,})"
            )

        # Update state — raw match_key for stability, match_label for display
        state[composite_key] = {
            "min_price": rtt_price,
            "get_in_price": get_in,
            "margin": margin,
            "match_key": listing["match_key"],
            "match_label": _build_match_label(listing, td_matches, rankings, statuses or {}, bbc_proj),
            "category": listing["category"],
            "price_history": history,
            "price_change_24h": price_change_24h,
            "tickets_available": tickets_available,
            "listings_at_min": listing.get("listings_at_min", 1),
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
        from alerts.email import _build_subject, _build_text_body
        print(f"\nEMAIL SUBJECT: {_build_subject(triggered, removed_listings, [], viagogo_drops, supply_dumps)}\n")
        print(_build_text_body(triggered, all_profitable, removed_listings, [], viagogo_drops, supply_dumps))
    elif triggered or removed_listings or viagogo_drops or supply_dumps or force_alert:
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
                viagogo_drops=viagogo_drops,
                supply_dumps=supply_dumps,
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
