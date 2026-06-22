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
) -> None:
    if not triggered_listings and not removed_listings:
        return

    subject = _build_subject(triggered_listings, removed_listings)
    html_body = _build_html_body(triggered_listings, all_profitable_listings, removed_listings)
    text_body = _build_text_body(triggered_listings, all_profitable_listings, removed_listings)

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


def _build_subject(triggered: list[dict], removed: list[dict] = []) -> str:
    if triggered and len(triggered) == 1:
        t = triggered[0]
        suffix = f" | {len(removed)} removed" if removed else ""
        return (
            f"RTT Alert: {t['match_key']} | "
            f"{t['profit_margin']:.0%} profit | "
            f"RTT ${t['rtt_price']:,.0f} vs ${t['get_in_price']:,.0f} get-in{suffix}"
        )
    if triggered:
        suffix = f" | {len(removed)} removed" if removed else ""
        return (
            f"RTT Alert: {len(triggered)} new opportunities | "
            f"Best: {max(t['profit_margin'] for t in triggered):.0%} profit{suffix}"
        )
    return f"RTT Activity: {len(removed)} listing(s) removed from marketplace"


def _build_html_body(triggered: list[dict], all_profitable: list[dict], removed: list[dict] = []) -> str:
    sections = []

    if triggered:
        sections.append(
            "<h2 style='color:#1a73e8;margin-bottom:12px'>New RTT Arbitrage Opportunity</h2>"
            + _html_table(_render_table_rows(triggered))
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

    body = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">
  {body}
  <p style="color:#888; font-size:12px; margin-top:24px;">
    Profit = (get-in &divide; 1.20 &times; 0.90 &minus; RTT price) &divide; RTT price &nbsp;|&nbsp;
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
        rows.append(
            f"<tr style='border-bottom:1px solid #e0e0e0'>"
            f"<td style='padding:6px'>{t['match_key']}</td>"
            f"<td style='padding:6px;text-align:right'>${t['rtt_price']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right'>${t['get_in_price']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right'>${t['seller_net']:,.0f}</td>"
            f"<td style='padding:6px;text-align:right;color:{color};font-weight:bold'>"
            f"{t['profit_margin']:.1%}</td>"
            f"<td style='padding:6px;text-align:center'>{t.get('category','?')}</td>"
            f"</tr>"
        )
    return "".join(rows)


def _build_text_body(triggered: list[dict], all_profitable: list[dict], removed: list[dict] = []) -> str:
    lines = []

    if triggered:
        lines.append("=== NEW RTT ARBITRAGE ALERT ===\n")
        for t in sorted(triggered, key=lambda x: -x["profit_margin"]):
            lines.append(
                f"  {t['match_key']} | Cat {t.get('category','?')} | "
                f"RTT ${t['rtt_price']:,.0f} | Get-in ${t['get_in_price']:,.0f} | "
                f"Seller net ${t['seller_net']:,.0f} | Profit {t['profit_margin']:.1%}"
            )

    if removed:
        lines.append("\n=== MARKETPLACE ACTIVITY (removed/sold since last run) ===\n")
        for r in sorted(removed, key=lambda x: x["match_key"]):
            lines.append(f"  {r['match_key']} | Cat {r['category']} | Last price ${r['last_price']:,.0f}")

    lines.append("\nFormula: profit = (get-in / 1.20 * 0.90 - RTT price) / RTT price")
    lines.append("FIFA RTT: https://collect.fifa.com/right-to-ticket")
    return "\n".join(lines)
