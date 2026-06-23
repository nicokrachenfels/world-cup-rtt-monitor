"""
Send RTT arbitrage alerts via SendGrid.
"""
import logging
import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def send_alert(
    sendgrid_api_key: str,
    from_email: str,
    to_email: str,
    triggered_listings: list[dict],
    all_profitable_listings: list[dict],
    removed_listings: list[dict] = [],
    new_listings: list[dict] = [],
    viagogo_drops: list[dict] = [],
) -> None:
    if not triggered_listings and not removed_listings and not new_listings and not viagogo_drops:
        return

    subject = _build_subject(triggered_listings, removed_listings, new_listings, viagogo_drops)
    html_body = _build_html_body(triggered_listings, all_profitable_listings, removed_listings, new_listings, viagogo_drops)
    text_body = _build_text_body(triggered_listings, all_profitable_listings, removed_listings, new_listings, viagogo_drops)

    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": html_body},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        SENDGRID_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {sendgrid_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            logger.info(f"Alert sent to {to_email} (status {resp.status}): {subject}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        logger.error(f"SendGrid error {e.code}: {body}")
        if e.code == 403:
            logger.error(
                "Fix: verify ALERT_FROM_EMAIL matches a Single Sender at "
                "https://app.sendgrid.com/settings/sender_auth/senders"
            )
        raise


def _build_subject(triggered: list[dict], removed: list[dict] = [], new_listings: list[dict] = [], viagogo_drops: list[dict] = []) -> str:
    if triggered and len(triggered) == 1:
        t = triggered[0]
        extras = []
        if removed:
            extras.append(f"{len(removed)} removed")
        if new_listings:
            extras.append(f"{len(new_listings)} new")
        if viagogo_drops:
            extras.append("Argentina drop")
        suffix = f" | {', '.join(extras)}" if extras else ""
        return (
            f"RTT Alert: {t['match_key']} | "
            f"{t['profit_margin']:.0%} profit | "
            f"RTT ${t['rtt_price']:,.0f} vs ${t['get_in_price']:,.0f} get-in{suffix}"
        )
    if triggered:
        extras = []
        if removed:
            extras.append(f"{len(removed)} removed")
        if new_listings:
            extras.append(f"{len(new_listings)} new")
        if viagogo_drops:
            extras.append("Argentina drop")
        suffix = f" | {', '.join(extras)}" if extras else ""
        return (
            f"RTT Alert: {len(triggered)} new opportunities | "
            f"Best: {max(t['profit_margin'] for t in triggered):.0%} profit{suffix}"
        )
    if new_listings and not removed and not viagogo_drops:
        return f"RTT Supply: {len(new_listings)} new listing(s) on marketplace"
    if viagogo_drops and not triggered and not removed and not new_listings:
        d = viagogo_drops[0]
        return f"Argentina price drop: ${d['current_price']:,.0f} (threshold ${d['threshold']:,.0f})"
    parts = []
    if removed:
        parts.append(f"{len(removed)} removed")
    if new_listings:
        parts.append(f"{len(new_listings)} new")
    if viagogo_drops:
        parts.append("Argentina drop")
    return f"RTT Activity: {', '.join(parts)}"


