from nse_quant_lab.notifications.telegram import TelegramNotifier


def test_telegram_mock_and_formatting():
    notifier = TelegramNotifier(bot_token=None, chat_id=None, enabled=False)
    # Should safely return False without raising exceptions
    assert not notifier.send_message("Test message")

    buys = [{"symbol": "INFY.NS", "alloc": 100000}]
    exits = [{"symbol": "TCS.NS", "reason": "STOP_LOSS"}]
    res = notifier.send_signal_alert("trend_momentum", "2026-10-24", buys, exits)
    assert res is False
