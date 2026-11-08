"""SYNTHETIC_TEST regression tests for the 2026-11-08 financial audit.
Run explicitly: python -m pytest research/audit_2026-11-08/probes.py -q
These are correctness fixtures, never historical performance evidence.
No network, real Mongo, broker or notifications are used.
"""
import os
import sys
from pathlib import Path

os.environ['TELEGRAM_ENABLED'] = 'false'
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
        return pd.DataFrame({'close':[r.get('raw_close',r.close)], 'turnover_50d':[1e9], 'rank':[1]}, index=['AAA'])
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
    for col in ('open', 'high', 'low', 'close'):
        adjusted['raw_'+col] = raw[col]
    adjusted['price_adjustment_factor'] = .1
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
    with pytest.raises(ValueError, match='MISSING_HELD_BAR'):
        BacktestEngine(Toy(dates),cfg).run(uni,str(dates[0].date()),str(dates[-1].date()))


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
    db.executions.insert_one({'strategy_id':'stub_strategy','mode':'paper','exec_id':'EXEC_A','net_cash':-1000})
    db.executions.insert_one({'strategy_id':'stub_strategy','mode':'paper','exec_id':'EXEC_B','net_cash':-2000})
    mgr._persist_cash(99000,'2026-03-03','EXEC_A')
    with pytest.raises(RuntimeError, match='CASH_CONFLICT'):
        mgr._persist_cash(98000,'2026-03-03','EXEC_B')
    assert _cash(db)==99000
    mgr._persist_cash(97000,'2026-03-03','EXEC_B')
    assert _cash(db)==97000


def test_cash_cas_seeds_legacy_strategy_document_without_cash():
    mgr, db, _ = _make()
    db.strategies.docs[0].pop('cash')            # legacy document: balance only ever lived in daily_nav
    db.executions.insert_one({'strategy_id':'stub_strategy','mode':'paper','exec_id':'EXEC_L','net_cash':-1000})
    mgr._persist_cash(99000,'2026-03-03','EXEC_L')
    assert _cash(db)==99000


def test_cash_cas_tolerates_sub_cent_float_drift():
    mgr, db, _ = _make()
    db.executions.insert_one({'strategy_id':'stub_strategy','mode':'paper','exec_id':'EXEC_D','net_cash':-1234.567})
    # Running balance is unrounded while the stored one is rounded to cents.
    mgr._persist_cash(100000-1234.567,'2026-03-03','EXEC_D')
    db.executions.insert_one({'strategy_id':'stub_strategy','mode':'paper','exec_id':'EXEC_E','net_cash':-0.333})
    mgr._persist_cash(100000-1234.567-0.333,'2026-03-03','EXEC_E')
    assert _cash(db)==round(100000-1234.567-0.333,2)


def test_afternoon_cron_entry_is_special_session_only():
    cron = (Path(__file__).resolve().parents[1]/'deploy'/'crontab.txt').read_text(encoding='utf-8')
    afternoon = [l for l in cron.splitlines() if l.startswith('21 14 ')]
    assert len(afternoon)==1 and '--special-session-only' in afternoon[0]


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


def test_muhurat_sunday_has_explicit_trading_session():
    from nse_quant_lab.holidays import is_trading_day, get_trading_session
    assert is_trading_day('2026-11-08')
    s=get_trading_session('2026-11-08')
    assert s.open_time=='14:20' and s.execution_cutoff=='15:10' and s.close_time=='15:35'
    assert not is_trading_day('2026-11-07')


def test_muhurat_scheduler_times_and_retry_window():
    from scripts.run_scheduler_daemon import QuantLabScheduler
    scheduler=QuantLabScheduler()
    jobs={j['id']:j for j in scheduler.jobs_for_date('2026-11-08')}
    assert jobs['morning_execution']['time']=='14:21'
    assert jobs['morning_execution']['max_time']=='15:10'
    assert jobs['fulltier_ingestion']['time']>'15:35'
    assert jobs['daily_pipeline']['time']>jobs['benchmark_ingestion']['time']