def _build_html_body(triggered: list[dict], all_profitable: list[dict], removed: list[dict] = [], new_listings: list[dict] = [], viagogo_drops: list[dict] = []) -> str:
    sections = []

    if triggered:
        sections.append(
            "<h2 style='color:#1a73e8;margin-bottom:12px'>New RTT Arbitrage Opportunity</h2>"
            + _html_table(_render_table_rows(triggered))
        )

    if new_listings:
        def _margin_str(m):
            if m is None:
                return "—"
            color = "#1e8e3e" if m >= 0 else "#c0392b"
            return f"<span style='color:{color}'>{m:.1%}</span>"

        def _get_in_str(r):
            g = r.get("get_in_price")
            return f"${g:,.0f}" if g else "—"

        new_rows = "".join(
            f"<tr style='border-bottom:1px solid #e0e0e0'>"
            f"<td style='padding:6px'>{r['match_key']}</td>"
            f"<td style='padding:6px;text-align:center'>Cat {r['category']}</td>"
            f"<td style='padding:6px;text-align:right'>${r['rtt_price']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right'>{_get_in_str(r)}</td>"
            f"<td style='padding:6px;text-align:right'>{_margin_str(r.get('profit_margin'))}</td>"
            f"</tr>"
            for r in sorted(new_listings, key=lambda x: x["match_key"])
        )
        new_header = (
            "<tr>"
            "<th style='text-align:left;padding:6px'>Match</th>"
            "<th style='padding:6px'>Cat</th>"
            "<th style='padding:6px'>RTT Price</th>"
            "<th style='padding:6px'>Get-In</th>"
            "<th style='padding:6px'>Margin</th>"
            "</tr>"
        )
        new_table = (
            "<table style='border-collapse:collapse;width:100%'>"
            f"<thead style='background:#f1f3f4'>{new_header}</thead>"
            f"<tbody>{new_rows}</tbody>"
            "</table>"
        )
        sections.append(
            "<h3 style='color:#555;margin:24px 0 8px'>New Supply</h3>"
            "<p style='color:#888;font-size:12px;margin-bottom:8px'>"
            "First-time listings — not yet profitable</p>"
            + new_table
        )

    if removed:
        removed_rows = "".join(
            f"<tr style='border-bottom:1px solid #e0e0e0'>"
            f"<td style='padding:6px'>{r['match_key']}</td>"
            f"<td style='padding:6px;text-align:center'>Cat {r['category']}</td>"
            f"<td style='padding:6px;text-align:right'>${r['last_price']:,.0f}</td>"
            f"</tr>"
            for r in sorted(removed, key=lambda x: x["match_key"])
        )
        removed_header = (
            "<tr>"
            "<th style='text-align:left;padding:6px'>Match</th>"
            "<th style='padding:6px'>Cat</th>"
            "<th style='padding:6px'>Last price</th>"
            "</tr>"
        )
        removed_table = (
            "<table style='border-collapse:collapse;width:100%'>"
            f"<thead style='background:#f1f3f4'>{removed_header}</thead>"
            f"<tbody>{removed_rows}</tbody>"
            "</table>"
        )
        sections.append(
            "<h3 style='color:#555;margin:24px 0 8px'>Marketplace Activity</h3>"
            "<p style='color:#888;font-size:12px;margin-bottom:8px'>"
            "Removed or sold since last run — demand signal</p>"
            + removed_table
        )

    if viagogo_drops:
        drop_rows = ""
        for d in viagogo_drops:
            prev = f"${d['previous_price']:,.0f}" if d.get("previous_price") else "first alert"
            drop_rows += (
                f"<tr style='border-bottom:1px solid #e0e0e0'>"
                f"<td style='padding:8px;font-size:20px;font-weight:bold;color:#e67e22'>"
                f"${d['current_price']:,.0f}</td>"
                f"<td style='padding:8px;color:#888'>{prev}</td>"
                f"<td style='padding:8px;color:#888'>${d['threshold']:,.0f}</td>"
                f"<td style='padding:8px'>"
                f"<a href='{d['url']}' style='color:#1a73e8'>View on Viagogo</a></td>"
                f"</tr>"
            )
        drop_table = (
            "<table style='border-collapse:collapse;width:100%'>"
            "<thead style='background:#fef3e2'>"
            "<tr>"
            "<th style='text-align:left;padding:8px'>Current Price</th>"
            "<th style='padding:8px'>Previous Alert</th>"
            "<th style='padding:8px'>Threshold</th>"
            "<th style='padding:8px'>Link</th>"
            "</tr></thead>"
            f"<tbody>{drop_rows}</tbody>"
            "</table>"
        )
        sections.append(
            "<h2 style='color:#e67e22;margin:24px 0 4px'>Argentina Price Drop</h2>"
            "<p style='color:#888;font-size:12px;margin-bottom:8px'>"
            "Match 95 &nbsp;&middot;&nbsp; Jul 7 &nbsp;&middot;&nbsp; "
            "Mercedes-Benz Stadium, Atlanta</p>"
            + drop_table
        )

    body = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">
  {body}
  <p style="color:#888; font-size:12px; margin-top:24px;">
    Profit = (get-in &times; 0.80 &minus; RTT price) &divide; RTT price &nbsp;|&nbsp;
    Threshold: 5% or +$300 &nbsp;|&nbsp;
    <a href="https://collect.fifa.com/right-to-ticket">FIFA RTT Marketplace</a>
  </p>
