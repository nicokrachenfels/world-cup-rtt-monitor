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
from datetime import datetime

sys.path.insert(0, ".")

from scraper.fifa_rtt import scrape_fifa_rtt, get_min_prices_by_match
from scraper.ticketdata import scrape_all_matches, find_get_in_price, find_td_teams
from scraper.standings import fetch_standings, is_deadwood_match, get_group_rankings

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
BUYER_FEE = 0.20
SELLER_FEE = 0.10
MARGIN_THRESHOLD = 0.05
DOLLAR_THRESHOLD = 300

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


def seller_net(get_in: float) -> float:
    return get_in / (1 + BUYER_FEE) * (1 - SELLER_FEE)


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


async def build_rows() -> list[dict]:
    listings = await scrape_fifa_rtt()
    mins = get_min_prices_by_match(listings)
    td = scrape_all_matches()
    statuses = fetch_standings()
    rankings = get_group_rankings(statuses)

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
            location = next(
                (m.get("venue") or m.get("city", "")
                 for m in td.values() if m.get("match_code") == venue_code),
                ""
            )
            td_teams = find_td_teams(venue_code, td) if venue_code else None
            if td_teams:
                h, a = td_teams
                h_proj = _resolve_group_code(h, rankings)
                a_proj = _resolve_group_code(a, rankings)
                if h_proj and a_proj:
                    teams_str = f"{h_proj} vs {a_proj}"
                    proj_suffix = " (projected)"
                else:
                    # Strip any outer parens TicketData puts on winner refs like "(W85)"
                    teams_str = f"{h.strip('() ')} vs {a.strip('() ')}"
                    proj_suffix = ""
                match_label = f"{round_label} ({teams_str}){proj_suffix}" if round_label else teams_str
            else:
                match_label = round_label or venue_code or "TBD"
            subtitle = f"{date_part} · {location}" if location else date_part
        else:
            match_label = f"{v['home_team']} vs {v['away_team']}"
            location = v.get("venue", "")
            subtitle = f"{date_part} · {location}" if location else date_part

        fees_dollars = round(used - net, 2)  # total dollar impact of all fees

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
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2d3148;
    --text: #e8eaf6;
    --text-dim: #8b91b8;
    --text-muted: #555a7c;
    --green: #22c55e;
    --green-dim: #166534;
    --green-bg: rgba(34,197,94,.08);
    --red: #ef4444;
    --red-dim: #7f1d1d;
    --red-bg: rgba(239,68,68,.06);
    --amber: #f59e0b;
    --blue: #60a5fa;
    --purple: #a78bfa;
    --accent: #6366f1;
    --accent2: #4f46e5;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}

  /* ── Header ── */
  header {{
    background: linear-gradient(135deg, #1e2035 0%, #13152a 100%);
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

  /* ── Summary cards ── */
  .summary {{
    display: flex;
    gap: 12px;
    padding: 16px 28px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    overflow-x: auto;
  }}
  .card {{
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 18px;
    min-width: 120px;
    flex-shrink: 0;
  }}
  .card .label {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); margin-bottom: 4px; }}
  .card .value {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .card.green .value {{ color: var(--green); }}
  .card.amber .value {{ color: var(--amber); }}
  .card.blue .value {{ color: var(--blue); }}
  .card.purple .value {{ color: var(--purple); }}

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
  select option {{ background: #22263a; }}
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
    background: rgba(99,102,241,.12);
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
  .table-wrap {{ overflow-x: auto; padding: 20px 28px 40px; }}
  .table-clip {{
    border-radius: 10px;
    overflow: clip;
    border: 1px solid var(--border);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
    background: var(--surface);
  }}
  thead {{ position: sticky; top: 56px; z-index: 10; }}
  th {{
    background: var(--surface2);
    padding: 10px 14px;
    text-align: left;
    font-weight: 700;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--text-muted);
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
    border-bottom: 1px solid rgba(45,49,72,.6);
    white-space: nowrap;
    transition: background .1s;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(99,102,241,.04); }}
  tr.alert-row td {{ background: var(--green-bg); }}
  tr.alert-row:hover td {{ background: rgba(34,197,94,.12); }}
  tr.dead-row td {{ background: var(--red-bg); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.match-cell {{ font-weight: 500; max-width: 240px; overflow: hidden; text-overflow: ellipsis; }}
  td.match-cell .date-sub {{ font-size: 10.5px; color: var(--text-muted); margin-top: 1px; }}

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

  /* ── Badges ── */
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 5px; font-size: 10px; font-weight: 700; letter-spacing: .04em; }}
  .badge-alert {{ background: rgba(34,197,94,.15); color: var(--green); border: 1px solid rgba(34,197,94,.25); }}
  .badge-dead {{ background: rgba(239,68,68,.12); color: var(--red); border: 1px solid rgba(239,68,68,.2); }}
  .badge-tbd {{ background: rgba(96,165,250,.12); color: var(--blue); border: 1px solid rgba(96,165,250,.2); }}

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
    <span>Updated {updated_at}</span>
  </div>
</header>

<div class="summary" id="summaryCards">
  <div class="card green"><div class="label">Alerts</div><div class="value" id="cAlerts">—</div></div>
  <div class="card amber"><div class="label">Profit+</div><div class="value" id="cProfit">—</div></div>
  <div class="card blue"><div class="label">Showing</div><div class="value" id="cRows">—</div></div>
  <div class="card purple"><div class="label">Best margin</div><div class="value" id="cBest">—</div></div>
</div>

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
  <label class="toggle">
    <input type="checkbox" id="filterHideDead">
    <div class="pip"></div>
    Hide deadwood
  </label>
</div>

<div class="table-wrap">
  <div class="table-clip">
  <table id="mainTable">
    <thead>
      <tr>
        <th data-col="match">Match</th>
        <th data-col="cat">Cat</th>
        <th data-col="rtt" class="num">RTT price</th>
        <th data-col="breakeven" class="num">Breakeven</th>
        <th data-col="profit" class="num">Profit $</th>
        <th data-col="margin" class="num">Margin</th>
        <th data-col="used" class="num">Cat-adj. get-in</th>
        <th data-col="fees" class="num">Fees $</th>
        <th data-col="td_floor" class="num">Floor price</th>
        <th>Flags</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
  </div>
  <div id="no-results">
    <div class="icon">🔍</div>
    No matches found for selected filters.
  </div>
</div>

<div class="formula-note">
  <span><b>Formula:</b> seller_net = get_in ÷ 1.20 × 0.90 &nbsp;·&nbsp; profit = seller_net − RTT price</span>
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
  const hideDead = document.getElementById("filterHideDead").checked;

  let rows = RAW.filter(r => {{
    if (cat && r.cat !== cat) return false;
    if (date && r.date !== date) return false;
    if (teams === "confirmed" && r.tbd) return false;
    if (teams === "tbd" && !r.tbd) return false;
    if (alertsOnly && !r.alert) return false;
    if (hideDead && r.dead) return false;
    return true;
  }});

  rows = [...rows].sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (typeof av === "string") av = av.toLowerCase(), bv = bv.toLowerCase();
    return sortDir * (av < bv ? -1 : av > bv ? 1 : 0);
  }});

  const alerts = rows.filter(r => r.alert).length;
  const positive = rows.filter(r => r.profit > 0).length;
  const best = rows.filter(r => r.margin > 0)[0];
  document.getElementById("cAlerts").textContent = alerts;
  document.getElementById("cProfit").textContent = positive;
  document.getElementById("cRows").textContent = rows.length;
  document.getElementById("cBest").textContent = best ? (best.margin*100).toFixed(1)+"%" : "—";

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

    const flags = [];
    if (r.alert) flags.push('<span class="badge badge-alert">ALERT</span>');
    if (r.dead) flags.push('<span class="badge badge-dead">DEAD</span>');
    if (r.tbd) flags.push('<span class="badge badge-tbd">TBD</span>');

    const sub = r.subtitle || fmtDate(r.date);

    tbody.innerHTML += `<tr class="${{rowClass}}">
      <td class="match-cell">
        <div>${{r.match}}</div>
        ${{sub ? `<div class="date-sub">${{sub}}</div>` : ""}}
      </td>
      <td><span class="cat-pill ${{catClass}}">Cat ${{r.cat}}</span></td>
      <td class="num dim">$${{fmt(r.rtt)}}</td>
      <td class="num dim">$${{fmt(r.breakeven)}}</td>
      <td class="num ${{profitClass}}">${{profitStr}}</td>
      <td class="num ${{pctClass}}">${{pct}}</td>
      <td class="num dim">$${{fmt(r.used)}}</td>
      <td class="num very-dim">$${{fmt(r.fees)}}</td>
      <td class="num very-dim">$${{fmt(r.td_floor)}}</td>
      <td>${{flags.join(" ")}}</td>
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
["filterAlerts","filterHideDead"].forEach(id =>
  document.getElementById(id).addEventListener("change", renderTable));

document.querySelector('th[data-col="margin"]').classList.add("sorted-desc");
renderTable();
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)


async def main(no_open: bool = False) -> None:
    print("Scraping live data...")
    rows = await build_rows()
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
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
