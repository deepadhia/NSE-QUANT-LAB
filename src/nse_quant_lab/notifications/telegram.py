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
from dotenv import load_dotenv

# Ensure environment variables loaded
load_dotenv()

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
        active_holdings: Optional[List[Dict[str, Any]]] = None,
        data_asof: Optional[str] = None,
        notify_empty: bool = False,
    ) -> bool:
        """
        Broadcasts post-market scan alert (15:45 IST) in Alpha-Pulse institutional format.
        Zero-Spam Policy: Does not send message if there are no new buys, no exits, and no positions near stop unless notify_empty=True.
        """
        near_stop_holdings = [h for h in (active_holdings or []) if h.get("stop_buffer_pct") is not None and h["stop_buffer_pct"] <= 2.5]
        if not buys and not exits and not near_stop_holdings and not notify_empty:
            logger.info("Zero new signals and no positions near stop. Suppressing Telegram alert to prevent spam.")
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
                sh_str = f" ({ex['shares']} sh @ ₹{ex['close']:,.2f})" if ex.get("shares") else ""
                lines.append(f"  🔻 <b>{e(ex['symbol'])}</b>{sh_str}: {e(reason)} -> <i>Sell at next Open</i>")
        else:
            lines.append("<i>No structural exits triggered.</i>")

        lines.append("")
        if buys:
            lines.append(f"<b>🟢 RESTING BUY ORDERS ({len(buys)}):</b>")
            for b in buys:
                alloc = b.get("alloc", 0)
                reason = b.get("reason", "RANK")
                details = f"~{b['est_shares']} sh | Close ₹{b['close']:,.2f} | Max Limit ₹{b['limit_price']:,.2f}" if b.get("close") else f"Target ₹{alloc:,.0f}"
                lines.append(f"  🟢 <b>{e(b['symbol'])}</b>: {details} ({e(str(reason))})")
        else:
            lines.append("<i>No new buy orders queued.</i>")

        if active_holdings:
            lines.extend(["", f"<b>🛡️ ACTIVE HOLDINGS & TRAILING STOPS ({len(active_holdings)}):</b>"])
            for h in active_holdings:
                sym = h["symbol"]
                c_px = h.get("close", 0.0)
                sh = h.get("shares", 0)
                stop_px = h.get("stop_loss_price")
                buf = h.get("stop_buffer_pct")
                if stop_px is not None:
                    buf_str = f"({buf:+.1f}% cushion)" if buf is not None else ""
                    warn = " ⚠️ <b>NEAR STOP</b>" if buf is not None and buf <= 2.5 else ""
                    lines.append(f"  🔹 <b>{e(sym)}</b>: {sh} sh @ ₹{c_px:,.2f} | Stop ₹{stop_px:,.2f} {buf_str}{warn}")
                else:
                    lines.append(f"  🔹 <b>{e(sym)}</b>: {sh} sh @ ₹{c_px:,.2f}")

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
            lines.extend(["", f"<b>⚠️ DEFERRALS & SKIPPED ORDERS ({len(deferred)}):</b>"])
            for d in deferred:
                if "reason" in d:
                    lines.append(f"  ⚠️ <b>{e(d['symbol'])}</b>: {e(d['reason'])}")
                else:
                    days = d.get("days", 1)
                    lines.append(f"  ⚠️ <b>{e(d['symbol'])}</b>: Lower circuit locked (deferral day {days})")

        lines.extend([
            "",
            f"<b>Portfolio NAV:</b> ₹{nav:,.2f}" + (f"  |  <b>Cash:</b> ₹{cash:,.2f}" if cash is not None else ""),
        ])
        return self.send_message("\n".join(lines))

    def send_weekly_summary(
        self,
        strategy_id: str,
        start_date: str,
        end_date: str,
        start_nav: float,
        end_nav: float,
        cash: float,
        holdings_val: float,
        open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
    ) -> bool:
        """
        Broadcasts weekly performance digest (e.g. Friday post-market).
        """
        e = html.escape
        weekly_ret_pct = ((end_nav - start_nav) / start_nav * 100.0) if start_nav > 0 else 0.0
        ret_badge = "🟢" if weekly_ret_pct >= 0 else "🔴"
        ret_sign = "+" if weekly_ret_pct >= 0 else ""

        lines = [
            f"<b>📊 NSE-QUANT-LAB | WEEKLY DIGEST</b>",
            f"<b>Strategy:</b> <code>{e(strategy_id)}</code>  |  <b>Week:</b> {e(start_date)} to {e(end_date)}",
            "",
            f"<b>Portfolio NAV:</b> ₹{end_nav:,.2f}  ({ret_badge} <b>{ret_sign}{weekly_ret_pct:.2f}% this week</b>)",
            f"<b>Cash:</b> ₹{cash:,.2f}  |  <b>Holdings Value:</b> ₹{holdings_val:,.2f}",
            f"<b>Active Positions:</b> {len(open_positions)}",
        ]

        if closed_trades:
            total_net_pnl = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades)
            wins = sum(1 for t in closed_trades if float(t.get("net_pnl", 0.0)) > 0)
            losses = len(closed_trades) - wins
            pnl_sign = "+" if total_net_pnl >= 0 else ""
            lines.extend([
                "",
                f"<b>🏁 Trades Closed This Week ({len(closed_trades)}):</b>",
                f"  Win / Loss: {wins}W / {losses}L  |  Realized Net: <b>{pnl_sign}₹{total_net_pnl:,.2f}</b>",
            ])
            for t in closed_trades[:5]:
                sym = t.get("symbol", "")
                raw_ret = float(t.get("return_pct", 0.0))
                ret = raw_ret * 100.0 if abs(raw_ret) < 5.0 else raw_ret
                r_badge = "🟢" if ret >= 0 else "🔴"
                r_sign = "+" if ret >= 0 else ""
                lines.append(f"  {r_badge} <b>{e(sym)}</b>: {r_sign}{ret:.2f}% ({e(t.get('exit_reason', 'EXIT'))})")
        else:
            lines.extend(["", "<i>No closed trades this week.</i>"])

        if open_positions:
            lines.extend(["", f"<b>🛡️ Current Holdings ({len(open_positions)}):</b>"])
            for p in open_positions[:6]:
                sym = p.get("symbol", "")
                entry_px = float(p.get("entry_price", 1.0))
                last_pr = float(p.get("last_close", entry_px))
                unreal_pct = ((last_pr - entry_px) / entry_px) * 100.0 if entry_px > 0 else 0.0
                u_badge = "🟢" if unreal_pct >= 0 else "🔴"
                u_sign = "+" if unreal_pct >= 0 else ""
                stop_str = f" | Stop ₹{p['stop_loss_price']:,.2f}" if p.get("stop_loss_price") else ""
                lines.append(f"  {u_badge} <b>{e(sym)}</b>: {u_sign}{unreal_pct:.1f}%{stop_str}")
            if len(open_positions) > 6:
                lines.append(f"  <i>...and {len(open_positions) - 6} more positions.</i>")

        return self.send_message("\n".join(lines))

    def send_monthly_summary(
        self,
        strategy_id: str,
        month_str: str,
        start_nav: float,
        end_nav: float,
        initial_capital: float,
        cash: float,
        holdings_val: float,
        closed_trades: List[Dict[str, Any]],
        ytd_ret_pct: float,
        benchmark_ret_pct: Optional[float] = None,
    ) -> bool:
        """
        Broadcasts monthly performance & rebalance report.
        """
        e = html.escape
        month_ret_pct = ((end_nav - start_nav) / start_nav * 100.0) if start_nav > 0 else 0.0
        tot_ret_pct = ((end_nav - initial_capital) / initial_capital * 100.0) if initial_capital > 0 else 0.0
        m_badge = "🟢" if month_ret_pct >= 0 else "🔴"
        m_sign = "+" if month_ret_pct >= 0 else ""
        t_sign = "+" if tot_ret_pct >= 0 else ""
        ytd_sign = "+" if ytd_ret_pct >= 0 else ""

        lines = [
            f"<b>🏆 NSE-QUANT-LAB | MONTHLY PERFORMANCE REPORT</b>",
            f"<b>Strategy:</b> <code>{e(strategy_id)}</code>  |  <b>Month:</b> {e(month_str)}",
            "",
            f"<b>Monthly Return:</b> {m_badge} <b>{m_sign}{month_ret_pct:.2f}%</b>",
            f"<b>YTD Return:</b> <b>{ytd_sign}{ytd_ret_pct:.2f}%</b>  |  <b>Total Cumulative:</b> <b>{t_sign}{tot_ret_pct:.2f}%</b>",
            f"<b>Portfolio NAV:</b> ₹{end_nav:,.2f}  |  <b>Cash:</b> ₹{cash:,.2f}",
        ]
        if benchmark_ret_pct is not None:
            b_sign = "+" if benchmark_ret_pct >= 0 else ""
            alpha = month_ret_pct - benchmark_ret_pct
            a_sign = "+" if alpha >= 0 else ""
            lines.append(f"<b>Nifty 500:</b> {b_sign}{benchmark_ret_pct:.2f}%  |  <b>Alpha:</b> {a_sign}{alpha:.2f}%")

        if closed_trades:
            wins = sum(1 for t in closed_trades if float(t.get("net_pnl", 0.0)) > 0)
            losses = len(closed_trades) - wins
            win_rate = (wins / len(closed_trades) * 100.0) if closed_trades else 0.0
            tot_net = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades)
            net_sign = "+" if tot_net >= 0 else ""
            lines.extend([
                "",
                f"<b>📊 Monthly Trade Statistics:</b>",
                f"  Closed Trades: {len(closed_trades)} ({wins}W / {losses}L)",
                f"  Win Rate: <b>{win_rate:.1f}%</b>  |  Net P&L: <b>{net_sign}₹{tot_net:,.2f}</b>",
            ])
        else:
            lines.extend(["", "<i>No trades closed this month.</i>"])

        lines.extend([
            "",
            "<i>Monthly rebalance completed. Next rebalance scheduled for next month-end.</i>",
        ])
        return self.send_message("\n".join(lines))
