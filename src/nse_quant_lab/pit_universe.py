"""
pit_universe.py -- Point-in-Time (PIT) Index Constituent Calendar and Dynamic Liquidity Filter
=============================================================================================
Tracks official semi-annual NSE reconstitution additions and exclusions (March/September)
using a chronological state machine to correctly handle multi-event re-entrants
(stocks that exited in one cycle and re-entered in a later cycle).
"""

from typing import Dict, Optional, Set, List, Tuple
import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("pit_universe")

# Embedded official reconstitution events (effective_date, symbol, event_type)
OFFICIAL_RECONSTITUTION_EVENTS: List[Tuple[str, str, str]] = [('2016-04-01', 'GVT&D', 'EXCLUSION'), ('2016-04-01', 'JETAIRWAYS', 'INCLUSION'), ('2016-04-01', 'INOXWIND', 'INCLUSION'), ('2016-04-01', 'IOB', 'INCLUSION'), ('2016-04-01', 'INDHOTEL', 'INCLUSION'), ('2016-04-01', 'INDIANB', 'INCLUSION'), ('2016-04-01', 'IDFC', 'INCLUSION'), ('2016-04-01', 'JUBLPHARMA', 'INCLUSION'), ('2016-04-01', 'HINDZINC', 'INCLUSION'), ('2016-04-01', 'FLUOROCHEM', 'INCLUSION'), ('2016-04-01', 'GESHIP', 'INCLUSION'), ('2016-04-01', 'GLAXO', 'INCLUSION'), ('2016-04-01', 'FORTIS', 'INCLUSION'), ('2016-04-01', 'DISHTV', 'INCLUSION'), ('2016-04-01', 'CENTRALBK', 'INCLUSION'), ('2016-04-01', 'HINDCOPPER', 'INCLUSION'), ('2016-04-01', 'BEML', 'INCLUSION'), ('2016-04-01', 'MMTC', 'INCLUSION'), ('2016-04-01', 'MUTHOOTFIN', 'INCLUSION'), ('2016-04-01', 'WELSPUNLIV', 'INCLUSION'), ('2016-04-01', 'VIDEOIND', 'INCLUSION'), ('2016-04-01', 'UCOBANK', 'INCLUSION'), ('2016-04-01', 'TTKPRESTIG', 'INCLUSION'), ('2016-04-01', 'TORNTPOWER', 'INCLUSION'), ('2016-04-01', 'THERMAX', 'INCLUSION'), ('2016-04-01', 'MPHASIS', 'INCLUSION'), ('2016-04-01', 'RAMCOCEM', 'INCLUSION'), ('2016-04-01', 'TATAELXSI', 'INCLUSION'), ('2016-04-01', 'SYNGENE', 'INCLUSION'), ('2016-04-01', 'PGHH', 'INCLUSION'), ('2016-04-01', 'PFIZER', 'INCLUSION'), ('2016-04-01', 'PCJEWELLER', 'INCLUSION'), ('2016-04-01', 'NBCC', 'INCLUSION'), ('2016-04-01', 'TATAMTRDVR', 'INCLUSION'), ('2016-04-01', 'ARVIND', 'INCLUSION'), ('2016-04-01', 'NATIONALUM', 'INCLUSION'), ('2016-04-01', 'WABAG', 'EXCLUSION'), ('2016-04-01', 'JISLJALEQS', 'EXCLUSION'), ('2016-04-01', 'IBREALEST', 'EXCLUSION'), ('2016-04-01', 'INDIACEM', 'EXCLUSION'), ('2016-04-01', 'IFCI', 'EXCLUSION'), ('2016-04-01', 'HDIL', 'EXCLUSION'), ('2016-04-01', 'GATEWAY', 'EXCLUSION'), ('2016-04-01', 'J&KBANK', 'EXCLUSION'), ('2016-04-01', 'DCBBANK', 'EXCLUSION'), ('2016-04-01', 'CUB', 'EXCLUSION'), ('2016-04-01', 'CARERATING', 'EXCLUSION'), ('2016-04-01', 'BALKRISIND', 'EXCLUSION'), ('2016-04-01', 'ANDHRABANK', 'EXCLUSION'), ('2016-04-01', 'AMTEKAUTO', 'EXCLUSION'), ('2016-04-01', 'ADANIENT', 'INCLUSION'), ('2016-04-01', 'COX&KINGS', 'EXCLUSION'), ('2016-04-01', 'JKLAKSHMI', 'EXCLUSION'), ('2016-04-01', 'JPASSOCIAT', 'EXCLUSION'), ('2016-04-01', 'KTKBANK', 'EXCLUSION'), ('2016-04-01', 'UNITECH', 'EXCLUSION'), ('2016-04-01', 'SOUTHBANK', 'EXCLUSION'), ('2016-04-01', 'SOBHA', 'EXCLUSION'), ('2016-04-01', 'SINTEX', 'EXCLUSION'), ('2016-04-01', 'SADBHAV', 'EXCLUSION'), ('2016-04-01', 'KAJARIACER', 'EXCLUSION'), ('2016-04-01', 'RNAVAL', 'EXCLUSION'), ('2016-04-01', 'RAYMOND', 'EXCLUSION'), ('2016-04-01', 'REPCOHOME', 'EXCLUSION'), ('2016-04-01', 'PTC', 'EXCLUSION'), ('2016-04-01', 'PIIND', 'EXCLUSION'), ('2016-04-01', 'ORIENTBANK', 'EXCLUSION'), ('2016-04-01', 'NCC', 'EXCLUSION'), ('2016-04-01', 'KPITTECH', 'EXCLUSION'), ('2016-04-01', 'KSCL', 'EXCLUSION'), ('2016-04-01', 'RALLIS', 'EXCLUSION'), ('2016-09-30', 'INDIGO', 'INCLUSION'), ('2016-09-30', 'VIDEOIND', 'EXCLUSION'), ('2016-09-30', 'UCOBANK', 'EXCLUSION'), ('2016-09-30', 'TTKPRESTIG', 'EXCLUSION'), ('2016-09-30', 'REDINGTON', 'EXCLUSION'), ('2016-09-30', 'PIIND', 'INCLUSION'), ('2016-09-30', 'MFSL', 'INCLUSION'), ('2016-09-30', 'MMTC', 'EXCLUSION'), ('2016-09-30', 'IDFCFIRSTB', 'INCLUSION'), ('2016-09-30', 'ALBK', 'EXCLUSION'), ('2016-09-30', 'GESHIP', 'EXCLUSION'), ('2016-09-30', 'COROMANDEL', 'INCLUSION'), ('2016-09-30', 'CHOLAFIN', 'INCLUSION'), ('2016-09-30', 'BEML', 'EXCLUSION'), ('2016-09-30', 'ALKEM', 'INCLUSION'), ('2016-09-30', 'ABNUVO', 'INCLUSION'), ('2016-09-30', 'ABFRL', 'INCLUSION'), ('2016-09-30', 'HINDCOPPER', 'EXCLUSION'), ('2016-11-15', 'CROMPTON', 'INCLUSION'), ('2016-11-15', 'CAIRN', 'EXCLUSION'), ('2017-01-23', 'MFSL', 'EXCLUSION'), ('2017-01-23', 'RBLBANK', 'INCLUSION'), ('2017-03-31', 'LALPATHLAB', 'INCLUSION'), ('2017-03-31', 'MANAPPURAM', 'INCLUSION'), ('2017-03-31', 'LTTS', 'INCLUSION'), ('2017-03-31', 'ICICIPRULI', 'INCLUSION'), ('2017-03-31', 'ENDURANCE', 'INCLUSION'), ('2017-03-31', 'QUESS', 'INCLUSION'), ('2017-03-31', 'EDELWEISS', 'INCLUSION'), ('2017-03-31', 'DALBHARAT', 'INCLUSION'), ('2017-03-31', 'JUSTDIAL', 'EXCLUSION'), ('2017-03-31', 'PFIZER', 'EXCLUSION'), ('2017-03-31', 'PERSISTENT', 'EXCLUSION'), ('2017-03-31', 'JETAIRWAYS', 'EXCLUSION'), ('2017-03-31', 'INOXWIND', 'EXCLUSION'), ('2017-03-31', 'IOB', 'EXCLUSION'), ('2017-03-31', 'FLUOROCHEM', 'EXCLUSION'), ('2017-03-31', 'TATAELXSI', 'EXCLUSION'), ('2017-05-26', 'PNBHOUSING', 'INCLUSION'), ('2017-05-26', 'ABNUVO', 'EXCLUSION'), ('2017-05-26', 'GRASIM', 'EXCLUSION'), ('2017-05-26', 'MGL', 'INCLUSION'), ('2017-09-05', 'MFSL', 'INCLUSION'), ('2017-09-05', 'RELCAPITAL', 'EXCLUSION'), ('2017-09-29', 'MRPL', 'INCLUSION'), ('2017-09-29', 'FRETAIL', 'INCLUSION'), ('2017-09-29', 'BALKRISIND', 'INCLUSION'), ('2017-09-29', 'APLLTD', 'EXCLUSION'), ('2017-09-29', 'THERMAX', 'EXCLUSION'), ('2017-09-29', 'DMART', 'INCLUSION'), ('2017-09-29', 'QUESS', 'EXCLUSION'), ('2017-09-29', 'LTTS', 'EXCLUSION'), ('2017-10-30', 'FRETAIL', 'EXCLUSION'), ('2017-10-30', 'HUDCO', 'INCLUSION'), ('2018-02-05', 'VGUARD', 'INCLUSION'), ('2018-02-05', 'AUBANK', 'INCLUSION'), ('2018-02-05', 'RELCAPITAL', 'INCLUSION'), ('2018-02-05', 'FORTIS', 'EXCLUSION'), ('2018-02-05', 'CESC', 'EXCLUSION'), ('2018-02-05', 'STAR', 'EXCLUSION'), ('2018-04-02', 'ABCAPITAL', 'INCLUSION'), ('2018-04-02', 'SBILIFE', 'INCLUSION'), ('2018-04-02', 'ICICIGI', 'INCLUSION'), ('2018-04-02', 'DHANI', 'INCLUSION'), ('2018-04-02', 'IBREALEST', 'INCLUSION'), ('2018-04-02', 'GRASIM', 'INCLUSION'), ('2018-04-02', 'STLTECH', 'INCLUSION'), ('2018-04-02', 'GICRE', 'INCLUSION'), ('2018-04-02', 'FCONSUMER', 'INCLUSION'), ('2018-04-02', 'WELSPUNLIV', 'EXCLUSION'), ('2018-04-02', 'GODREJAGRO', 'INCLUSION'), ('2018-04-02', 'RCOM', 'EXCLUSION'), ('2018-04-02', 'SYNDIBANK', 'EXCLUSION'), ('2018-04-02', 'ADANIENT', 'EXCLUSION'), ('2018-04-02', 'CRISIL', 'EXCLUSION'), ('2018-04-02', 'DALBHARAT', 'EXCLUSION'), ('2018-04-02', 'GLAXO', 'EXCLUSION'), ('2018-04-02', 'DBL', 'INCLUSION'), ('2018-04-02', 'GSKCONS', 'EXCLUSION'), ('2018-04-02', 'IPCALAB', 'EXCLUSION'), ('2018-04-02', 'LALPATHLAB', 'EXCLUSION'), ('2018-04-02', 'GPPL', 'EXCLUSION'), ('2018-06-29', 'ARVIND', 'EXCLUSION'), ('2018-06-29', 'TATACOMM', 'EXCLUSION'), ('2018-06-29', 'FRETAIL', 'INCLUSION'), ('2018-06-29', 'QUESS', 'INCLUSION'), ('2018-09-28', 'NIACL', 'INCLUSION'), ('2018-09-28', 'LTIM', 'INCLUSION'), ('2018-09-28', 'HDFCLIFE', 'INCLUSION'), ('2018-09-28', 'HEG', 'INCLUSION'), ('2018-09-28', 'GRAPHITE', 'INCLUSION'), ('2018-09-28', 'ESCORTS', 'INCLUSION'), ('2018-09-28', 'BANDHANBNK', 'INCLUSION'), ('2018-09-28', 'AVANTIFEED', 'INCLUSION'), ('2018-09-28', 'IRB', 'EXCLUSION'), ('2018-09-28', 'SUZLON', 'EXCLUSION'), ('2018-09-28', 'KARURVYSYA', 'EXCLUSION'), ('2018-09-28', 'IBREALEST', 'EXCLUSION'), ('2018-09-28', 'IDFC', 'EXCLUSION'), ('2018-09-28', 'AIAENG', 'EXCLUSION'), ('2018-09-28', 'WOCKPHARMA', 'EXCLUSION'), ('2018-09-28', 'EMAMILTD', 'EXCLUSION'), ('2018-12-28', 'BBTC', 'INCLUSION'), ('2018-12-28', 'VARROC', 'INCLUSION'), ('2018-12-28', 'CENTURYTEX', 'EXCLUSION'), ('2018-12-28', 'BHARATFIN', 'EXCLUSION'), ('2019-03-29', 'LTTS', 'INCLUSION'), ('2019-03-29', 'AVANTIFEED', 'EXCLUSION'), ('2019-03-29', 'CENTRALBK', 'EXCLUSION'), ('2019-03-29', 'PCJEWELLER', 'EXCLUSION'), ('2019-03-29', 'TV18BRDCST', 'EXCLUSION'), ('2019-03-29', 'VAKRANGEE', 'EXCLUSION'), ('2019-03-29', 'BAJAJHLDNG', 'INCLUSION'), ('2019-03-29', 'GSKCONS', 'INCLUSION'), ('2019-03-29', 'HDFCAMC', 'INCLUSION'), ('2019-03-29', 'PFIZER', 'INCLUSION'), ('2019-09-27', 'IPCALAB', 'INCLUSION'), ('2019-09-27', 'NESTLEIND', 'INCLUSION'), ('2019-09-27', 'ORIENTBANK', 'INCLUSION'), ('2019-09-27', 'RPOWER', 'EXCLUSION'), ('2019-09-27', 'RELINFRA', 'EXCLUSION'), ('2019-09-27', 'SPARC', 'EXCLUSION'), ('2019-09-27', 'GRUH', 'EXCLUSION'), ('2019-09-27', 'RELCAPITAL', 'EXCLUSION'), ('2019-09-27', 'GODREJPROP', 'INCLUSION'), ('2019-09-27', 'NAM-INDIA', 'INCLUSION'), ('2019-09-27', 'EMAMILTD', 'INCLUSION'), ('2019-09-27', 'DISHTV', 'EXCLUSION'), ('2019-09-27', 'DBL', 'EXCLUSION'), ('2019-09-27', 'DHFL', 'EXCLUSION'), ('2019-09-27', 'DALBHARAT', 'INCLUSION'), ('2019-09-27', 'CUB', 'INCLUSION'), ('2019-09-27', 'CESC', 'INCLUSION'), ('2019-09-27', 'GSKCONS', 'EXCLUSION'), ('2019-09-27', 'ALBK', 'INCLUSION'), ('2019-09-27', 'ABB', 'EXCLUSION'), ('2019-12-27', 'WHIRLPOOL', 'INCLUSION'), ('2019-12-27', 'VBL', 'INCLUSION'), ('2019-12-27', 'TATACHEM', 'EXCLUSION'), ('2019-12-27', 'ORIENTBANK', 'EXCLUSION'), ('2019-12-27', 'FORTIS', 'INCLUSION'), ('2019-12-27', 'ALBK', 'EXCLUSION'), ('2020-03-19', 'ADANIENSOL', 'INCLUSION'), ('2020-03-19', 'YESBANK', 'EXCLUSION'), ('2020-03-27', 'YESBANK', 'EXCLUSION'), ('2020-06-26', 'ATGL', 'INCLUSION'), ('2020-06-26', 'VARROC', 'EXCLUSION'), ('2020-06-26', 'NIACL', 'EXCLUSION'), ('2020-06-26', 'LALPATHLAB', 'INCLUSION'), ('2020-06-26', 'GUJGASLTD', 'INCLUSION'), ('2020-06-26', 'TATAMTRDVR', 'EXCLUSION'), ('2020-06-26', 'FCONSUMER', 'EXCLUSION'), ('2020-06-26', 'NBCC', 'EXCLUSION'), ('2020-06-26', 'AARTIIND', 'INCLUSION'), ('2020-06-26', 'COFORGE', 'INCLUSION'), ('2020-06-26', 'STLTECH', 'EXCLUSION'), ('2020-06-26', 'ENGINERSIN', 'EXCLUSION'), ('2020-06-26', 'TRENT', 'INCLUSION'), ('2020-06-26', 'ABBOTINDIA', 'INCLUSION'), ('2020-06-26', 'HEG', 'EXCLUSION'), ('2020-06-26', 'GRAPHITE', 'EXCLUSION'), ('2020-06-26', 'POLYCAB', 'INCLUSION'), ('2020-06-26', 'INDIANB', 'EXCLUSION'), ('2020-06-26', 'IRCTC', 'INCLUSION'), ('2020-07-31', 'VEDL', 'EXCLUSION'), ('2020-07-31', 'SBICARD', 'INCLUSION'), ('2021-03-31', 'HAPPSTMNDS', 'INCLUSION'), ('2021-03-31', 'ROUTE', 'INCLUSION'), ('2021-03-31', 'CAMS', 'INCLUSION'), ('2021-03-31', 'UTIAMC', 'INCLUSION'), ('2021-03-31', 'ATGL', 'INCLUSION'), ('2021-09-30', 'CLEAN', 'INCLUSION'), ('2021-09-30', 'GRINFRA', 'INCLUSION'), ('2021-09-30', 'SAREGAMA', 'INCLUSION'), ('2022-03-31', 'MAZDOCK', 'INCLUSION'), ('2022-03-31', 'ZOMATO', 'INCLUSION'), ('2022-03-31', 'NYKAA', 'INCLUSION'), ('2022-03-31', 'PAYTM', 'INCLUSION'), ('2022-03-31', 'ANGELONE', 'INCLUSION'), ('2022-03-31', 'DEVYANI', 'INCLUSION'), ('2022-03-31', 'KPITTECH', 'INCLUSION'), ('2022-09-30', 'COCHINSHIP', 'INCLUSION'), ('2022-09-30', 'POLICYBZR', 'INCLUSION'), ('2022-09-30', 'DELHIVERY', 'INCLUSION'), ('2022-09-30', 'LIC', 'INCLUSION'), ('2023-03-31', 'DATAPATTNS', 'INCLUSION'), ('2023-03-31', 'CAMPUS', 'INCLUSION'), ('2023-03-31', 'RAINBOW', 'INCLUSION'), ('2023-03-31', 'MEDANTA', 'INCLUSION'), ('2023-03-31', 'APARINDS', 'INCLUSION'), ('2023-09-01', 'JIOFIN', 'INCLUSION'), ('2023-09-29', 'TITAGARH', 'INCLUSION'), ('2023-09-29', 'BSE', 'INCLUSION'), ('2023-09-29', 'KAYNES', 'INCLUSION'), ('2023-09-29', 'SUZLON', 'INCLUSION'), ('2023-09-29', 'MANKIND', 'INCLUSION'), ('2023-09-29', 'MAHLIFE', 'INCLUSION'), ('2023-09-29', 'JBMA', 'INCLUSION'), ('2023-09-29', 'ZENTEC', 'INCLUSION'), ('2023-09-29', 'HBLENGINE', 'INCLUSION'), ('2023-09-29', 'TEJASNET', 'INCLUSION'), ('2024-03-28', 'GRAVITA', 'INCLUSION'), ('2024-03-28', 'DOMS', 'INCLUSION'), ('2024-03-28', 'IREDA', 'INCLUSION'), ('2024-03-28', 'MOTISONS', 'INCLUSION'), ('2024-03-28', 'INDOCO', 'INCLUSION'), ('2024-03-28', 'SBFC', 'INCLUSION'), ('2024-03-28', 'GVT&D', 'INCLUSION'), ('2024-03-28', 'IFCI', 'INCLUSION'), ('2024-03-28', 'TARIL', 'INCLUSION'), ('2024-03-28', 'LLOYDSENGG', 'INCLUSION'), ('2024-03-28', 'KIRLOSBROS', 'INCLUSION'), ('2024-03-28', 'ANANDRATHI', 'INCLUSION'), ('2024-09-30', 'FORCEMOT', 'INCLUSION')]


