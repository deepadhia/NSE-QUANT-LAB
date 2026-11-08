"""Strict replay using the new nominal-price panel; preserve all prior audit outputs.

An unresolved held bar/action produces INCOMPLETE, never a fabricated final CAGR.
"""
import json
from pathlib import Path
import sys
import time
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from nse_quant_lab.config import BacktestConfig
from nse_quant_lab.data_loader import load_pit_universe
from nse_quant_lab.engine import BacktestEngine
from nse_quant_lab.strategies.trend_momentum_pit import TrendMomentumPITStrategy


def main():
    out=Path(__file__).resolve().parent/'corrected_history'
    out.mkdir(exist_ok=True)
    target=out/'validation.json'
    if target.exists():
        raise FileExistsError('Preserve existing corrected replay output')
    cfg=BacktestConfig.from_dict(json.loads((out.parent/'baseline/config.json').read_text()))
    bench=pd.read_csv(out.parent/'baseline/benchmark.csv',parse_dates=['date']).set_index('date')['close']
    start=time.time()
    try:
        uni=load_pit_universe('2026-10-30',include_live=False)
        print(f'Loaded {len(uni)} symbols; nominal prices present: '+str(all('raw_open' in f for f in uni.values())),flush=True)
        metrics,*_=BacktestEngine(TrendMomentumPITStrategy(cfg,benchmark=bench),cfg).run(
            uni,'2007-01-01','2026-10-30',save_results=True,output_dir=str(out))
        result={'status':'SIMULATION_COMPLETED_NOT_FORWARD_VALIDATED','cagr':metrics['cagr_calendar_pct'],
                'ending_nav':metrics['ending_nav'],'seconds':time.time()-start}
    except ValueError as exc:
        result={'status':'INCOMPLETE_DATA_REQUIRES_REVIEW','reason':str(exc),'seconds':time.time()-start}
        target.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2),flush=True)
        return 2
    target.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
    return 0


if __name__=='__main__':
    sys.exit(main())
