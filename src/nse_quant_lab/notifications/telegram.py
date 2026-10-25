"""
telegram.py -- Institutional Telegram Alerting Module (Adapted from Alpha-Pulse Pipeline)
"""

from __future__ import annotations
import os
import time
import html
import logging
from typing import Optional, List, Dict, Any
import urllib.request
import urllib.parse

logger = logging.getLogger("telegram_alerts")


def chunk_telegram_text(text: str, max_chars: int = 4000) -> List[str]:
    """Splits long text into chunks adhering to Telegram 4096-char message limits."""
    if len(text) <= max_chars:
        return [text]
    lines = text.split("\n")
    chunks = []
    curr = []
    curr_len = 0
    for line in lines:
        if curr_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(curr))
            curr = [line]
            curr_len = len(line) + 1
        else:
            curr.append(line)
            curr_len += len(line) + 1
    if curr:
        chunks.append("\n".join(curr))
    return chunks


class TelegramNotifier:
    """
    Manages Telegram bot alerts for daily quantitative signals and paper executions.
    Features:
    - Pure HTML formatting with automatic string escaping
    - Long-message chunking (safe for 4096 character limits)
    - Data freshness validation line
    - Actionable stop alerts and pending rebalance buy orders
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        env_enabled = os.environ.get("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
        self.enabled = enabled if enabled is not None else env_enabled

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Sends text message (chunked if long) to configured Telegram chat."""
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.info(f"[TELEGRAM DISABLED / MOCK] Message:\n{text}")
            return False

        chunks = chunk_telegram_text(text, max_chars=4000)
        all_success = True

        for chunk in chunks:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            data = urllib.parse.urlencode(payload).encode("utf-8")

            try:
                req = urllib.request.Request(url, data=data, method="POST")
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status != 200:
                        all_success = False
            except Exception as e:
                logger.error(f"Failed to send Telegram alert chunk: {e}")
                all_success = False
            time.sleep(0.05)  # Throttle between chunks

        return all_success

    def send_signal_alert(
        self,
        strategy_id: str,
        date_str: str,
        buys: List[Dict[str, Any]],
        exits: List[Dict[str, Any]],
        data_asof: Optional[str] = None,
        notify_empty: bool = False,
    ) -> bool:
        """
        Broadcasts post-market scan alert (15:45 IST) in Alpha-Pulse institutional format.
        Zero-Spam Policy: Does not send message if there are no new buys and no exits unless notify_empty=True.
        """
        if not buys and not exits and not notify_empty:
            logger.info("Zero new buy or exit signals today. Suppressing Telegram alert to prevent spam.")
            return False
        e = html.escape
        asof = data_asof or date_str
        lines = [
            f"<b>🔔 NSE-QUANT-LAB | PAPER PIPELINE</b>",
            f"<b>Strategy:</b> <code>{e(strategy_id)}</code>  |  <b>Orders for Next Open</b>",
            f"<i>Data as of {e(asof)}</i>",
            "",
        ]

        if exits:
            lines.append(f"<b>🛑 STRUCTURAL EXITS ({len(exits)}):</b>")
            for ex in exits:
                reason = ex.get("reason", "EXIT_SIGNAL")
                lines.append(f"  🔻 <b>{e(ex['symbol'])}</b>: Triggered {e(reason)} -> <i>Exit at next Open</i>")
        else:
            lines.append("<i>No structural exits triggered.</i>")

        lines.append("")
        if buys:
            lines.append(f"<b>🟢 RESTING BUY ORDERS ({len(buys)}):</b>")
            for b in buys:
                alloc = b.get("alloc", 0)
                reason = b.get("reason", "RANK")
                lines.append(f"  🟢 <b>{e(b['symbol'])}</b>: Sizing Target ₹{alloc:,.0f} ({e(str(reason))}) -> <i>Buy at next Open</i>")
        else:
            lines.append("<i>No new buy orders queued.</i>")

        lines.extend([
            "",
            "<i>Orders logged to MongoDB (mode: 'paper', status: 'PENDING_OPEN').</i>",
        ])
        return self.send_message("\n".join(lines))

    def send_execution_alert(
        self,
        strategy_id: str,
        date_str: str,
        fills: List[Dict[str, Any]],
        deferred: List[Dict[str, Any]],
        nav: float,
        cash: Optional[float] = None,
        notify_empty: bool = False,
    ) -> bool:
        """
        Broadcasts morning open execution alert (09:16 IST) in Alpha-Pulse institutional format.
        Zero-Spam Policy: Does not send message if there are no fills and no circuit deferrals unless notify_empty=True.
        """
        if not fills and not deferred and not notify_empty:
            logger.info("Zero order fills or deferrals today. Suppressing Telegram alert to prevent spam.")
            return False
        e = html.escape
        lines = [
            f"<b>📈 NSE-QUANT-LAB | MORNING EXECUTIONS</b>",
            f"<b>Strategy:</b> <code>{e(strategy_id)}</code>  |  <b>Date:</b> {e(date_str)}",
            "",
        ]

        if fills:
            lines.append(f"<b>✅ EXECUTED FILLS ({len(fills)}):</b>")
            for f in fills:
                action = f.get("action", "EXEC")
                badge = "🟢" if action == "BUY" else "🔴"
                lines.append(f"  {badge} <b>{e(action)} {e(f['symbol'])}</b>: {f['shares']} sh @ ₹{f['fill_price']:,.2f}")
        else:
            lines.append("<i>No orders filled this session.</i>")

        if deferred:
            lines.extend(["", f"<b>⚠️ CIRCUIT DEFERRALS ({len(deferred)}):</b>"])
            for d in deferred:
                days = d.get("days", 1)
                lines.append(f"  ⚠️ <b>{e(d['symbol'])}</b>: Lower circuit locked (deferral day {days})")

        lines.extend([
            "",
            f"<b>Portfolio NAV:</b> ₹{nav:,.2f}" + (f"  |  <b>Cash:</b> ₹{cash:,.2f}" if cash is not None else ""),
        ])
        return self.send_message("\n".join(lines))
