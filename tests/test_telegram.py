"""
test_telegram.py -- Tests for Institutional Telegram Alerting and Order Ticket Formatting
"""

import html
import urllib.parse
from unittest.mock import MagicMock, patch
from nse_quant_lab.notifications.telegram import TelegramNotifier, chunk_telegram_text


def test_telegram_mock_and_formatting():
    notifier = TelegramNotifier(bot_token=None, chat_id=None, enabled=False)
    # Should safely return False without raising exceptions
    assert not notifier.send_message("Test message")

    buys = [{"symbol": "INFY.NS", "alloc": 100000}]
    exits = [{"symbol": "TCS.NS", "reason": "STOP_LOSS"}]
    res = notifier.send_signal_alert("trend_momentum", "2026-10-24", buys, exits)
    assert res is False


def test_telegram_traceback_escaping():
    """Verify tracebacks containing <module>, <stdin>, and special chars escape safely for HTML mode."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)
    raw_traceback = 'Traceback (most recent call last):\n  File "<stdin>", line 1, in <module>\nKeyError: <class \'str\'> & > 5'
    escaped = html.escape(raw_traceback)

    # Verify no raw angle brackets or unescaped ampersands remain
    assert "<module>" not in escaped
    assert "&lt;module&gt;" in escaped
    assert "&lt;stdin&gt;" in escaped
    assert "&amp;" in escaped

    # Verify message formatting matches daemon fail_msg
    job_id = "test_job"
    fail_msg = (
        f"🚨 <b>ALERT: Job Failure in NSE Quant Lab</b>\n"
        f"Job: <code>{html.escape(job_id)}</code>\n"
        f"Exit Code: <code>1</code>\n"
        f"Error Snippet:\n<pre>{escaped}</pre>"
    )

    with patch("urllib.request.urlopen") as mock_url:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_url.return_value.__enter__.return_value = mock_response

        ok = notifier.send_message(fail_msg)
        assert ok is True
        # Check payload sent to urllib
        call_args = mock_url.call_args[0][0]
        payload = call_args.data.decode("utf-8")
        assert "parse_mode=HTML" in payload
        assert "lt%3Bmodule%3Bgt" in payload or "&lt;module&gt;" in urllib.parse.unquote(payload)


def test_send_signal_alert_ticket_formatting():
    """Verify 15:45 IST post-market alert formats broker-ready order tickets with shares, close, and limit price."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)

    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    notifier.send_message = mock_send

    exits = [
        {"symbol": "TCS.NS", "shares": 50, "close": 3520.50, "reason": "Supertrend(10,3)"},
        {"symbol": "RELIANCE.NS", "reason": "STOP_LOSS"},  # Fallback test without shares/close
    ]
    buys = [
        {
            "symbol": "INFY.NS",
            "alloc": 100000.0,
            "close": 1750.00,
            "limit_price": 1785.00,
            "est_shares": 57,
            "reason": "RANK_1",
        },
        {"symbol": "HDFCBANK.NS", "alloc": 80000.0, "reason": "RANK_2"},  # Fallback test without close
    ]

    ok = notifier.send_signal_alert("trend_momentum", "2026-10-24", buys, exits)
    assert ok is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]

    # Verify exit details
    assert "TCS.NS" in msg
    assert "(50 sh @ ₹3,520.50)" in msg
    assert "Supertrend(10,3) -> <i>Sell at next Open</i>" in msg
    assert "RELIANCE.NS" in msg
    assert "STOP_LOSS -> <i>Sell at next Open</i>" in msg

    # Verify buy details
    assert "INFY.NS" in msg
    assert "~57 sh | Close ₹1,750.00 | Max Limit ₹1,785.00 (RANK_1)" in msg
    assert "HDFCBANK.NS" in msg
    assert "Target ₹80,000 (RANK_2)" in msg


