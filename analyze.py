"""
RTT Arbitrage analysis: scrapes live data and generates dashboard.html.
Run: python analyze.py
Opens dashboard.html in your browser automatically.
"""
import asyncio
import json
import os
import sys
import webbrowser
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from scraper.fifa_rtt import scrape_fifa_rtt, get_min_prices_by_match
from scraper.ticketdata import scrape_all_matches, find_get_in_price, find_td_teams, find_td_match, find_td_inventory
from scraper.standings import fetch_standings, is_deadwood_match, get_group_rankings
from scraper.bbc_bracket import scrape_bbc_bracket, _norm as _bbc_norm

import re as _re
from typing import Optional

_GROUP_CODE_RE = _re.compile(r'^(\d)([A-Z])$')

_MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_MON_MAP = {m.upper(): i for i, m in enumerate(_MONTH_ABBR)}


def _fmt_date(date_str: str) -> str:
    """Convert 'JUN 25' → 'Jun 25'. Pass through anything else."""
    parts = date_str.split()
    if len(parts) == 2 and parts[0].upper() in _MON_MAP:
        return f"{_MONTH_ABBR[_MON_MAP[parts[0].upper()]]} {int(parts[1])}"
    return date_str


def _resolve_group_code(code: str, rankings: dict) -> Optional[str]:
    """'1H' → current 1st-place team in Group H, or None if unresolvable."""
    m = _GROUP_CODE_RE.match(code.strip())
    if not m:
        return None
    pos, group = int(m.group(1)), m.group(2)
    teams = rankings.get(group, [])
    return teams[pos - 1] if len(teams) >= pos else None


CAT1_MULTIPLIER = 1.20
BUYER_FEE = 0.00
SELLER_FEE = 0.275
MARGIN_THRESHOLD = 0.15
DOLLAR_THRESHOLD = 250

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


def seller_net(get_in: float) -> float:
    return get_in / (1 + BUYER_FEE) * (1 - SELLER_FEE)


def _team_label(
    name: str,
    raw_code: str,
    statuses: dict,
    rankings: dict,
) -> str:
    """
    Return "Name (lock)" or "Name (proj.)" based on whether the team's
    R32 slot is mathematically secured.

    raw_code: the original TicketData token for this side — e.g. "1B", "2F",
              "Germany", or "TBD" (wildcard resolved via BBC).
    """
    if not name or name == "TBD":
        return name
    # Bracket references (e.g. "W74", "L85", "RU101") are not team projections
    if _re.match(r'^[WLR][UL]?\d+$', name.strip()):
        return name

    s = statuses.get(name.lower(), {})
    m = _GROUP_CODE_RE.match(raw_code.strip()) if raw_code else None

    if m:
        pos, group = int(m.group(1)), m.group(2)
        if pos == 1 and s.get("clinched_first"):
            return f"{name} (lock)"
        if pos == 2 and s.get("clinched_second"):
            return f"{name} (lock)"
        return f"{name} (proj.)"

    # Direct team name already known (not a group code)
    if raw_code and raw_code != "TBD":
        if s.get("clinched_first"):
            return f"{name} (lock)"
        if s.get("clinched_second"):
            return f"{name} (lock)"
        return f"{name} (proj.)"

    # raw_code == "TBD" means this slot came from BBC (wildcard 3rd-place)
    return f"{name} (proj.)"


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


