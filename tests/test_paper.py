import copy
import pandas as pd
import pytest

from nse_quant_lab.config import PaperTradingConfig
from nse_quant_lab.paper.manager import PaperTradingManager
from nse_quant_lab.strategies.trend_momentum import TrendMomentumStrategy
from nse_quant_lab.data_loader import make_synthetic_stock


class InMemoryCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d.get(key, 0), reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)

    def __list__(self):
        return self.docs


class InMemoryCollection:
    def __init__(self):
        self.docs = []

    def _matches(self, doc, query):
        for k, v in query.items():
            if isinstance(v, dict):
                if "$in" in v:
                    if doc.get(k) not in v["$in"]:
                        return False
                if "$gte" in v:
                    if doc.get(k, 0) < v["$gte"]:
                        return False
                if "$lte" in v:
                    if doc.get(k, 0) > v["$lte"]:
                        return False
            elif doc.get(k) != v:
                return False
        return True

    def find(self, query=None):
        query = query or {}
        matched = [copy.deepcopy(d) for d in self.docs if self._matches(d, query)]
        return InMemoryCursor(matched)

    def find_one(self, query=None):
        query = query or {}
        for d in self.docs:
            if self._matches(d, query):
                return copy.deepcopy(d)
        return None

    def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return type("InsertResult", (), {"inserted_id": "mock_id"})()

    def replace_one(self, query, replacement, upsert=False):
        for i, d in enumerate(self.docs):
            if self._matches(d, query):
                self.docs[i] = copy.deepcopy(replacement)
                return type("UpdateResult", (), {"matched_count": 1, "modified_count": 1})()
        if upsert:
            self.docs.append(copy.deepcopy(replacement))
            return type("UpdateResult", (), {"matched_count": 0, "upserted_id": "mock_id"})()
        return type("UpdateResult", (), {"matched_count": 0, "modified_count": 0})()

    def update_one(self, query, update):
        for d in self.docs:
            if self._matches(d, query):
                if "$set" in update:
                    for k, v in update["$set"].items():
                        d[k] = copy.deepcopy(v)
                return type("UpdateResult", (), {"matched_count": 1, "modified_count": 1})()
        return type("UpdateResult", (), {"matched_count": 0, "modified_count": 0})()

    def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._matches(d, query):
                self.docs.pop(i)
                return type("DeleteResult", (), {"deleted_count": 1})()
        return type("DeleteResult", (), {"deleted_count": 0})()


class MockMongoDbWrapper:
    def __init__(self):
        self.orders = InMemoryCollection()
        self.executions = InMemoryCollection()
        self.positions = InMemoryCollection()
        self.trades = InMemoryCollection()
        self.daily_nav = InMemoryCollection()
        self.strategies = InMemoryCollection()

    def ping(self) -> bool:
        return True


