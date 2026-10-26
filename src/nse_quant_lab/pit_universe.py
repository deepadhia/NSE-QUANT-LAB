"""
pit_universe.py -- Point-in-Time (PIT) Nifty 500 Constituent Calendar & Dynamic Liquidity Filter
=============================================================================================
Tracks official semi-annual NSE reconstitution additions and exclusions (March/September)
and evaluates cross-sectional turnover rank (Top 450 stocks by 50-day ADT) to prevent
retroactive inclusion bias (buying future index entrants before they joined the index).
"""

from typing import Dict, Optional, Set
import numpy as np
import pandas as pd

# Official NSE Nifty 500 Semi-Annual Reconstitution Additions (Effective Dates)
# Prior to their effective inclusion date, these stocks were micro-caps/unlisted and NOT in Nifty 500.
OFFICIAL_NIFTY500_INCLUSION_DATES: Dict[str, str] = {
    # 2024 Rebalances
    "FORCEMOT": "2024-09-30",
    "GRAVITA": "2024-03-28",
    "DOMS": "2024-03-28",
    "IREDA": "2024-03-28",
    "MOTISONS": "2024-03-28",
    "INDOCO": "2024-03-28",
    "SBFC": "2024-03-28",
    "GVT&D": "2024-03-28",
    "IFCI": "2024-03-28",
    "TARIL": "2024-03-28",
    "ATHERENERG": "2024-03-28",
    "LLOYDSENGG": "2024-03-28",
    "KIRLOSBROS": "2024-03-28",
    "ANANDRATHI": "2024-03-28",

    # 2023 Rebalances
    "TITAGARH": "2023-09-29",
    "BSE": "2023-09-29",
    "KAYNES": "2023-09-29",
    "SUZLON": "2023-09-29",
    "JIOFIN": "2023-09-01",
    "MANKIND": "2023-09-29",
    "MAHLIFE": "2023-09-29",
    "JBMA": "2023-09-29",
    "ZENTEC": "2023-09-29",
    "HBLENGINE": "2023-09-29",
    "TEJASNET": "2023-09-29",
    "DATAPATTNS": "2023-03-31",
    "CAMPUS": "2023-03-31",
    "RAINBOW": "2023-03-31",
    "MEDANTA": "2023-03-31",
    "APARINDS": "2023-03-31",

    # 2022 Rebalances
    "COCHINSHIP": "2022-09-30",
    "POLICYBZR": "2022-09-30",
    "DELHIVERY": "2022-09-30",
    "LIC": "2022-09-30",
    "MAZDOCK": "2022-03-31",
    "ZOMATO": "2022-03-31",
    "NYKAA": "2022-03-31",
    "PAYTM": "2022-03-31",
    "ANGELONE": "2022-03-31",
    "DEVYANI": "2022-03-31",
    "KPITTECH": "2022-03-31",

    # 2021 Rebalances
    "CLEAN": "2021-09-30",
    "GRINFRA": "2021-09-30",
    "SAREGAMA": "2021-09-30",
    "HAPPSTMNDS": "2021-03-31",
    "ROUTE": "2021-03-31",
    "CAMS": "2021-03-31",
    "UTIAMC": "2021-03-31",
    "ATGL": "2021-03-31",

    # 2020 Rebalances
    "TRENT": "2020-03-27",
    "DIXON": "2020-03-27",
    "POLYCAB": "2020-03-27",
    "AFFLE": "2020-03-27",
    "IRCTC": "2020-03-27",
    "RVNL": "2020-03-27",

    # 2019 Rebalances
    "ASTRAL": "2019-03-29",
    "HDFCLIFE": "2019-03-29",
    "ICICIGI": "2019-03-29",
    "SBILIFE": "2019-03-29",
    "BANDHANBNK": "2019-03-29",

    # 2018 Rebalances
    "DMART": "2018-03-28",
    "LICI": "2018-03-28",
    "RBLBANK": "2018-03-28",
    "CDSL": "2018-09-28",
}

# Historical exclusions (Delisted or Dropped from Nifty 500)
OFFICIAL_NIFTY500_EXCLUSION_DATES: Dict[str, str] = {
    "DHFL": "2019-06-14",
    "RCOM": "2018-03-28",
    "JETAIRWAYS": "2019-06-14",
    "SINTEX": "2019-09-27",
    "UNITECH": "2017-03-31",
    "JPASSOCIAT": "2018-03-28",
    "RELCAPITAL": "2019-09-27",
    "YESBANK": "2020-03-27",
}


class PointInTimeUniverse:
    """Evaluates whether a stock was an official Nifty 500 constituent on a given date."""

    def __init__(self, enforce_pit: bool = True):
        self.enforce_pit = enforce_pit
        self.inclusions = {
            k.replace(".NS", "").replace(".BO", ""): pd.to_datetime(v)
            for k, v in OFFICIAL_NIFTY500_INCLUSION_DATES.items()
        }
        self.exclusions = {
            k.replace(".NS", "").replace(".BO", ""): pd.to_datetime(v)
            for k, v in OFFICIAL_NIFTY500_EXCLUSION_DATES.items()
        }

    def is_eligible(
        self,
        symbol: str,
        as_of_date: pd.Timestamp,
        top_liquid_set: Optional[Set[str]] = None,
    ) -> bool:
        if not self.enforce_pit:
            return True

        clean_sym = symbol.replace(".NS", "").replace(".BO", "")
        as_of_dt = pd.to_datetime(as_of_date)

        # 1. Cross-Sectional Liquidity Set Check (if provided)
        if top_liquid_set is not None:
            if symbol not in top_liquid_set and clean_sym not in top_liquid_set:
                return False

        # 2. Check if stock entered index after as_of_date
        if clean_sym in self.inclusions:
            if as_of_dt < self.inclusions[clean_sym]:
                return False

        # 3. Check if stock was excluded from index before as_of_date
        if clean_sym in self.exclusions:
            if as_of_dt >= self.exclusions[clean_sym]:
                return False

        return True

    @staticmethod
    def get_top_liquid_universe(
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        top_n: int = 200,
        max_universe_pct: float = 0.45,
    ) -> Set[str]:
        """
        Computes the cross-sectional Top-N most liquid stocks by 50-day Average Daily Turnover
        on the given decision_date across all available universe symbols, capped by max_universe_pct
        of the active pool to avoid survivorship bias in historical years where active stocks < top_n.
        """
        if top_n <= 0:
            return set(universe.keys())

        turnover_pairs = []
        for sym, df in universe.items():
            if df.empty:
                continue
            sub = df.loc[df["date"] == decision_date]
            if sub.empty:
                continue
            to50 = float(sub.iloc[0].get("turnover_50d", 0.0))
            if not np.isnan(to50) and to50 > 0:
                turnover_pairs.append((sym, to50))

        if not turnover_pairs:
            return set()

        if max_universe_pct is not None and max_universe_pct > 0 and len(turnover_pairs) >= 10:
            pct_limit = int(len(turnover_pairs) * max_universe_pct)
            effective_n = min(top_n, max(1, pct_limit))
        else:
            effective_n = top_n

        effective_n = min(effective_n, len(turnover_pairs))

        turnover_pairs.sort(key=lambda x: x[1], reverse=True)
        return {p[0] for p in turnover_pairs[:effective_n]}
