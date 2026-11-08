"""SYNTHETIC_TEST falsification probes, intentionally RED on audited HEAD.
Run explicitly: python -m pytest research/audit_2026-11-08/probes.py -q
These are correctness fixtures, never historical performance evidence.
No network, real Mongo, broker or notifications are used.
"""
import os
import sys
from pathlib import Path

os.environ['TELEGRAM_ENABLED'] = 'false'
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
import numpy as np
import pandas as pd
import pytest
from nse_quant_lab.base_strategy import BaseStrategy
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy
from nse_quant_lab.tax_india import compute_fy_tax
from test_paper_failure_modes import _make, _frame, _order, _position, _cash


class Toy(BaseStrategy):
    """Buy AAA once; optionally close it on the next decision. Synthetic only."""
    def __init__(self, dates, sell=False):
        super().__init__()
        self.dates, self.sell = dates, sell
    def compute_indicators(self, df, config=None):
        return df.copy()
    def get_rebalance_schedule(self, timeline, eval_start, eval_end, config=None):
        return {self.dates[1]: self.dates[0]}
    def select_entry_candidates(self, universe, date, holdings, config):
        r = universe['AAA'].iloc[0]
        return pd.DataFrame({'close':[r.close], 'turnover_50d':[1e9], 'rank':[1]}, index=['AAA'])
    def evaluate_daily_exits(self, universe, date, positions, config):
        return {'AAA':'TEST_EXIT'} if self.sell and date == self.dates[1] and 'AAA' in positions else {}


def toy_frame(dates, prices):
    return pd.DataFrame({'date':dates, 'open':prices, 'close':prices,
        'high':np.array(prices)*1.01, 'low':np.array(prices)*.99, 'volume':1e7})


def test_future_split_must_not_change_past_entry_eligibility():
    dates = pd.bdate_range('2020-01-01', periods=320)
    prices = 20*np.exp(np.arange(320)*.004)
    raw = toy_frame(dates, prices)
    raw['series'], raw['turnover'] = 'EQ', 2e8
    adjusted = raw.copy()
    adjusted[['open','high','low','close']] *= .1  # later 10:1 split
    adjusted['volume'] *= 10
    cfg = BacktestConfig()
    strategy = TrendMomentumPITStrategy(cfg, use_regime=False)
    a = strategy.select_entry_candidates({'AAA':strategy.compute_indicators(raw)}, dates[-1], {}, cfg)
    b = strategy.select_entry_candidates({'AAA':strategy.compute_indicators(adjusted)}, dates[-1], {}, cfg)
    assert list(a.index) == list(b.index), f'Later split removes earlier entry: raw={list(a.index)}, adjusted={list(b.index)}'


def test_engine_must_not_sell_at_last_close_when_history_disappears():
    dates = pd.bdate_range('2025-06-02', periods=3)
    cfg = BacktestConfig(initial_capital=100000, portfolio_size=1)
    uni = {'AAA':toy_frame(dates[:2],[100,100]), 'BBB':toy_frame(dates,[100,100,100])}
    _, trades, _, _, _ = BacktestEngine(Toy(dates),cfg).run(uni,str(dates[0].date()),str(dates[-1].date()))
    assert trades.empty, f'Untradeable missing history creates cash: {trades[["symbol","exit_reason","exit_price"]].to_dict("records")}'


def test_final_partial_fy_must_include_realised_tax_liability():
    dates = pd.to_datetime(['2025-04-01','2025-04-02','2025-04-03'])
    cfg = BacktestConfig(initial_capital=100000, portfolio_size=1, deduct_annual_tax=True)
    metrics, trades, _, _, _ = BacktestEngine(Toy(dates,sell=True),cfg).run(
        {'AAA':toy_frame(dates,[100,100,110])},'2025-04-01','2025-04-03')
    liability, *_ = compute_fy_tax(2026,trades.to_dict('records'),[],[])
    assert metrics['total_tax_paid'] == pytest.approx(liability), f'Engine tax={metrics["total_tax_paid"]}, realised liability={liability}'


