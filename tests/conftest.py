import os
import pytest


@pytest.fixture(autouse=True)
def disable_telegram_in_test_suite(monkeypatch):
    """
    Institutionally guarantees that no unit, integration, or regression test
    ever dispatches live alert messages to production Telegram channels.
    """
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")