def test_send_execution_alert_deferred_and_skipped_orders():
    """Verify 09:16 IST morning execution alert renders skipped gap/circuit orders clearly."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)

    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    notifier.send_message = mock_send

    fills = [{"action": "BUY", "symbol": "INFY.NS", "shares": 57, "fill_price": 1752.00}]
    reason_gap = "Skipped: Open ₹1,050.00 > +2% Cap ₹1,020.00"
    deferred = [
        {"symbol": "TATAMOTORS.NS", "reason": reason_gap},
        {"symbol": "TITAN.NS", "reason": "Skipped: Upper Circuit Lock (Open ₹3,400.00)"},
        {"symbol": "BEL.NS", "days": 2},  # Lower circuit exit deferral
    ]

    ok = notifier.send_execution_alert(
        strategy_id="trend_momentum",
        date_str="2026-10-25",
        fills=fills,
        deferred=deferred,
        nav=1050000.0,
        cash=950000.0,
    )
    assert ok is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]

    assert "⚠️ DEFERRALS & SKIPPED ORDERS (3):" in msg
    assert "TATAMOTORS.NS" in msg
    # Verify that reason string with '>' was safely escaped to '&gt;' in the HTML output
    assert html.escape(reason_gap) in msg
    assert "TITAN.NS" in msg
    assert "Skipped: Upper Circuit Lock (Open ₹3,400.00)" in msg
    assert "BEL.NS" in msg
    assert "Lower circuit locked (deferral day 2)" in msg
    assert "<b>Portfolio NAV:</b> ₹1,050,000.00  |  <b>Cash:</b> ₹950,000.00" in msg


def test_chunking_utility():
    """Verify message chunking respects character boundary."""
    long_text = "\n".join([f"Line {i}: Some dummy content for telegram alert test" for i in range(200)])
    chunks = chunk_telegram_text(long_text, max_chars=500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 500


def test_send_signal_alert_active_holdings_and_near_stop():
    """Verify 15:45 IST alert renders active trailing stops and triggers risk warning when near stop."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)

    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    notifier.send_message = mock_send

    active_holdings = [
        {"symbol": "INFY.NS", "shares": 50, "close": 1850.00, "stop_loss_price": 1780.00, "stop_buffer_pct": 3.78},
        {"symbol": "TATAMOTORS.NS", "shares": 100, "close": 960.00, "stop_loss_price": 945.00, "stop_buffer_pct": 1.56},  # <= 2.5% NEAR STOP
    ]

    # Even with 0 buys and 0 exits, NEAR STOP condition triggers alert (Zero-Spam exception)
    ok = notifier.send_signal_alert(
        strategy_id="trend_momentum",
        date_str="2026-10-25",
        buys=[],
        exits=[],
        active_holdings=active_holdings,
    )
    assert ok is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]

    assert "🛡️ ACTIVE HOLDINGS & TRAILING STOPS (2):" in msg
    assert "INFY.NS" in msg
    assert "Stop ₹1,780.00 (+3.8% cushion)" in msg
    assert "TATAMOTORS.NS" in msg
    assert "Stop ₹945.00 (+1.6% cushion)" in msg
    assert "<b>NEAR STOP</b>" in msg


def test_send_weekly_summary_formatting():
    """Verify weekly performance digest formats accurately."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)

    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    notifier.send_message = mock_send

    open_positions = [
        {"symbol": "INFY.NS", "entry_price": 1700.0, "last_close": 1750.0, "stop_loss_price": 1680.0},
    ]
    closed_trades = [
        {"symbol": "TCS.NS", "net_pnl": 5200.0, "return_pct": 0.052, "exit_reason": "REBALANCE_DROP"},
        {"symbol": "WIPRO.NS", "net_pnl": -1200.0, "return_pct": -0.025, "exit_reason": "STRUCTURAL_SUPERTREND"},
    ]

    ok = notifier.send_weekly_summary(
        strategy_id="trend_momentum",
        start_date="2026-10-19",
        end_date="2026-10-23",
        start_nav=1000000.0,
        end_nav=1025000.0,
        cash=250000.0,
        holdings_val=775000.0,
        open_positions=open_positions,
        closed_trades=closed_trades,
    )
    assert ok is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]

    assert "📊 NSE-QUANT-LAB | WEEKLY DIGEST" in msg
    assert "Week:</b> 2026-10-19 to 2026-10-23" in msg
    assert "+2.50% this week" in msg
    assert "Trades Closed This Week (2):" in msg
    assert "1W / 1L" in msg
    assert "+₹4,000.00" in msg
    assert "INFY.NS" in msg
    assert "+2.9%" in msg


def test_send_monthly_summary_formatting():
    """Verify monthly performance report formats accurately."""
    notifier = TelegramNotifier(bot_token="fake_token", chat_id="12345", enabled=True)

    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)
        return True

    notifier.send_message = mock_send

    closed_trades = [
        {"symbol": "TCS.NS", "net_pnl": 15000.0},
        {"symbol": "INFY.NS", "net_pnl": 22000.0},
        {"symbol": "RELIANCE.NS", "net_pnl": -5000.0},
    ]

    ok = notifier.send_monthly_summary(
        strategy_id="trend_momentum",
        month_str="October 2026",
        start_nav=1000000.0,
        end_nav=1045000.0,
        initial_capital=1000000.0,
        cash=300000.0,
        holdings_val=745000.0,
        closed_trades=closed_trades,
        ytd_ret_pct=18.50,
        benchmark_ret_pct=1.20,
    )
    assert ok is True
    assert len(sent_messages) == 1
    msg = sent_messages[0]

    assert "🏆 NSE-QUANT-LAB | MONTHLY PERFORMANCE REPORT" in msg
    assert "Month:</b> October 2026" in msg
    assert "+4.50%" in msg
    assert "YTD Return:</b> <b>+18.50%</b>" in msg
    assert "Nifty 500:</b> +1.20%" in msg
    assert "Alpha:</b> +3.30%" in msg
    assert "Closed Trades: 3 (2W / 1L)" in msg
    assert "Win Rate: <b>66.7%</b>" in msg
