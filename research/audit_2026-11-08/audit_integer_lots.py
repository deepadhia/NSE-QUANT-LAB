"""Compare baseline simulated BUY units with raw contemporary exchange units.
Diagnostic only: not a counterfactual backtest or a replacement execution ledger.
"""
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent


def main():
    e=pd.read_csv(OUT/'baseline/executions.csv',parse_dates=['date'])
    buys=e[e.action=='BUY'].copy()
    rows=[]
    for year,b in buys.groupby(buys.date.dt.year):
        raw=pd.read_parquet(ROOT/f'external/nse_archive/parquet/bhavcopy_{year}.parquet',
            columns=['symbol','date','series','open','volume'])
        raw['date']=pd.to_datetime(raw.date)
        raw=raw[raw.series=='EQ'].drop_duplicates(['symbol','date'])
        m=b.merge(raw,on=['symbol','date'],suffixes=('_engine','_raw'),how='left')
        rows.append(m)
    m=pd.concat(rows,ignore_index=True)
    m['adjustment_factor']=m.raw_price/m.open
    m['contemporary_share_equivalent']=m.shares*m.adjustment_factor
    matched=m.open.notna()
    fractional=matched & (np.abs(m.contemporary_share_equivalent-m.contemporary_share_equivalent.round())>1e-5)
    under_one=matched & (m.contemporary_share_equivalent<1-1e-5)
    m.to_csv(OUT/'integer_lot_reconciliation.csv',index=False)
    result={'total_buys':len(m),'matched_raw_EQ_buys':int(matched.sum()),'unmatched_buys':int((~matched).sum()),
        'matched_buys_with_fractional_contemporary_share_equivalent':int(fractional.sum()),
        'matched_buys_with_less_than_one_contemporary_share':int(under_one.sum()),
        'under_one_examples':m.loc[under_one,['symbol','date','shares','raw_price','open','contemporary_share_equivalent']].astype({'date':str}).to_dict('records'),
        'cupid_buys':m.loc[m.symbol=='CUPID',['symbol','date','shares','raw_price','open','contemporary_share_equivalent']].astype({'date':str}).to_dict('records'),
        'caveat':'Raw matches only same ticker/date EQ; ticker renames and series changes are unmatched. Does not resimulate cash or rankings.'}
    (OUT/'integer_lot_analysis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