</body>
</html>
"""


def _html_table(rows: str) -> str:
    header = (
        "<tr>"
        "<th style='text-align:left;padding:6px'>Match</th>"
        "<th style='padding:6px'>RTT Min</th>"
        "<th style='padding:6px'>Get-In</th>"
        "<th style='padding:6px'>Seller Net</th>"
        "<th style='padding:6px'>Profit $</th>"
        "<th style='padding:6px'>Profit %</th>"
        "<th style='padding:6px'>Cat</th>"
        "</tr>"
    )
    return (
        "<table style='border-collapse:collapse;width:100%'>"
        f"<thead style='background:#f1f3f4'>{header}</thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _render_table_rows(listings: list[dict]) -> str:
    rows = []
    for t in sorted(listings, key=lambda x: -x["profit_margin"]):
        color = "#1e8e3e" if t["profit_margin"] >= 0.20 else "#188038"
        profit_d = t.get("profit_dollars", t["seller_net"] - t["rtt_price"])
        profit_str = f"+${profit_d:,.0f}" if profit_d >= 0 else f"-${abs(profit_d):,.0f}"
        rows.append(
            f"<tr style='border-bottom:1px solid #e0e0e0'>"
            f"<td style='padding:6px'>{t['match_key']}</td>"
            f"<td style='padding:6px;text-align:right'>${t['rtt_price']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right'>${t['get_in_price']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right'>${t['seller_net']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right;color:{color};font-weight:bold'>{profit_str}</td>"
            f"<td style='padding:6px;text-align:right;color:{color};font-weight:bold'>"
            f"{t['profit_margin']:.1%}</td>"
            f"<td style='padding:6px;text-align:center'>{t.get('category','?')}</td>"
            f"</tr>"
        )
    return "".join(rows)


def _build_text_body(triggered: list[dict], all_profitable: list[dict], removed: list[dict] = [], new_listings: list[dict] = [], viagogo_drops: list[dict] = []) -> str:
    lines = []

    if triggered:
        lines.append("=== NEW RTT ARBITRAGE ALERT ===\n")
        for t in sorted(triggered, key=lambda x: -x["profit_margin"]):
            lines.append(
                f"  {t['match_key']} | Cat {t.get('category','?')} | "
                f"RTT ${t['rtt_price']:,.0f} | Get-in ${t['get_in_price']:,.0f} | "
                f"Seller net ${t['seller_net']:,.0f} | "
                f"Profit +${t.get('profit_dollars', 0):,.0f} / {t['profit_margin']:.1%}"
            )

    if new_listings:
        lines.append("\n=== NEW SUPPLY (first-time listings, not yet profitable) ===\n")
        for r in sorted(new_listings, key=lambda x: x["match_key"]):
            get_in_str = f"${r['get_in_price']:,.0f}" if r.get("get_in_price") else "—"
            margin_str = f"{r['profit_margin']:.1%}" if r.get("profit_margin") is not None else "—"
            lines.append(
                f"  {r['match_key']} | Cat {r['category']} | "
                f"RTT ${r['rtt_price']:,.0f} | Get-in {get_in_str} | Margin {margin_str}"
            )

    if removed:
        lines.append("\n=== MARKETPLACE ACTIVITY (removed/sold since last run) ===\n")
        for r in sorted(removed, key=lambda x: x["match_key"]):
            lines.append(f"  {r['match_key']} | Cat {r['category']} | Last price ${r['last_price']:,.0f}")

    if viagogo_drops:
        lines.append("\n=== ARGENTINA PRICE DROP (Match 95 · Jul 7 · Atlanta) ===\n")
        for d in viagogo_drops:
            prev = f"${d['previous_price']:,.0f}" if d.get("previous_price") else "first alert"
            lines.append(f"  Current cheapest: ${d['current_price']:,.0f}  (was {prev} last alert)")
            lines.append(f"  Threshold: ${d['threshold']:,.0f}")
            lines.append(f"  Link: {d['url']}")

    lines.append("\nFormula: profit = (get-in * 0.80 - RTT price) / RTT price")
    lines.append("FIFA RTT: https://collect.fifa.com/right-to-ticket")
    return "\n".join(lines)
