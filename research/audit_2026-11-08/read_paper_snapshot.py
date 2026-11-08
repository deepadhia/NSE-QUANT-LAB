"""Read-only aggregate paper-account checks. Never prints connection details or writes DB."""
import json
import sys
from pathlib import Path
from pymongo import MongoClient

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from nse_quant_lab.config import PaperTradingConfig


def main():
    cfg=PaperTradingConfig(telegram_enabled=False)
    client=MongoClient(cfg.mongo_uri,serverSelectionTimeoutMS=5000)
    db=client[cfg.db_name]
    result=[]
    for s in db.strategies.find({'status':'ACTIVE','mode':'paper'},
        {'_id':0,'strategy_id':1,'mode':1,'cash':1,'initial_capital':1,'last_signal_date':1,'last_execution_date':1}):
        q={'strategy_id':s['strategy_id'],'mode':'paper'}
        execs=list(db.executions.find(q,{'_id':0,'net_cash':1,'exec_id':1}))
        positions=list(db.positions.find(q,{'_id':0,'symbol':1,'shares':1}))
        fills=list(db.fill_audit.find(q,{'_id':0,'quote_vs_open_bps':1,'half_spread_bps':1,'model_slip_bps':1,'quote':1}))
        ledger=float(s.get('initial_capital',cfg.initial_capital))+sum(e.get('net_cash',0) for e in execs)
        qvalid=[f for f in fills if f.get('quote') and f.get('quote_vs_open_bps') is not None]
        result.append({'strategy':s['strategy_id'],'last_signal_date':s.get('last_signal_date'),
            'last_execution_date':s.get('last_execution_date'),'execution_count':len(execs),
            'position_count':len(positions),'fill_audit_count':len(fills),'fill_audit_with_bidask_count':len(qvalid),
            'stored_cash_minus_execution_ledger':round(float(s.get('cash',0))-ledger,2),
            'duplicate_exec_ids':len(execs)-len(set(e.get('exec_id') for e in execs)),
            'nonpositive_share_positions':sum(p.get('shares',0)<=0 for p in positions),
            'mean_directional_quote_vs_open_bps':sum(f['quote_vs_open_bps'] for f in qvalid)/len(qvalid) if qvalid else None,
            'mean_model_slip_bps':sum(f.get('model_slip_bps',0) for f in fills)/len(fills) if fills else None,
            'mean_half_spread_bps':sum(f.get('half_spread_bps',0) for f in qvalid)/len(qvalid) if qvalid else None,
            'quotes_worse_than_model_fill':sum(f['quote_vs_open_bps']>f.get('model_slip_bps',0) for f in qvalid)})
    client.close()
    (Path(__file__).resolve().parent/'paper_snapshot.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