class PointInTimeUniverse:
    """
    Evaluates whether a stock was an official index constituent on a given date.
    Implements a robust chronological state machine:
    - If as_of_date is on or after an event, the most recent event determines membership.
    - If as_of_date is before the earliest event, 'INCLUSION' means not entered yet,
      while 'EXCLUSION' means it was in the index prior to being excluded.
    """

    def __init__(self, enforce_pit: bool = True, mongo_uri: Optional[str] = None):
        self.enforce_pit = enforce_pit
        self.events_by_sym: Dict[str, List[Tuple[str, str]]] = {}
        self._load_events(mongo_uri)

    def _load_events(self, mongo_uri: Optional[str] = None):
        loaded = False
        try:
            from pymongo import MongoClient
            uri = mongo_uri or "mongodb://localhost:27017/"
            client = MongoClient(uri, serverSelectionTimeoutMS=1500)
            col = client["stock_scanner"]["pit_reconstitution_events"]
            docs = list(col.find().sort("effective_date", 1))
            if docs:
                for d in docs:
                    sym = d["symbol"].replace(".NS", "").replace(".BO", "")
                    if sym not in self.events_by_sym:
                        self.events_by_sym[sym] = []
                    self.events_by_sym[sym].append((d["effective_date"], d["event_type"]))
                loaded = True
        except Exception:
            pass

        if not loaded or not self.events_by_sym:
            for dt, sym, ev_type in OFFICIAL_RECONSTITUTION_EVENTS:
                clean_sym = sym.replace(".NS", "").replace(".BO", "")
                if clean_sym not in self.events_by_sym:
                    self.events_by_sym[clean_sym] = []
                self.events_by_sym[clean_sym].append((dt, ev_type))

        for sym in self.events_by_sym:
            self.events_by_sym[sym].sort(key=lambda x: x[0])

    @property
    def inclusions(self) -> Dict[str, pd.Timestamp]:
        inc = {}
        for sym, evs in self.events_by_sym.items():
            incls = [e[0] for e in evs if e[1] == "INCLUSION"]
            if incls:
                inc[sym] = pd.to_datetime(incls[0])
        return inc

    @property
    def exclusions(self) -> Dict[str, pd.Timestamp]:
        exc = {}
        for sym, evs in self.events_by_sym.items():
            excls = [e[0] for e in evs if e[1] == "EXCLUSION"]
            if excls:
                exc[sym] = pd.to_datetime(excls[0])
        return exc

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
        dt_str = as_of_dt.strftime("%Y-%m-%d")

        # 1. Cross-Sectional Liquidity Set Check (if provided)
        if top_liquid_set is not None:
            if symbol not in top_liquid_set and clean_sym not in top_liquid_set:
                return False

        # 2. Chronological State-Machine Evaluation
        events = self.events_by_sym.get(clean_sym)
        if events:
            past_events = [e for e in events if e[0] <= dt_str]
            if past_events:
                # Most recent reconstitution event determines active status
                if past_events[-1][1] == "EXCLUSION":
                    return False
            else:
                # as_of_date is strictly before the earliest event
                if events[0][1] == "INCLUSION":
                    return False

        return True

    @staticmethod
    def get_top_liquid_universe(
        universe: Dict[str, pd.DataFrame],
        decision_date: pd.Timestamp,
        top_n: int = 200,
        max_universe_pct: float = 0.45,
    ) -> Set[str]:
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