def apply_bbc_projections(
    td_matches: dict,
    bbc_pairs: list[dict],
    rankings: dict,
) -> dict:
    """
    Return {match_code: (home_team, away_team)} for R32 matches (M73-M88)
    using BBC 'as it stands' bracket as the source of wildcard team names.
    """
    if not bbc_pairs:
        return {}

    # Build lookup: norm(team_name) → pair
    bbc_by_team: dict[str, dict] = {}
    for pair in bbc_pairs:
        bbc_by_team[_bbc_norm(pair["home"])] = pair
        bbc_by_team[_bbc_norm(pair["away"])] = pair

    # Build lookup: date → [pair] for both-TBD date matching
    bbc_by_date: dict[str, list] = {}
    for pair in bbc_pairs:
        d = pair.get("date", "")
        if d:
            bbc_by_date.setdefault(d, []).append(pair)

    _MONTHS = ["","JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

    result = {}
    for td_m in td_matches.values():
        mc = td_m.get("match_code", "")
        if not mc:
            continue
        try:
            n = int(mc[1:])
        except (ValueError, IndexError):
            continue
        if n < 73 or n > 88:
            continue

        h_raw = td_m.get("home_team", "TBD").strip("() ")
        a_raw = td_m.get("away_team", "TBD").strip("() ")
        if _re.match(r'^World Cup Match\b', h_raw, _re.I): h_raw = "TBD"
        if _re.match(r'^World Cup Match\b', a_raw, _re.I): a_raw = "TBD"

        h_res = _resolve_group_code(h_raw, rankings) or (None if h_raw == "TBD" else h_raw)
        a_res = _resolve_group_code(a_raw, rankings) or (None if a_raw == "TBD" else a_raw)

        bbc_pair = None
        if h_res and _bbc_norm(h_res) in bbc_by_team:
            bbc_pair = bbc_by_team[_bbc_norm(h_res)]
        elif a_res and _bbc_norm(a_res) in bbc_by_team:
            bbc_pair = bbc_by_team[_bbc_norm(a_res)]

        if bbc_pair is None:
            # Fall back to date matching
            raw_date = td_m.get("match_date", "")
            try:
                parts = raw_date.split("-")
                month_num = int(parts[1])
                day = int(parts[2])
                date_key = f"{_MONTHS[month_num]} {day}"
                candidates = bbc_by_date.get(date_key, [])
                if len(candidates) == 1:
                    bbc_pair = candidates[0]
            except (ValueError, IndexError):
                pass

        if bbc_pair:
            result[mc] = (bbc_pair["home"], bbc_pair["away"])

    return result


async def build_rows() -> list[dict]:
    listings = await scrape_fifa_rtt()
    mins = get_min_prices_by_match(listings)
    td = scrape_all_matches()
    statuses = fetch_standings()
    rankings = get_group_rankings(statuses)

    state = {}
    state_path = os.path.join(os.path.dirname(__file__), "state.json")
    if os.path.exists(state_path):
        with open(state_path) as _f:
            state = json.load(_f)

    bbc_pairs = await scrape_bbc_bracket()
    bbc_proj = apply_bbc_projections(td, bbc_pairs, rankings)

    rows = []
    for v in mins.values():
        cat = v["category"]
        rtt_price = v["min_price"]
        td_floor = find_get_in_price(
            v["home_team"], v["away_team"], td,
            venue=v.get("venue"), date_str=v.get("match_date"),
        )
        if td_floor is None:
            continue

        used = (td_floor * CAT1_MULTIPLIER if cat == "1"
                else td_floor if cat == "2"
                else td_floor * 0.80)

        net = seller_net(used)
        profit = net - rtt_price
        margin = profit / rtt_price
        dead = is_deadwood_match(v["home_team"], v["away_team"], statuses)
        tbd = v["home_team"] == "TBD"
        venue_code = v.get("venue", "")
        match_date = v.get("match_date", "Unknown")

        date_part = _fmt_date(match_date)

        if tbd:
            round_label = _round_name(venue_code)
            # Get stadium/city from TicketData for the location subtitle
            _td_m = next((m for m in td.values() if m.get("match_code") == venue_code), None)
            if _td_m:
                _v, _c = _td_m.get("venue", ""), _td_m.get("city", "")
                location = f"{_v}, {_c}" if (_v and _c) else (_v or _c)
            else:
                location = ""

            # Pull raw TicketData codes for this slot (needed for lock/proj. tagging)
            _h_code = _td_m.get("home_team", "TBD").strip("() ") if _td_m else "TBD"
            _a_code = _td_m.get("away_team", "TBD").strip("() ") if _td_m else "TBD"
            if _re.match(r'^World Cup Match\b', _h_code, _re.I): _h_code = "TBD"
            if _re.match(r'^World Cup Match\b', _a_code, _re.I): _a_code = "TBD"

            def _fmt_team(name: str, raw: str) -> str:
                """Format one team name with (lock)/(proj.) suffix."""
                return _team_label(name, raw, statuses, rankings)

            # 1. BBC bracket projection (covers wildcard 3rd-place slots)
            bbc_teams = bbc_proj.get(venue_code)
            if bbc_teams:
                h_bbc, a_bbc = bbc_teams
                h_str = _fmt_team(h_bbc, _h_code)
                a_str = _fmt_team(a_bbc, _a_code)
                teams_str = f"{h_str} vs {a_str}"
                match_label = f"{round_label}: {teams_str}" if round_label else teams_str
            else:
                # 2. TicketData known teams (both sides resolved)
                td_teams = find_td_teams(venue_code, td) if venue_code else None
                if td_teams:
                    h, a = td_teams
                    h_proj = _resolve_group_code(h, rankings)
                    a_proj = _resolve_group_code(a, rankings)
                    if h_proj and a_proj:
                        h_str = _fmt_team(h_proj, h)
                        a_str = _fmt_team(a_proj, a)
                    else:
                        h_str = _fmt_team(h.strip("() "), h)
                        a_str = _fmt_team(a.strip("() "), a)
                    teams_str = f"{h_str} vs {a_str}"
                    match_label = f"{round_label}: {teams_str}" if round_label else teams_str
                else:
                    # 3. Partial resolution from TicketData when one side is known
                    if _td_m and (_h_code != "TBD" or _a_code != "TBD"):
                        h_resolved = _resolve_group_code(_h_code, rankings) if _h_code != "TBD" else None
                        a_resolved = _resolve_group_code(_a_code, rankings) if _a_code != "TBD" else None
                        h_name = h_resolved or (_h_code if _h_code != "TBD" else None)
                        a_name = a_resolved or (_a_code if _a_code != "TBD" else None)
                        h_str = _fmt_team(h_name, _h_code) if h_name else "TBD"
                        a_str = _fmt_team(a_name, _a_code) if a_name else "TBD"
                        teams_str = f"{h_str} vs {a_str}"
                        match_label = f"{round_label}: {teams_str}" if round_label else teams_str
                    else:
                        match_label = f"{round_label}: {venue_code}" if (round_label and venue_code) else (round_label or venue_code or "TBD")
            subtitle = f"{date_part} · {location}" if location else date_part
        else:
            match_label = f"{v['home_team']} vs {v['away_team']}"
            _td_m = find_td_match(v["home_team"], v["away_team"], td,
                                  venue=venue_code, date_str=match_date)
            if _td_m:
                _v, _c = _td_m.get("venue", ""), _td_m.get("city", "")
                location = f"{_v}, {_c}" if (_v and _c) else (_v or _c)
            else:
                location = venue_code
            subtitle = f"{date_part} · {location}" if location else date_part

        fees_dollars = round(used - net, 2)  # total dollar impact of all fees

        inv_count, _inv_status = find_td_inventory(
            v["home_team"], v["away_team"], td,
            venue=v.get("venue"), date_str=v.get("match_date"),
        )
        composite_key = f"{v['match_key']}||cat{cat}"
        price_change_24h = state.get(composite_key, {}).get("price_change_24h")
        listings_at_min = v.get("listings_at_min", 1)

        rows.append({
            "match": match_label,
            "subtitle": subtitle,
            "home": v["home_team"],
            "away": v["away_team"],
            "venue": venue_code,
            "date": match_date,
            "cat": cat,
            "rtt": rtt_price,
            "td_floor": td_floor,
            "used": round(used, 2),
            "net": round(net, 2),
            "breakeven": round(net, 2),
            "fees": fees_dollars,
            "profit": round(profit, 2),
            "margin": round(margin, 4),
            "dead": dead,
            "tbd": tbd,
            "alert": margin >= MARGIN_THRESHOLD or profit >= DOLLAR_THRESHOLD,
            "tickets_available": inv_count,
            "price_change_24h": price_change_24h,
            "listings_at_min": listings_at_min,
        })

    rows.sort(key=lambda x: -x["margin"])
    return rows


def generate_dashboard(rows: list[dict], updated_at: str) -> None:
    data_json = json.dumps(rows)
    unique_dates = sorted(set(r["date"] for r in rows if r["date"] != "Unknown"))
    date_options = "\n".join(
        f'<option value="{d}">{d}</option>' for d in unique_dates
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RTT Arbitrage Monitor</title>
<style>
  :root {{
    --bg: #080C16;
    --surface: #0C1220;
    --surface2: #111826;
    --border: #1B2640;
    --row-border: rgba(27,38,64,.65);
    --text: #DCE3F0;
    --text-dim: #5B7192;
    --text-muted: #344358;
    --green: #05C96A;
    --green-dim: #034D2A;
    --green-bg: rgba(5,201,106,.05);
    --red: #F04461;
    --red-dim: #5A1122;
    --red-bg: rgba(240,68,97,.05);
    --amber: #F59E0B;
    --blue: #4EA8DE;
    --purple: #A78BFA;
    --accent: #05C96A;
    --accent2: #04A354;
    --mono: "SF Mono","Fira Code","Cascadia Code",ui-monospace,monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}

  /* ── Header ── */
  header {{
    background: linear-gradient(135deg, #0E1828 0%, #080C16 100%);
    border-bottom: 1px solid var(--border);
    padding: 0 28px;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(8px);
  }}
  header h1 {{ font-size: 15px; font-weight: 700; letter-spacing: .01em; display: flex; align-items: center; gap: 8px; }}
  header h1 .icon {{ font-size: 18px; }}
  header .meta {{ font-size: 11px; color: var(--text-muted); display: flex; align-items: center; gap: 16px; }}
  header .meta .dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity: 1 }} 50% {{ opacity: .4 }} }}

  /* ── Controls ── */
  .controls {{
    padding: 12px 28px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .filter-group {{
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 5px 10px;
  }}
  .filter-group label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--text-muted); white-space: nowrap; }}
  select {{
    background: transparent;
    border: none;
    color: var(--text);
    font-size: 13px;
    outline: none;
    cursor: pointer;
    padding: 0 2px;
  }}
  select option {{ background: #111826; }}
  .toggle {{
    display: flex;
    align-items: center;
    gap: 7px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
    font-size: 12px;
    color: var(--text-dim);
    transition: border-color .15s, background .15s;
    user-select: none;
  }}
  .toggle:has(input:checked) {{
    border-color: var(--accent);
    background: rgba(5,201,106,.09);
    color: var(--text);
  }}
  .toggle input {{ display: none; }}
  .toggle .pip {{
    width: 28px; height: 16px;
    background: var(--border);
    border-radius: 8px;
    position: relative;
    transition: background .15s;
  }}
  .toggle:has(input:checked) .pip {{ background: var(--accent); }}
  .toggle .pip::after {{
    content: "";
    position: absolute;
    top: 2px; left: 2px;
    width: 12px; height: 12px;
    background: #fff;
    border-radius: 50%;
    transition: transform .15s;
  }}
  .toggle:has(input:checked) .pip::after {{ transform: translateX(12px); }}

  /* ── Table ── */
  .table-wrap {{ padding: 20px 28px 40px; }}
  table {{
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 12.5px;
    background: var(--surface);
    border: 1px solid var(--border);
  }}
  thead {{ background: var(--surface2); }}
  th {{
    position: sticky;
    top: 56px;
    z-index: 20;
    background: var(--surface2);
    padding: 10px 14px;
    text-align: left;
    font-weight: 700;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
    user-select: none;
    border-bottom: 1px solid var(--border);
  }}
  th:hover {{ color: var(--text); }}
  th.num {{ text-align: right; }}
  th.sorted-asc::after {{ content: " ↑"; color: var(--accent); }}
  th.sorted-desc::after {{ content: " ↓"; color: var(--accent); }}
  td {{
    padding: 9px 14px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    transition: background .1s;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(5,201,106,.025); }}
  tr.alert-row td {{ background: var(--green-bg); }}
  tr.alert-row:hover td {{ background: rgba(5,201,106,.10); }}
  tr.dead-row td {{ background: var(--red-bg); }}
  .num {{ text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }}
  td.match-cell {{ font-weight: 500; min-width: 220px; white-space: normal; }}
  td.match-cell .date-sub {{ font-size: 10.5px; color: #6B84A2; margin-top: 2px; }}

  /* ── Number colors ── */
  .profit-pos {{ color: var(--green); font-weight: 700; }}
  .profit-neg {{ color: var(--red); }}
  .margin-pos {{ color: var(--green); font-weight: 700; }}
  .margin-neg {{ color: var(--red); }}
  .dim {{ color: var(--text-dim); }}
  .very-dim {{ color: var(--text-muted); }}

  /* ── Cat pill ── */
  .cat-pill {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .04em;
  }}
  .cat-1 {{ background: rgba(167,139,250,.15); color: var(--purple); }}
  .cat-2 {{ background: rgba(96,165,250,.12); color: var(--blue); }}
  .cat-3 {{ background: rgba(100,116,139,.15); color: #94a3b8; }}

  /* ── No results ── */
  #no-results {{
    display: none;
    padding: 60px;
    text-align: center;
    color: var(--text-muted);
    font-size: 14px;
    background: var(--surface);
    border-radius: 10px;
    margin-top: 0;
    border: 1px solid var(--border);
  }}
  #no-results .icon {{ font-size: 32px; margin-bottom: 8px; }}

  /* ── Formula note ── */
  .formula-note {{
    margin: 0 28px 24px;
    padding: 10px 16px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 11px;
    color: var(--text-muted);
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
  }}
  .formula-note span {{ white-space: nowrap; }}
  .formula-note b {{ color: var(--text-dim); }}

  /* ── Scrollbar ── */
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
</style>
</head>
<body>
<header>
  <h1><span class="icon">⚽</span> RTT Arbitrage Monitor</h1>
  <div class="meta">
    <div class="dot"></div>
    <span id="updatedAt" data-utc="{updated_at}">Updated {updated_at}</span>
  </div>
</header>

<div class="controls">
  <div class="filter-group">
    <label>Cat</label>
    <select id="filterCat">
      <option value="">All</option>
      <option value="1">Cat 1</option>
      <option value="2">Cat 2</option>
      <option value="3">Cat 3</option>
    </select>
  </div>
  <div class="filter-group">
    <label>Date</label>
    <select id="filterDate">
      <option value="">All</option>
      {date_options}
    </select>
  </div>
  <div class="filter-group">
    <label>Teams</label>
    <select id="filterTeams">
      <option value="">All</option>
      <option value="confirmed">Confirmed</option>
      <option value="tbd">TBD</option>
    </select>
  </div>
  <label class="toggle">
    <input type="checkbox" id="filterAlerts">
    <div class="pip"></div>
    Alerts only (≥{int(MARGIN_THRESHOLD*100)}% or +${DOLLAR_THRESHOLD})
  </label>
</div>

<div class="table-wrap">
  <table id="mainTable">
    <thead>
      <tr>
        <th data-col="match">Match</th>
        <th data-col="cat">Cat</th>
        <th data-col="rtt" class="num">RTT price</th>
        <th data-col="breakeven" class="num">Breakeven Bid Price</th>
        <th data-col="profit" class="num">Profit $</th>
        <th data-col="margin" class="num">Margin</th>
        <th data-col="used" class="num">Cat-adj. get-in</th>
        <th data-col="price_change_24h" class="num">3h get-in Δ</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
  <div id="no-results">
    <div class="icon">🔍</div>
    No matches found for selected filters.
  </div>
</div>

<div class="formula-note">
  <span><b>Formula:</b> seller_net = get_in × 0.725 &nbsp;·&nbsp; profit = seller_net − RTT price</span>
  <span><b>Cat multipliers:</b> Cat1 × 1.20, Cat2 × 1.00, Cat3 × 0.80</span>
  <span><b>Alert:</b> margin ≥ {int(MARGIN_THRESHOLD*100)}% OR profit ≥ ${DOLLAR_THRESHOLD}</span>
</div>

<script>
const RAW = {data_json};
let sortCol = "margin", sortDir = -1;

function fmt(n) {{
  return new Intl.NumberFormat("en-US", {{maximumFractionDigits: 0}}).format(n);
}}

function fmtDate(d) {{
  if (!d || d === "Unknown") return "";
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  // "JUN 25" format from FIFA RTT
  const MON = {{JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11}};
  const parts = d.split(" ");
  if (parts.length === 2 && MON[parts[0]] !== undefined)
    return `${{MONTHS[MON[parts[0]]]}} ${{parseInt(parts[1])}}`;
  // "2026-07-04" fallback
  const [, m, day] = d.split("-");
  if (m) return `${{MONTHS[+m-1]}} ${{+day}}`;
  return d;
}}

function renderTable() {{
  const cat = document.getElementById("filterCat").value;
  const date = document.getElementById("filterDate").value;
  const teams = document.getElementById("filterTeams").value;
  const alertsOnly = document.getElementById("filterAlerts").checked;

  let rows = RAW.filter(r => {{
    if (cat && r.cat !== cat) return false;
    if (date && r.date !== date) return false;
    if (teams === "confirmed" && r.tbd) return false;
    if (teams === "tbd" && !r.tbd) return false;
    if (alertsOnly && !r.alert) return false;
    return true;
  }});

  rows = [...rows].sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (typeof av === "string") av = av.toLowerCase(), bv = bv.toLowerCase();
    return sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  }});

  const tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";

  if (rows.length === 0) {{
    document.getElementById("no-results").style.display = "block";
    document.getElementById("mainTable").style.display = "none";
    return;
  }}
  document.getElementById("no-results").style.display = "none";
  document.getElementById("mainTable").style.display = "";

  rows.forEach(r => {{
    const pct = (r.margin * 100).toFixed(1) + "%";
    const pctClass = r.margin > 0 ? "margin-pos" : "margin-neg";
    const profitClass = r.profit >= 0 ? "profit-pos" : "profit-neg";
    const profitStr = (r.profit >= 0 ? "+$" : "−$") + fmt(Math.abs(r.profit));
    const rowClass = r.alert ? "alert-row" : r.dead ? "dead-row" : "";
    const catClass = `cat-${{r.cat}}`;
    const rttDelta = r.price_change_24h != null ? (r.price_change_24h > 0 ? "▲ " + Math.abs(r.price_change_24h).toFixed(1) + "%" : "▼ " + Math.abs(r.price_change_24h).toFixed(1) + "%") : "—";
    const deltaClass = r.price_change_24h != null ? "dim" : "very-dim";
    const rttStr = "$" + fmt(r.rtt) + (r.listings_at_min > 1 ? ` <span style="color:#5B7192;font-size:11px">×${{r.listings_at_min}}</span>` : "");

    const sub = r.subtitle || fmtDate(r.date);

    tbody.innerHTML += `<tr class="${{rowClass}}">
      <td class="match-cell">
        <div>${{r.match}}</div>
        ${{sub ? `<div class="date-sub">${{sub}}</div>` : ""}}
      </td>
      <td><span class="cat-pill ${{catClass}}">Cat ${{r.cat}}</span></td>
      <td class="num dim">${{rttStr}}</td>
      <td class="num dim">$${{fmt(r.breakeven)}}</td>
      <td class="num ${{profitClass}}">${{profitStr}}</td>
      <td class="num ${{pctClass}}">${{pct}}</td>
      <td class="num dim">$${{fmt(r.used)}}</td>
      <td class="num ${{deltaClass}}">${{rttDelta}}</td>
    </tr>`;
  }});
}}

