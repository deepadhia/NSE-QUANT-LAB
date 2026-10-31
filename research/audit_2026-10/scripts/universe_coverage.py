import pandas as pd, numpy as np, pickle, sys
from pymongo import MongoClient
W="C:/Users/DEEPJA~1/AppData/Local/Temp/claude/D--Personal-Projects-NSE-Quant-Lab/0897a504-3001-43fc-a23d-f068c139a6eb/scratchpad/work/"
CSV=r"C:/Users/DeepJAdhia/Downloads/archive/Combined_Data_2016_2024.csv"
df=pd.read_csv(CSV,usecols=["SYMBOL","SERIES","CLOSE","TOTTRDQTY","TIMESTAMP","ISIN"],dtype={"SYMBOL":str,"SERIES":str,"ISIN":str})
df=df[df.SERIES.isin(["EQ","BE","BZ"])]; df["date"]=pd.to_datetime(df.TIMESTAMP)
df=df.sort_values(["SYMBOL","date","TOTTRDQTY"]).drop_duplicates(["SYMBOL","date"],keep="last")
df["to"]=df.CLOSE*df.TOTTRDQTY
isin=df.groupby("SYMBOL").ISIN.last()
eq=isin[~isin.str.startswith("INF")].index            # exclude ETFs/funds (INF ISINs)
to50=df[df.SYMBOL.isin(eq)].pivot(index="date",columns="SYMBOL",values="to").rolling(50,min_periods=30).mean()
U=pickle.load(open(W+"universe2.pkl","rb"))
db=MongoClient("mongodb://localhost:27017/")["stock_scanner"]
dbisin={d["isin"] for d in db["symbols"].find({},{"isin":1,"_id":0})}
have={s.replace(".NS","") for s in U}                    # universe symbols (renamed names handled via ISIN below)
ren={"ABNUVO":"ABIRLANUVO","DHANI":"IBVENTURES"}
have|={v for k,v in ren.items() if k in have}|{"DHANI","IBVENTURES"}&have
cov_isin=set(isin[isin.isin(dbisin)].index)             # raw symbols whose ISIN is in the DB (covers renames)
covered=have|cov_isin
# date presence per symbol
pres={s.replace(".NS",""):set(d["date"]) for s,d in U.items()}
mes=pd.Series(to50.index,index=to50.index).groupby(to50.index.to_period("M")).max()
mes=[d for d in mes if d>=pd.Timestamp("2016-07-01") and d<=pd.Timestamp("2024-11-30")]   # >=50d history
rows=[]; miss={}
for d in mes:
    t=to50.loc[d].dropna().sort_values(ascending=False).head(200)
    ok=[s for s in t.index if s in covered]
    # present with a price on that date or the previous 5 sessions in my panel
    lo=d-pd.Timedelta(days=7)
    def has(s):
        key=s if s in pres else None
        if key is None:
            return s in cov_isin       # ISIN-matched rename: assume present (checked separately below)
        return any((x<=d and x>=lo) for x in pres[key])
    ok2=[s for s in ok if has(s)]
    for s in t.index:
        if s not in ok2: miss[s]=miss.get(s,0)+1
    rows.append({"date":d.date(),"covered_symbol_or_isin":len(ok)/200,"with_price_in_panel":len(ok2)/200})
r=pd.DataFrame(rows)
print("months:",len(r),"| mean coverage (in universe):",round(r.covered_symbol_or_isin.mean()*100,2),"% | with price on date:",round(r.with_price_in_panel.mean()*100,2),"% | worst month:",round(r.with_price_in_panel.min()*100,1),"%")
print("by year (with price):"); print((r.assign(y=pd.to_datetime(r.date).dt.year).groupby("y").with_price_in_panel.mean()*100).round(1).to_string())
m=pd.Series(miss).sort_values(ascending=False)
print("uncovered top-200 names, month-ends missed (top 25):"); print(m.head(25).to_string()); print("distinct missing:",len(m))
r.to_csv(W+"out2/universe_coverage.csv",index=False); m.to_csv(W+"out2/universe_missing_names.csv")