def test_paper_trading_lifecycle():
    """
    Tests end-to-end forward paper trading workflow:
    1. Initialize portfolio
    2. Generate post-market signals
    3. Execute morning fills
    4. Verify DB persistence with mode='paper'
    """
    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(initial_capital=500_000.0, portfolio_size=2, strategy_id="trend_momentum", telegram_enabled=False, min_turnover_50d=0.0, min_price=0.0, enforce_pit_universe=False)
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)

    # 1. Initialize
    mgr.initialize_portfolio()
    nav_doc = mock_db.daily_nav.find_one({"strategy_id": "trend_momentum", "mode": "paper"})
    assert nav_doc is not None
    assert nav_doc["cash"] == 500_000.0
    assert nav_doc["nav"] == 500_000.0

    # 2. Build 2 synthetic stocks
    s1 = make_synthetic_stock("STOCK1.NS", n_days=300, start_price=100.0, daily_ret=0.002, start_date="2024-01-01")
    s2 = make_synthetic_stock("STOCK2.NS", n_days=300, start_price=200.0, daily_ret=0.001, start_date="2024-01-01")
    universe = {"STOCK1.NS": s1, "STOCK2.NS": s2}

    dates = sorted(s1["date"].tolist())
    decision_date = dates[239]      # 2024-11-29 month-end close
    execution_date = dates[240]     # 2024-12-02 next-day morning open

    # 3. Post-Market Scan
    sig_res = mgr.generate_post_market_signals(universe, decision_date)
    assert len(sig_res["buys"]) > 0

    pending_orders = list(mock_db.orders.find({"mode": "paper", "status": "PENDING_OPEN"}))
    assert len(pending_orders) > 0
    for order in pending_orders:
        assert order["mode"] == "paper"
        assert order["status"] == "PENDING_OPEN"
        assert order["action"] in ("BUY", "SELL")

    # 4. Morning Execution
    exec_res = mgr.execute_morning_fills(universe, execution_date)
    assert len(exec_res["fills"]) > 0

    # Verify executions collection
    exec_docs = list(mock_db.executions.find({"mode": "paper"}))
    assert len(exec_docs) > 0
    for ex in exec_docs:
        assert ex["mode"] == "paper"
        assert ex["shares"] > 0
        assert ex["fill_price"] > 0

    # Verify positions collection
    pos_docs = list(mock_db.positions.find({"mode": "paper"}))
    assert len(pos_docs) > 0
    for pos in pos_docs:
        assert pos["mode"] == "paper"
        assert pos["shares"] > 0

    # Verify Daily NAV conservation
    latest_nav = mock_db.daily_nav.find_one({"strategy_id": "trend_momentum", "mode": "paper", "date": execution_date.strftime("%Y-%m-%d")})
    assert latest_nav is not None
    assert latest_nav["mode"] == "paper"
    assert abs((latest_nav["cash"] + latest_nav["holdings_val"]) - latest_nav["nav"]) < 0.05

    # 5. Verify strategies collection for dashboard listing
    strat_doc = mock_db.strategies.find_one({"strategy_id": "trend_momentum", "mode": "paper"})
    assert strat_doc is not None, "strategies collection must contain strategy listing document"
    assert strat_doc["status"] == "ACTIVE"
    assert strat_doc["current_nav"] > 0
    assert "total_return_pct" in strat_doc
    assert "realized_pnl" in strat_doc
    assert strat_doc["open_positions_count"] > 0


def test_paper_manager_v6_enhancements():
    mock_db = MockMongoDbWrapper()
    cfg = PaperTradingConfig(
        initial_capital=100_000.0,
        portfolio_size=5,
        strategy_id='trend_momentum',
        unaffordable_policy='TRY_NEXT',
        telegram_enabled=False,
        min_turnover_50d=0.0,
        min_price=0.0,
        enforce_pit_universe=False
    )
    strat = TrendMomentumStrategy(cfg)
    mgr = PaperTradingManager(strategy=strat, config=cfg, db=mock_db)
    mgr.initialize_portfolio()

    s1 = make_synthetic_stock('STOCK1.NS', n_days=300, start_price=100.0, daily_ret=0.002, start_date='2024-01-01')
    s2 = make_synthetic_stock('STOCK2.NS', n_days=300, start_price=200.0, daily_ret=0.001, start_date='2024-01-01')
    s3 = make_synthetic_stock('STOCK3.NS', n_days=300, start_price=300.0, daily_ret=0.003, start_date='2024-01-01')
    universe = {'STOCK1.NS': s1, 'STOCK2.NS': s2, 'STOCK3.NS': s3}

    dates = sorted(s1['date'].tolist())
    decision_date = dates[239]
    execution_date = dates[240]

    bar = mgr._get_execution_bar('STOCK1.NS', execution_date, universe)
    assert bar is not None
    assert 'turnover_50d' in bar
    assert bar['turnover_50d'] > 0

    sig_res = mgr.generate_post_market_signals(universe, decision_date)
    assert len(sig_res['buys']) > 0

    mock_db.orders.insert_one({
        'order_id': 'ORDER_EXTRA_BACKUP',
        'strategy_id': 'trend_momentum',
        'mode': 'paper',
        'symbol': 'EXTRA.NS',
        'action': 'BUY',
        'status': 'PENDING_OPEN',
        'created_at': decision_date.isoformat(),
    })

    exec_res = mgr.execute_morning_fills(universe, execution_date)

    expired_order = mock_db.orders.find_one({'order_id': 'ORDER_EXTRA_BACKUP'})
    assert expired_order is not None
    assert expired_order['status'] == 'SKIPPED_EXPIRED'

    remaining_pending = list(mock_db.orders.find({'mode': 'paper', 'status': 'PENDING_OPEN'}))
    assert len(remaining_pending) == 0, 'No orders should remain PENDING_OPEN after morning execution'

    pos_docs = list(mock_db.positions.find({'mode': 'paper'}))
    assert len(pos_docs) > 0
    for pos in pos_docs:
        if pos.get('ema100_stop') is not None:
            assert pos['stop_loss_price'] == pos['ema100_stop']
