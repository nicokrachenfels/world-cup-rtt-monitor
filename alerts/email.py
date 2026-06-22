"""
Send RTT arbitrage alerts via Gmail SMTP.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587


def send_alert(
    gmail_user: str,
    gmail_app_password: str,
    to_email: str,
    triggered_listings: list[dict],
    all_profitable_listings: list[dict],
) -> None:
    """
    Send an email alert for new profitable RTT listings.

    triggered_listings: the specific new/lower-priced listings that fired the alert
    all_profitable_listings: all currently profitable listings (for context)
    """
    if not triggered_listings:
        return

    subject = _build_subject(triggered_listings)
    html_body = _build_html_body(triggered_listings, all_profitable_listings)
    text_body = _build_text_body(triggered_listings, all_profitable_listings)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(gmail_user, gmail_app_password)
            server.sendmail(gmail_user, to_email, msg.as_string())
        logger.info(f"Alert sent to {to_email}: {subject}")
    except smtplib.SMTPException as e:
        logger.error(f"Failed to send email alert: {e}", exc_info=True)
        raise


def _build_subject(triggered: list[dict]) -> str:
    if len(triggered) == 1:
        t = triggered[0]
        return (
            f"RTT Alert: {t['match_key']} | "
            f"{t['profit_margin']:.0%} profit | "
            f"RTT ${t['rtt_price']:,.0f} vs ${t['get_in_price']:,.0f} get-in"
        )
    return (
        f"RTT Alert: {len(triggered)} new opportunities | "
        f"Best: {max(t['profit_margin'] for t in triggered):.0%} profit"
    )


def _build_html_body(triggered: list[dict], all_profitable: list[dict]) -> str:
    rows_triggered = _render_table_rows(triggered)
    rows_all = _render_table_rows(all_profitable)

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">
  <h2 style="color: #1a73e8;">New RTT Arbitrage Opportunity</h2>

  <h3>Triggered Alert(s)</h3>
  {_html_table(rows_triggered)}

  <h3 style="margin-top:24px;">All Currently Profitable Matches</h3>
  {_html_table(rows_all)}

  <p style="color:#888; font-size:12px; margin-top:24px;">
    Profit = (get-in ÷ 1.20 × 0.90 − RTT price) ÷ RTT price &nbsp;|&nbsp;
    Threshold: 10% &nbsp;|&nbsp;
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


def _build_text_body(triggered: list[dict], all_profitable: list[dict]) -> str:
    lines = ["=== NEW RTT ARBITRAGE ALERT ===\n"]

    lines.append("TRIGGERED:")
    for t in sorted(triggered, key=lambda x: -x["profit_margin"]):
        lines.append(
            f"  {t['match_key']} | Cat {t.get('category','?')} | "
            f"RTT ${t['rtt_price']:,.0f} | Get-in ${t['get_in_price']:,.0f} | "
            f"Seller net ${t['seller_net']:,.0f} | Profit {t['profit_margin']:.1%}"
        )

    if all_profitable:
        lines.append("\nALL PROFITABLE MATCHES:")
        for t in sorted(all_profitable, key=lambda x: -x["profit_margin"]):
            lines.append(
                f"  {t['match_key']} | Cat {t.get('category','?')} | "
                f"RTT ${t['rtt_price']:,.0f} | Get-in ${t['get_in_price']:,.0f} | "
                f"Profit {t['profit_margin']:.1%}"
            )

    lines.append("\nFormula: profit = (get-in / 1.20 * 0.90 - RTT price) / RTT price")
    lines.append("FIFA RTT: https://collect.fifa.com/right-to-ticket")
    return "\n".join(lines)
