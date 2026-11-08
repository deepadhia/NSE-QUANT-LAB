"""Read-only panel integrity and raw-v-adjusted price audit; saves aggregate evidence."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent


def main():
    arch=ROOT/'external/nse_archive'
    panel=pd.read_parquet(arch/'clean_panel.parquet')
    panel['date']=pd.to_datetime(panel.date)
    px=panel[['open','high','low','close']]
    bad=((panel.high<panel.low)|(panel.high<panel.open-1e-9)|(panel.high<panel.close-1e-9)|
         (panel.low>panel.open+1e-9)|(panel.low>panel.close+1e-9))
    result={'rows':len(panel),'symbols':panel.symbol.nunique(),'first_date':str(panel.date.min().date()),
        'last_date':str(panel.date.max().date()),'duplicate_symbol_dates':int(panel.duplicated(['symbol','date']).sum()),
        'nan_prices_or_volume':int(panel[['open','high','low','close','volume']].isna().any(axis=1).sum()),
        'nonpositive_prices':int((px<=0).any(axis=1).sum()),'invalid_ohlc_envelopes':int(bad.sum()),
        'negative_volumes':int((panel.volume<0).sum()),'zero_volumes':int((panel.volume==0).sum())}
    cols=['symbol','date','series','close','turnover']
    raw=pd.read_parquet(arch/'parquet/bhavcopy_2024.parquet',columns=cols)
    raw['date']=pd.to_datetime(raw.date)
    m=raw.merge(panel[cols],on=['symbol','date','series'],suffixes=('_raw','_adjusted'))
    mismatch=m[(m.series=='EQ') & (m.turnover_raw>=1e8) & (m.close_raw>=10) & (m.close_adjusted<10)]
    mismatch.to_csv(OUT/'raw_adjusted_floor_mismatches_2024.csv',index=False)
    result['2024_liquid_EQ_stockdays_raw_above_10_adjusted_below_10']=len(mismatch)
    result['2024_floor_mismatch_symbols']=sorted(mismatch.symbol.unique().tolist())
    cupid=m[(m.symbol=='CUPID') & (m.date>='2024-03-25') & (m.date<='2024-04-05')]
    cupid.to_csv(OUT/'cupid_raw_adjusted_example.csv',index=False)
    events=pd.read_csv(ROOT/'research/audit_2026-10/results/clean_panel_events.csv')
    result['adjustment_event_sources']=events.source.value_counts().to_dict()
    inferred=events[events.source!='official'].copy()
    inferred.to_csv(OUT/'inferred_corporate_actions.csv',index=False)
    trades=pd.read_csv(ROOT/'research/audit_2026-10/results/ab_rs55_filter/trades_A0_baseline.csv',parse_dates=['entry_date','exit_date'])
    inferred['date']=pd.to_datetime(inferred.date)
    overlaps=[]
    for e in inferred.itertuples():
        hits=trades[(trades.symbol==e.symbol) & (trades.entry_date<e.date) & (trades.exit_date>=e.date)]
        for t in hits.itertuples():
            overlaps.append({'symbol':e.symbol,'event_date':str(e.date.date()),'factor':e.factor,
                'entry_date':str(t.entry_date.date()),'exit_date':str(t.exit_date.date()),'net_pnl':t.net_pnl})
    pd.DataFrame(overlaps).to_csv(OUT/'inferred_actions_overlapping_closed_trades.csv',index=False)
    result['inferred_actions_overlapping_baseline_closed_trades']=overlaps
    (OUT/'panel_analysis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
