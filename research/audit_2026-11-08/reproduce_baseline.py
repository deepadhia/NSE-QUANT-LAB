"""Audit-only reproduction. Reads local data/Mongo benchmark; writes only this audit directory.
No broker, Telegram, production DB writes, or parameter search.
"""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.data_loader import load_pit_universe
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy


def main():
    out = Path(__file__).resolve().parent / 'baseline'
    out.mkdir(exist_ok=True)
    if (out / 'metrics.json').exists():
        raise RuntimeError('Audit outputs already exist; preserve them.')
    start = time.time()
    client = MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=3000)
    docs = list(client.stock_scanner.benchmark_ohlcv.find(
        {'symbol': '^CRSLDX'}, {'_id': 0, 'date': 1, 'close': 1}))
    client.close()
    bench = pd.DataFrame(docs)
    bench['date'] = pd.to_datetime(bench['date'])
    bench = bench.drop_duplicates('date', keep='last').sort_values('date').set_index('date')['close']
    bench.to_csv(out / 'benchmark.csv')
    cfg = BacktestConfig(initial_capital=100_000, portfolio_size=15, max_open_positions=15,
        rebalance_hold_rank_buffer=1500, min_turnover_50d=0, min_price=0, slippage_pct=.002,
        unaffordable_policy='TRY_NEXT', enforce_pit_universe=False, pit_top_n_liquidity=0,
        brokerage_per_order=0, dp_charge_sell=14.75, max_volume_participation_pct=.10,
        deduct_annual_tax=True, tax_model='india_fy', execution_approach='APPROACH_3')
    uni = load_pit_universe('2026-10-30', include_live=False)
    print(f'Loaded {len(uni)} symbols in {time.time()-start:.1f}s', flush=True)
    metrics, trades, orders, executions, nav = BacktestEngine(
        TrendMomentumPITStrategy(config=cfg, benchmark=bench), cfg).run(
        uni, '2007-01-01', '2026-10-30', save_results=True, output_dir=str(out))
    panel = ROOT / 'external/nse_archive/clean_panel.parquet'
    manifest = {'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'panel_sha256': hashlib.file_digest(panel.open('rb'), 'sha256').hexdigest(),
        'benchmark_sha256': hashlib.sha256((out/'benchmark.csv').read_bytes()).hexdigest(),
        'config': cfg.to_dict(), 'seconds': time.time()-start,
        'limitations': 'Current local panel, provenance not independently certified; in-sample reproduction only.'}
    (out/'audit_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps({k:metrics[k] for k in ['ending_nav','total_trades','cagr_calendar_pct','max_drawdown_pct','total_tax_paid']}, indent=2), flush=True)


if __name__ == '__main__':
    main()