def test_fy2025_pre_budget_ltcg_must_use_sale_date_rate():
    t = {'exit_date':'2024-06-03','hold_days':400,'net_pnl':300000,'shares':0,
        'entry_fill_price':0,'exit_fill_price':0}
    tax, *_ = compute_fy_tax(2025,[t],[],[])
    # Rs 1.25 lakh FY exemption; pre-23 July rate 10%; 4% cess.
    assert tax == pytest.approx(175000*.10*1.04)


def test_quote_outage_is_not_confirmed_delisting(monkeypatch):
    mgr, db, _ = _make()
    _position(db)
    _order(db,'ORD_EXIT_stub_strategy_2026-03-02_AAA','AAA','SELL','2026-03-02',reason='DATA_ENDED_FORCED_EXIT')
    monkeypatch.setattr(mgr,'_get_execution_bar',lambda *a:None)
    result = mgr.execute_morning_fills({'AAA':_frame('2026-02-10')},pd.Timestamp('2026-03-03'))
    assert not result['fills'], f'Quote outage booked sale: {result["fills"]}'


def test_next_day_restart_must_recover_executed_buy_before_expiring_signal(monkeypatch):
    mgr, db, _ = _make()
    oid='ORD_BUY_stub_strategy_2026-03-02_AAA'
    _order(db,oid,'AAA','BUY','2026-03-02',reason='RANK_1',shares=0)
    monkeypatch.setattr(mgr,'_audit_fill',lambda *a:(_ for _ in ()).throw(RuntimeError('crash after cash write')))
    with pytest.raises(RuntimeError):
        mgr.execute_morning_fills({'AAA':_frame('2026-03-03')},pd.Timestamp('2026-03-03'))
    assert len(db.executions.docs)==1 and _cash(db)<100000
    retry, _, _ = _make(db)
    retry.execute_morning_fills({'AAA':_frame('2026-03-04')},pd.Timestamp('2026-03-04'))
    pos=db.positions.find_one({'symbol':'AAA'})
    assert pos is not None, f'Executed buy lost: cash={_cash(db)}, order={db.orders.find_one({"order_id":oid})}'


def test_two_stale_cash_snapshots_must_not_lose_a_fill_delta():
    mgr, db, _ = _make()
    # Two workers both read 100000 before either persists a distinct execution.
    mgr._persist_cash(99000,'2026-03-03','EXEC_A')
    mgr._persist_cash(98000,'2026-03-03','EXEC_B')
    assert _cash(db)==97000, f'Absolute stale balance overwrote another fill: cash={_cash(db)}'


def test_0916_buy_must_not_fill_below_current_ask():
    mgr, db, _ = _make()
    _order(db,'ORD_BUY_stub_strategy_2026-03-02_AAA','AAA','BUY','2026-03-02',reason='RANK_1',shares=0)
    mgr.quote_provider=lambda sym:{'open':100.,'high':110.,'low':100.,'ltp':110.,'bid':109.9,'ask':110.,
        'ts':'2026-03-03T09:16:00+05:30','upper_circuit':120.,'lower_circuit':80.}
    result=mgr.execute_morning_fills({'AAA':_frame('2026-03-02')},pd.Timestamp('2026-03-03'))
    assert not result['fills'] or result['fills'][0]['fill_price']>=110., f'09:16 ask=110, fills={result["fills"]}'


def test_0916_lower_circuit_must_use_current_price_not_earlier_open():
    mgr, db, _ = _make()
    _position(db)
    _order(db,'ORD_EXIT_stub_strategy_2026-03-02_AAA','AAA','SELL','2026-03-02')
    mgr.quote_provider=lambda sym:{'open':100.,'high':100.,'low':95.,'ltp':95.,'bid':None,'ask':95.,
        'ts':'2026-03-03T09:16:00+05:30','upper_circuit':105.,'lower_circuit':95.}
    result=mgr.execute_morning_fills({'AAA':_frame('2026-03-02')},pd.Timestamp('2026-03-03'))
    assert not result['fills'], f'No buyers at lower circuit, but fills={result["fills"]}'
