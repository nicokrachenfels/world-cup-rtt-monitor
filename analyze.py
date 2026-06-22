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
from scraper.ticketdata import scrape_all_matches, find_get_in_price
from scraper.standings import fetch_standings, is_deadwood_match

CAT1_MULTIPLIER = 1.20
BUYER_FEE = 0.20
SELLER_FEE = 0.10
MARGIN_THRESHOLD = 0.05
DOLLAR_THRESHOLD = 300

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


def seller_net(get_in: float) -> float:
    return get_in / (1 + BUYER_FEE) * (1 - SELLER_FEE)


async def build_rows() -> list[dict]:
    listings = await scrape_fifa_rtt()
    mins = get_min_prices_by_match(listings)
    td = scrape_all_matches()
    statuses = fetch_standings()

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

        if tbd:
            match_label = f"{v.get('venue', '?')} {v.get('match_date', '?')} TBD"
        else:
            match_label = f"{v['home_team']} vs {v['away_team']}"

        rows.append({
            "match": match_label,
            "home": v["home_team"],
            "away": v["away_team"],
            "venue": v.get("venue", "Unknown"),
            "date": v.get("match_date", "Unknown"),
            "cat": cat,
            "rtt": rtt_price,
            "td_floor": td_floor,
            "used": round(used, 2),
            "net": round(net, 2),
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
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f7; color: #1d1d1f; }}
  header {{ background: #1d1d1f; color: #fff; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }}
  header h1 {{ font-size: 18px; font-weight: 600; }}
  header .updated {{ font-size: 12px; color: #aaa; }}
  .controls {{ padding: 14px 24px; background: #fff; border-bottom: 1px solid #e0e0e0; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
  .controls label {{ font-size: 13px; font-weight: 500; color: #555; }}
  .controls select, .controls input[type=checkbox] {{ font-size: 13px; }}
  select {{ padding: 5px 8px; border: 1px solid #ccc; border-radius: 6px; background: #fff; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .badge-alert {{ background: #d4f0d4; color: #1a7f37; }}
  .badge-dead {{ background: #fce8e8; color: #c62828; }}
  .badge-tbd {{ background: #e8f0fe; color: #1a73e8; }}
  .stats {{ padding: 10px 24px; background: #fff; border-bottom: 1px solid #e0e0e0; font-size: 13px; color: #555; display: flex; gap: 20px; }}
  .stats b {{ color: #1d1d1f; }}
  .table-wrap {{ overflow-x: auto; padding: 0 24px 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  th {{ background: #f1f3f4; padding: 10px 12px; text-align: left; font-weight: 600; font-size: 12px; color: #555; cursor: pointer; white-space: nowrap; user-select: none; }}
  th:hover {{ background: #e8eaed; }}
  th.sorted-asc::after {{ content: " ▲"; }}
  th.sorted-desc::after {{ content: " ▼"; }}
  td {{ padding: 9px 12px; border-top: 1px solid #f0f0f0; white-space: nowrap; }}
  tr:hover td {{ background: #fafafa; }}
  tr.alert-row td {{ background: #f0faf2; }}
  tr.dead-row td {{ background: #fff8f8; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .profit-pos {{ color: #1a7f37; font-weight: 600; }}
  .profit-neg {{ color: #c62828; }}
  .margin-high {{ color: #1a7f37; font-weight: 700; }}
  .margin-mid {{ color: #e67700; font-weight: 600; }}
  .margin-low {{ color: #c62828; }}
  .sep {{ color: #ccc; }}
  #no-results {{ display: none; padding: 40px; text-align: center; color: #888; font-size: 14px; background: #fff; border-radius: 8px; margin-top: 16px; }}
</style>
</head>
<body>
<header>
  <h1>⚽ RTT Arbitrage Monitor</h1>
  <span class="updated">Updated: {updated_at}</span>
</header>
<div class="controls">
  <label>Category:
    <select id="filterCat">
      <option value="">All</option>
      <option value="1">Cat 1</option>
      <option value="2">Cat 2</option>
      <option value="3">Cat 3</option>
    </select>
  </label>
  <label>Date:
    <select id="filterDate">
      <option value="">All dates</option>
      {date_options}
    </select>
  </label>
  <label>Teams:
    <select id="filterTeams">
      <option value="">All</option>
      <option value="confirmed">Confirmed only</option>
      <option value="tbd">TBD only</option>
    </select>
  </label>
  <label><input type="checkbox" id="filterAlerts"> Alerts only (≥{int(MARGIN_THRESHOLD*100)}% or +${DOLLAR_THRESHOLD})</label>
  <label><input type="checkbox" id="filterHideDead"> Hide deadwood</label>
</div>
<div class="stats" id="statsBar">Loading...</div>
<div class="table-wrap">
  <table id="mainTable">
    <thead>
      <tr>
        <th data-col="match">Match</th>
        <th data-col="cat">Cat</th>
        <th data-col="date">Date</th>
        <th data-col="rtt" class="num sorted-desc">RTT $</th>
        <th data-col="td_floor" class="num">TD Floor</th>
        <th data-col="used" class="num">Used</th>
        <th data-col="profit" class="num">Profit $</th>
        <th data-col="margin" class="num">Margin</th>
        <th>Flags</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
  <div id="no-results">No matches found for selected filters.</div>
</div>
<script>
const RAW = {data_json};
let sortCol = "margin", sortDir = -1;

function fmt(n) {{
  return new Intl.NumberFormat("en-US", {{maximumFractionDigits: 0}}).format(n);
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
  document.getElementById("statsBar").innerHTML =
    `<span>Showing <b>${{rows.length}}</b> rows</span>` +
    `<span class="sep">|</span><span><b>${{alerts}}</b> alerts</span>` +
    `<span class="sep">|</span><span><b>${{positive}}</b> profit+ matches</span>` +
    `<span class="sep">|</span><span>Formula: get_in × 0.75 (÷1.20 buyer fee, ×0.90 seller fee) &nbsp;·&nbsp; Cat1 used = floor × ${{(1.20).toFixed(2)}}x</span>`;

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
    const pctClass = r.margin >= 0.10 ? "margin-high" : r.margin >= 0.05 ? "margin-mid" : "margin-low";
    const profitClass = r.profit >= 0 ? "profit-pos" : "profit-neg";
    const profitStr = (r.profit >= 0 ? "+$" : "-$") + fmt(Math.abs(r.profit));
    const rowClass = r.alert ? "alert-row" : r.dead ? "dead-row" : "";

    const flags = [];
    if (r.alert) flags.push('<span class="badge badge-alert">◄ ALERT</span>');
    if (r.dead) flags.push('<span class="badge badge-dead">DEAD</span>');
    if (r.tbd) flags.push('<span class="badge badge-tbd">TBD</span>');

    tbody.innerHTML += `<tr class="${{rowClass}}">
      <td>${{r.match}}</td>
      <td>Cat ${{r.cat}}</td>
      <td>${{r.date}}</td>
      <td class="num">$${{fmt(r.rtt)}}</td>
      <td class="num">$${{fmt(r.td_floor)}}</td>
      <td class="num">$${{fmt(r.used)}}</td>
      <td class="num ${{profitClass}}">${{profitStr}}</td>
      <td class="num ${{pctClass}}">${{pct}}</td>
      <td>${{flags.join(" ")}}</td>
    </tr>`;
  }});
}}

// Sortable columns
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

// Filter listeners
["filterCat","filterDate","filterTeams"].forEach(id =>
  document.getElementById(id).addEventListener("change", renderTable));
["filterAlerts","filterHideDead"].forEach(id =>
  document.getElementById(id).addEventListener("change", renderTable));

// Initial sort state
document.querySelector('th[data-col="margin"]').classList.add("sorted-desc");
renderTable();
</script>
</body>
</html>"""

    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)


async def main() -> None:
    print("Scraping live data...")
    rows = await build_rows()
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    generate_dashboard(rows, updated_at)

    alerts = sum(1 for r in rows if r["alert"])
    positive = sum(1 for r in rows if r["profit"] > 0)
    print(f"Done. {len(rows)} rows, {alerts} alerts, {positive} profit+")
    print(f"Dashboard: {DASHBOARD_PATH}")

    webbrowser.open(f"file://{DASHBOARD_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