def test_recovery_preserves_original_fill_date(monkeypatch):
    mgr, db, _ = _make()
    _order(db,'ORD_BUY_stub_strategy_2026-03-02_AAA','AAA','BUY','2026-03-02',reason='RANK_1',shares=0)
    monkeypatch.setattr(mgr,'_audit_fill',lambda *a:(_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        mgr.execute_morning_fills({'AAA':_frame('2026-03-03')},pd.Timestamp('2026-03-03'))
    retry, _, _ = _make(db)
    retry.execute_morning_fills({'AAA':_frame('2026-03-04')},pd.Timestamp('2026-03-04'))
    pos=db.positions.find_one({'symbol':'AAA'})
    assert pos is not None and pos['entry_date']=='2026-03-03'


def _raw_basis_market(bonus=False):
    dates=pd.bdate_range('2025-04-01',periods=4)
    df=toy_frame(dates,[10.,10.,10.,10.])
    for col in ('open','high','low','close'):
        df['raw_'+col]=df[col]*[10,10,5,5]
    df['price_adjustment_factor']=[.1,.1,.2,.2]
    df['split_ratio']=[1.,1.,1. if bonus else 2.,1.]
    df['bonus_ratio']=[1.,1.,2. if bonus else 1.,1.]
    df['share_action_verified']=[False,False,True,False]
    return dates,df


def _zero_fee_config():
    return BacktestConfig(initial_capital=1000,portfolio_size=1,slippage_pct=0,fees_buffer_pct=0,
        stt_pct=0,stamp_duty_buy_pct=0,sebi_pct=0,exchange_txn_pct_legacy=0,
        exchange_txn_pct_revised=0,gst_pct=0,dp_charge_sell=0)


def test_raw_integer_fill_and_split_preserve_equity():
    dates,df=_raw_basis_market()
    metrics,_,_,execs,nav=BacktestEngine(Toy(dates),_zero_fee_config()).run({'AAA':df},str(dates[0].date()),str(dates[-1].date()))
    assert execs.iloc[0]['raw_price']==100 and execs.iloc[0]['shares']==10
    assert nav.nav.tolist()==pytest.approx([1000]*4)
    assert metrics['corporate_action_ledger'][0]['new_shares']==20


def test_bonus_creates_dated_zero_cost_tax_lot():
    dates,df=_raw_basis_market(bonus=True)
    strategy=Toy(dates)
    strategy.evaluate_daily_exits=lambda uni,date,positions,cfg: {'AAA':'TEST_EXIT'} if date==dates[2] else {}
    _,trades,_,_,_=BacktestEngine(strategy,_zero_fee_config()).run({'AAA':df},str(dates[0].date()),str(dates[-1].date()))
    lots=trades.iloc[0].tax_lots
    assert len(lots)==2 and lots[0]['entry_date']==str(dates[1].date())
    assert lots[1]['entry_date']==str(dates[2].date())
    assert lots[1]['net_pnl']==pytest.approx(500)
    assert sum(l['net_pnl'] for l in lots)==pytest.approx(trades.iloc[0].net_pnl)


def test_unverified_share_action_cannot_be_simulated():
    dates,df=_raw_basis_market()
    df['share_action_verified']=False
    with pytest.raises(ValueError,match='UNVERIFIED_SHARE_ACTION'):
        BacktestEngine(Toy(dates),_zero_fee_config()).run({'AAA':df},str(dates[0].date()),str(dates[-1].date()))


def test_special_session_execution_window_boundaries():
    from datetime import datetime
    from nse_quant_lab.holidays import execution_window_open, IST
    for h,m,expected in [(9,16,False),(14,20,False),(14,21,True),(15,9,True),(15,10,False),(18,0,False)]:
        assert execution_window_open(datetime(2026,11,8,h,m,tzinfo=IST)) is expected


def test_account_lock_blocks_another_process_and_releases(tmp_path):
    import subprocess
    from nse_quant_lab.paper.locking import account_lock
    identity='audit-process-lock-'+str(tmp_path)
    code="from nse_quant_lab.paper.locking import account_lock\nwith account_lock("+repr(identity)+", timeout=.1):\n print('acquired')"
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    with account_lock(identity):
        result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20)
        assert result.returncode!=0 and 'ACCOUNT_BUSY' in result.stderr
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0 and 'acquired' in result.stdout


class TwoStepToy(Toy):
    """Buy AAA on the first rebalance, then BBB on the second (the morning AAA splits). Synthetic only."""
    def get_rebalance_schedule(self, timeline, eval_start, eval_end, config=None):
        return {self.dates[1]: self.dates[0], self.dates[2]: self.dates[1]}
    def select_entry_candidates(self, universe, date, holdings, config):
        syms = ['AAA'] if date == self.dates[0] else ['AAA', 'BBB']   # AAA stays ranked 1 so it is held through the split
        rows = [universe[s].loc[universe[s]['date'] == date].iloc[0] for s in syms]
        return pd.DataFrame({'close': [r.get('raw_close', r.close) for r in rows], 'turnover_50d': [1e9] * len(syms),
                             'rank': list(range(1, len(syms) + 1))}, index=syms)


def test_same_morning_split_must_not_inflate_sizing_equity_for_a_new_buy():
    """Holdings are re-based by the morning's share action but were valued at the pre-split decision close, so pre-trade equity (and the
    new position) was overstated by the split ratio. AAA (1 share @600) splits 2:1 on the morning BBB is bought; true equity stays 4,000."""
    dates, aaa = _raw_basis_market()                      # AAA raw 600,600,300,300 after scaling ; 2:1 split on dates[2]
    for col in ('open', 'high', 'low', 'close', 'raw_open', 'raw_high', 'raw_low', 'raw_close'):
        aaa[col] = aaa[col] * 6
    bbb = toy_frame(dates, [10., 10., 10., 10.])
    for col in ('open', 'high', 'low', 'close'):
        bbb['raw_' + col] = bbb[col]
    bbb['price_adjustment_factor'] = 1.0
    cfg = _zero_fee_config()
    cfg.initial_capital, cfg.portfolio_size = 4000, 4
    _, _, _, execs, nav = BacktestEngine(TwoStepToy(dates), cfg).run({'AAA': aaa, 'BBB': bbb}, str(dates[0].date()), str(dates[-1].date()))
    buys = execs[execs.action == 'BUY'].set_index('symbol')
    assert buys.loc['AAA', 'shares'] == 1                  # floor(1000/600)
    assert buys.loc['BBB', 'shares'] == 100                # true equity 4000 -> 1000 at raw 10 (inflated valuation gave 113)
    assert nav.nav.tolist() == pytest.approx([4000] * 4)


def test_schedule_preview_is_read_only_and_describes_the_special_session(monkeypatch):
    from scripts import run_scheduler_daemon as d
    scheduler = d.QuantLabScheduler()
    monkeypatch.setattr(scheduler, 'execute_job', lambda *a, **k: (_ for _ in ()).throw(AssertionError('preview must not run jobs')))
    monkeypatch.setattr(scheduler, '_save_state', lambda *a, **k: (_ for _ in ()).throw(AssertionError('preview must not write state')))
    text = d.format_schedule_preview(scheduler, '2026-11-08')
    assert 'SPECIAL session' in text and '14:21  morning_execution  stop retrying at 15:10' in text
    assert 'CLOSED' in d.format_schedule_preview(scheduler, '2026-11-07')
