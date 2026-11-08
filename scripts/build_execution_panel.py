"""Build a NEW versioned raw/adjusted panel from existing canonical bhavcopy data.

Never overwrites the research panel. No downloads or DB writes. Inferred price-gap
adjustments remain unverified; a held crossing will fail the corrected backtester.
"""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd


def build_panel(archive: Path, output: Path):
    if output.exists():
        raise FileExistsError(f"Preserve existing artifact: {output}")
    panel = pd.read_parquet(archive / "clean_panel.parquet")
    raw = pd.read_parquet(archive / "raw_canon_cache.parquet",
        columns=["canon", "date", "series", "open", "high", "low", "close", "volume"])
    raw["priority"] = raw.series.map({"EQ": 0, "BE": 1, "BZ": 2, "SM": 3})
    raw = raw.sort_values(["canon", "date", "priority", "volume"], ascending=[True, True, True, False])
    raw = raw.drop_duplicates(["canon", "date"]).rename(columns={"canon": "symbol"})
    cols = ["open", "high", "low", "close"]
    raw = raw.rename(columns={c: "raw_"+c for c in cols})
    panel = panel.merge(raw[["symbol", "date", "series"]+["raw_"+c for c in cols]],
        on=["symbol", "date", "series"], how="left", validate="one_to_one")
    if panel[["raw_"+c for c in cols]].isna().any().any():
        raise ValueError("PRICE_BASIS_REQUIRED: canonical raw panel has unmatched rows")
    panel["price_adjustment_factor"] = panel.close / panel.raw_close
    for c in cols:
        if not np.allclose(panel[c], panel["raw_"+c]*panel.price_adjustment_factor, rtol=1e-7):
            raise ValueError("PRICE_BASIS_INVALID: nonuniform OHLC adjustment")
    actions = pd.read_parquet(archive / "all_corporate_actions.parquet")
    actions["ex_date"] = pd.to_datetime(actions.ex_date)
    actions = actions[actions.kind.isin(["split", "bonus"]) & (actions.price_factor<.99)]
    events = {}
    for (symbol, date), g in actions.groupby(["symbol", "ex_date"]):
        events[(symbol,date)] = (1/g.loc[g.kind=='split','price_factor'].prod(),
                                 1/g.loc[g.kind=='bonus','price_factor'].prod())
    panel["split_ratio"], panel["bonus_ratio"], panel["share_action_verified"] = 1.0, 1.0, False
    # Only observed factor transitions with matching dated official-labelled actions are accepted.
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
    previous = panel.groupby("symbol", sort=False).price_adjustment_factor.shift()
    changed = previous.notna() & ~np.isclose(panel.price_adjustment_factor, previous, rtol=1e-8)
    for i in panel.index[changed]:
        row=panel.loc[i]
        event=events.get((row.symbol,row.date))
        if event and np.isclose(event[0]*event[1],row.price_adjustment_factor/previous.loc[i],rtol=1e-6):
            panel.loc[i,['split_ratio','bonus_ratio','share_action_verified']]=[*event,True]
    panel.to_parquet(output,index=False)
    print(f"Wrote {len(panel):,} rows to {output}; {int(changed.sum())} transitions, "
          f"{int(panel.share_action_verified.sum())} matched dated actions",flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=None)
    parser.add_argument('--output',type=Path,default=None)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    archive=args.archive or (root/'external/nse_archive')
    build_panel(archive,args.output or archive/'clean_panel_execution_v2.parquet')