document.querySelectorAll("th[data-col]").forEach(th => {{
  th.addEventListener("click", () => {{
    const col = th.dataset.col;
    if (sortCol === col) sortDir *= -1;
    else {{ sortCol = col; sortDir = -1; }}
    document.querySelectorAll("th").forEach(t => t.classList.remove("sorted-asc","sorted-desc"));
    th.classList.add(sortDir === 1 ? "sorted-asc" : "sorted-desc");
    renderTable();
  }});
}});

["filterCat","filterDate","filterTeams"].forEach(id =>
  document.getElementById(id).addEventListener("change", renderTable));
document.getElementById("filterAlerts").addEventListener("change", renderTable);

document.querySelector('th[data-col="margin"]').classList.add("sorted-desc");
renderTable();

(function() {{
  const el = document.getElementById("updatedAt");
  if (!el) return;
  const d = new Date(el.dataset.utc);
  if (isNaN(d)) return;
  el.textContent = "Updated " + d.toLocaleString(undefined, {{
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit"
  }});
}})();
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)


async def main(no_open: bool = False) -> None:
    print("Scraping live data...")
    rows = await build_rows()
    updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    generate_dashboard(rows, updated_at)

    alerts = sum(1 for r in rows if r["alert"])
    positive = sum(1 for r in rows if r["profit"] > 0)
    print(f"Done. {len(rows)} rows, {alerts} alerts, {positive} profit+")
    print(f"Dashboard: {DASHBOARD_PATH}")

    if not no_open:
        webbrowser.open(f"file://{DASHBOARD_PATH}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate RTT arbitrage dashboard")
    parser.add_argument("--no-open", action="store_true", help="Skip opening browser (for CI)")
    args = parser.parse_args()
    asyncio.run(main(no_open=args.no_open))
