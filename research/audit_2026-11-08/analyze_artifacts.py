"""Independent audit calculations from saved baseline artifacts; no strategy changes."""
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from nse_quant_lab.tax_india import compute_fy_tax


def main():
    out=Path(__file__).resolve().parent
    base=out/'baseline'
    nav=pd.read_csv(base/'daily_nav.csv',parse_dates=['date']).set_index('date')
    trades=pd.read_csv(base/'trades.csv',parse_dates=['entry_date','exit_date'])
    executions=pd.read_csv(base/'executions.csv',parse_dates=['date'])
    metrics=json.loads((base/'metrics.json').read_text())
    years=(nav.index[-1]-nav.index[0]).days/365.25
    cagr=lambda v:100*((v/100000)**(1/years)-1)
    annual=nav.nav.resample('YE').last()
    annual_returns=annual.pct_change()
    annual_returns.iloc[0]=annual.iloc[0]/100000-1
    annual_returns.to_csv(out/'annual_returns.csv')
    positions={}
    for e in executions.itertuples():
        if e.action=='BUY':
            positions[e.symbol]={'symbol':e.symbol,'entry_date':str(e.date.date()),'shares':e.shares,
                'entry_fill_price':e.fill_price,'outlay':-e.net_cash}
        else:
            positions.pop(e.symbol,None)
    panel=pd.read_parquet(ROOT/'external/nse_archive/clean_panel.parquet',columns=['symbol','date','close'])
    panel['date']=pd.to_datetime(panel.date)
    for p in positions.values():
        g=panel[(panel.symbol==p['symbol']) & (panel.date<=nav.index[-1])].sort_values('date')
        p['last_close']=float(g.close.iloc[-1])
        p['value']=p['shares']*p['last_close']
        p['unrealised_pnl']=p['value']-p['outlay']
        p['weight_pct']=100*p['value']/float(nav.nav.iloc[-1])
    positions=pd.DataFrame(positions.values()).sort_values('value',ascending=False)
    positions.to_csv(out/'terminal_positions.csv',index=False)
    cs,cl=[],[]
    tax_rows=[]
    for year in range(2007,2028):
        fy=trades[(trades.exit_date>=f'{year-1}-04-01') & (trades.exit_date<=f'{year}-03-31')]
        tax,cs,cl,detail=compute_fy_tax(year,fy.to_dict('records'),cs,cl)
        tax_rows.append({'fy_end':year,'tax':tax,'trades':len(fy),**detail})
    pd.DataFrame(tax_rows).to_csv(out/'recomputed_fy_tax.csv',index=False)
    terminal_tax=next(x['tax'] for x in tax_rows if x['fy_end']==2027)
    cash_from_ledger=100000+executions.net_cash.sum()
    assessed=sum(x['tax_paid'] for x in metrics['fy_tax_ledger'])
    ledger_nav=cash_from_ledger-assessed+positions.value.sum()
    rets=nav.nav.pct_change().dropna()
    roll=nav.nav/nav.nav.shift(1260)
    result={'sessions':len(nav),'start':str(nav.index[0].date()),'end':str(nav.index[-1].date()),
        'first_eod_nav':float(nav.nav.iloc[0]),'final_nav':float(nav.nav.iloc[-1]),
        'cagr_initial_capital_pct':cagr(nav.nav.iloc[-1]),
        'cagr_first_eod_nav_pct':100*((nav.nav.iloc[-1]/nav.nav.iloc[0])**(1/years)-1),
        'max_drawdown_pct':float((nav.nav/nav.nav.cummax()-1).min()*100),
        'sharpe_arithmetic_zero_rf_252':float(rets.mean()/rets.std()*np.sqrt(252)),
        'sharpe_arithmetic_rf6_252':float((rets.mean()-.06/252)/rets.std()*np.sqrt(252)),
        'engine_sharpe':metrics['sharpe_ratio'],
        'realised_final_partial_fy_tax_not_accrued':terminal_tax,
        'nav_after_partial_fy_tax_only':float(nav.nav.iloc[-1]-terminal_tax),
        'cagr_after_partial_fy_tax_only':cagr(nav.nav.iloc[-1]-terminal_tax),
        'nav_reconciled_from_executions_and_terminal_marks':float(ledger_nav),
        'nav_reconciliation_residual':float(nav.nav.iloc[-1]-ledger_nav),
        'top_terminal_holding':positions.iloc[0].to_dict(),
        'cagr_mark_cupid_at_entry_outlay_no_path_resimulation':cagr(nav.nav.iloc[-1]-positions.loc[positions.symbol=='CUPID','unrealised_pnl'].sum()),
        'cagr_all_terminal_positions_at_entry_outlay_no_path_resimulation':cagr(nav.nav.iloc[-1]-positions.unrealised_pnl.sum()),
        'closed_trades':len(trades),'forced_history_end_exits':int((trades.exit_reason=='DATA_ENDED_FORCED_EXIT').sum()),
        'worst_year_return_pct':float(annual_returns.min()*100),
        'worst_1260_session_annualised_return_pct':float((roll.min()**(1/5)-1)*100),
        'top5_closed_symbol_pnl':trades.groupby('symbol').net_pnl.sum().nlargest(5).to_dict()}
    (out/'artifact_analysis.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
