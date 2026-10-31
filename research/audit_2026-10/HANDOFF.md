# Session handoff — trend_momentum audit & survivorship-free test

Written 2026-10-28 (session started 2026-10-27). **Nothing has been committed** (user's
instruction: do not commit). Read this whole file before continuing.

## 1. Goal (user's words, paraphrased)
Find out honestly whether `trend_momentum` has real alpha over passive investing, with a
"fully true" point-in-time backtest for **2016 → today**, and make the algo robust and
trustworthy. User capital in mind: ₹1 lakh (paper first). User wants no tuning-to-target;
honest, verified results only (see `AGENTS.md`, `BACKTEST_VALIDATION.md`).

## 2. Where the evidence stands (VERIFIED from runs this session)

### 2a. Original audit (saved results in `results/`)
- Committed code reproduces `results/realistic_pit_max_period` exactly (₹3,39,53,149.64).
- DB `upstox_daily_ohlcv` = 501 symbols, **all still trading on 2026-10-23** → survivor-only.
- No-skill equal-weight basket of the same survivors beat Nifty 500 by 4–7 pp/yr → most
  headline "alpha" was survivorship. Momentum ranking itself does beat random picks within
  the survivor universe (~99th pct).
- Saved true-PIT 10-yr run (`results/true_pit_10yr_2016_2026`, regenerated 22:22 by another
  session): 11.71% post-tax vs Nifty 500 11.27%; top-10 trades = 119% of profit; t = 0.43.

### 2b. Pre-registered protocol (`PROTOCOL.md`), 12 configs, survivor data
Rule picked monthly / 6 positions / no regime on 2006–2017 (IR 0.361). Holdout 2018–2026:
16.65% pre-tax vs N500 9.76%, passed S1–S3, but t = 1.11 and top-10 = 135% of profit.
Regime filter (N500 < 200-SMA → cash) **hurt returns in every holdout variant**.
Files: `results/protocol_*.csv`, `results/protocol_selected.json`.

### 2c. Near-true 2016–2026 test (`scripts/near_true_test.py`, `capital_check.py`)
Universe = top-200 by 50-day turnover each date (past data only, no index events),
Upstox 500 + 43 validated ex-Nifty-200 stocks. 20 bps/leg, all fees, ₹10 lakh unless noted.

| Version | Pre-tax CAGR | 40 bps | Post-tax | Max DD | t |
|---|---|---|---|---|---|
| 6 positions (current algo) | 14.46% | — | — | −48.6% | 0.96 |
| **15 positions** (hold buffer 38) | **17.85%** | 15.61%* | 14.85%* | −33.5% | 1.43 |
| 15 positions at ₹1 lakh | 17.71% | | | −33.0% | 1.40 |
| No-skill basket, same universe (gross) | 13.97% | | | | |
| Nifty 500 **price** index | 11.27% | | | −38.3% | |
*from the 41-external-stock run (17.87% pre-tax base); 43-stock run differs by ≤0.1 pp.

Conclusions: 6 positions ≈ no-skill basket and is fragile (adding 41 stocks changed 28 of
357 trades and cut CAGR 3.6 pp by displacing TATAELXSI/ADANIENT/DEEPAKNTR). 15 positions is
robust to universe, cost and capital changes, beats no-skill by ~3.9 pp, but **not
statistically significant** and 2025 (−21.5% vs +6.2%) / 2026 YTD (−13.8%) are bad.
Choosing 15 over 6 is post-hoc (the pre-registered rule picked 6). Benchmark excludes
dividends (TRI ≈ +1.3 pp/yr). Still missing 20 stocks incl. 7 true collapses (§4).

### 2d. Proposed "fixes" from another assistant — checked against full daily paths (15-pos run)
- Hold-rule inconsistency: **real** — 384/457 REBALANCE_DROPs happen because a held stock fails
  the *entry* filter (Supertrend/1.03×EMA) while still above EMA100. But dropped stocks then did
  only +1.3% (21d) / +3.5% (63d) on average → the "₹21.66 L lost" claim was selection-biased
  (counted only re-bought names). Worth an isolated test → **§3 in-flight**.
- Entry caps (≤1.35×EMA, ≥0.85×52w high): REJECTED — blocked trades averaged +8.3% vs +3.5%,
  incl. 7 of top-20 winners.
- −12% hard stop: REJECTED — with next-open exits, net −₹3.05 L (saves ₹6.6 L on losers,
  gives up ₹9.6 L on recoveries; gap exits avg −14.2%, worst −32.5%).
- 40% breadth gate: untested; threshold chosen post-hoc; similar regime filter already hurt.

## 3. Hold-rule A/B — DONE, pre-registered verdict: KEEP (not yet applied to the strategy)
Protocol: `PROTOCOL_HOLDRULE.md` (written before running). Script: `scripts/ab_holdrule.py`.
Results: `results/holdrule_ab.csv`. 15 positions, buffer 38, ₹10 lakh, 20 bps/leg.
Rule: keep only if post-tax CAGR higher AND fewer trades in BOTH windows → passed both.

| Window | Arm | Pre-tax | Post-tax | Max DD (post-tax) | Trades | Holds ≥365d | t (post-tax) |
|---|---|---|---|---|---|---|---|
| 2006–2015 | baseline | 23.57% | 19.25% | −55.9% | 679 | 0 | 1.10 |
| 2006–2015 | holdfix | 25.34% | 21.69% | −57.9% | 433 | 9 | 1.40 |
| 2016–2026 | baseline | 17.85% | 14.88% | −34.8% | 815 | 1 | 0.92 |
| 2016–2026 | holdfix | 20.40% | 18.78% | −34.6% | 500 | 9 | 1.57 |
Nifty 500 price index: 10.57% (W1), 11.27% (W2). Baseline W2 pre-tax reproduced 17.85% exactly.

Caveats: still not significant (best t = 1.86 pre-tax W2). 2006–2015 is survivors-only (external
stocks start 2014), and holding longer may benefit more from the absence of collapsed stocks —
re-check the fix on the Kaggle/delisted data before trusting it. Drawdown slightly worse in W1.
The fix exists only as research subclass `HoldFix`; `trend_momentum.py` is unchanged — apply
it (with a regression test) only with the user's OK.

## 4. Data state (MongoDB **local**: `mongodb://localhost:27017/`, db `stock_scanner`)
- `upstox_daily_ohlcv`: primary research data (501 symbols, survivors). Split/bonus-adjusted,
  not dividend-adjusted. Known defect: SIEMENS 2025-04-07 demerger **not adjusted** (others with
  >30% unexplained gaps: ABFRL 2025-05-22, VEDL 2026-04-30, TMPV 2025-10-14, SKFINDIA, HEG,
  TRIVENI) — none hit trades so far. Junk pre-listing rows exist (360ONE, HOMEFIRST, ANGELONE…).
- `external_daily_ohlcv` (NEW, written this session): 43 symbols, unique index
  (symbol,date,source). Sources: `yfinance` (41 ex-Nifty-200 stocks, 2014→), `yfinance_bse`
  (VIDEOIND via VIDEOIND.BO, real trading to 2021-06-15), `upstox_isin` (IBREALEST via ISIN
  INE069I01010). All validated; yfinance placeholder rows (prev close, 0 volume) removed and
  listed in `data/external/reports/*.json` (gitignored).
- `pit_reconstitution_events`: genuine NSE NIFTY_200 events only **2016-04-01 → 2020-07-31**.
  The 49 rows 2021-03-31 → 2024-09-30 (source `NSE_Official_Circular`) are the old hand-typed
  **Nifty 500** inclusion list mislabeled NIFTY_200, no exclusions. Do not trust them.
- **`.env` MONGO_URI points to an Atlas cluster** (`cluster0…mongodb.net`) that was failing TLS.
  Anything calling `load_clean_universe()` / `MongoDatabase()` without an explicit URI goes there.
  Research scripts pass localhost explicitly. Paper trading may be pointing at Atlas — check.
- Upstox API quirk: some instruments return daily candles stamped 09:15 and a duplicate bar
  (seen for INE069I01010 from 2015-12-31). Live ingestion is unaffected (keys by date string).

## 5. Code changed this session (all uncommitted)
Mine:
- `src/nse_quant_lab/data_loader.py` — `load_clean_universe()` raises `ConnectionError` instead
  of silently loading the 48-stock sample; sample only with `allow_sample_fallback=True` (logged).
- `src/nse_quant_lab/price_validation.py` (new) — `validate_ohlcv`, `raise_on_errors`,
  `compare_sources`, `normalize_session_dates`, `drop_carried_forward_rows`, `carried_forward_mask`.
- `src/nse_quant_lab/external_prices.py` (new) — yfinance fetch/normalize, idempotent upsert.
- `scripts/ingest_external_prices.py` (new) — `crosscheck` and `ingest` subcommands, `--mongo-uri`
  (default localhost), JSON reports to `data/external/reports/`.
- Tests (new): `tests/test_data_loader.py`, `tests/test_price_validation.py`,
  `tests/test_external_prices.py`. **Full suite: 71 passed** (last run this session).
- `.gitignore` — ignores `data/external/*` except `data/external/README.md`.
- `data/external/README.md` — manual-download instructions; empty drop folders created.
- `research/audit_2026-10/` — this handoff, protocols, research scripts, small result CSVs.

NOT mine — another session's uncommitted edits, do not touch without asking:
`src/nse_quant_lab/pit_universe.py` (Mongo-backed event state machine),
`tests/test_audit_regressions.py`, and untracked `results/realistic_pit_10yr_2016_2026_nifty200/`,
`results/realistic_pit_max_period_nifty200/`, `results/true_pit_10yr_2016_2026/`.

## 6. Waiting on the user / open decisions
1. **Kaggle file**: user said it was saved, but `data/external/kaggle_bhavcopy/` was empty and no
   `Combined_Data_2016_2024*` was found in Downloads/Desktop/Documents/D:\. Probably still a zip
   (`archive.zip`) or elsewhere. Dataset: kaggle.com/datasets/yashyennam/nse-bhavcopy-csv-jan-2016-dec-2024
   (uploader-stated Apache-2.0, ~483 MB, updated 2024-12-15, no provenance). Treat as untrusted:
   inspect columns first, write the parser after seeing it, cross-check vs Upstox on survivors
   (`compare_sources`), check DHFL 2018-09-21 crash, Jet vs yfinance, then adjust raw prices for
   splits/bonuses (bhavcopy is unadjusted) before ingesting the 20 missing stocks.
2. **20 still-missing ex-Nifty-200 stocks**: true collapses DHFL, AMTEKAUTO, COX&KINGS, JPASSOCIAT,
   RELCAPITAL, RNAVAL, SINTEX; mergers/conversions ALBK, ANDHRABANK, ORIENTBANK, SYNDIBANK,
   BHARATFIN, GRUH, GSKCONS, CAIRN, ABNUVO, IDFC, TV18BRDCST, TATAMTRDVR; DHANI. Renames already
   in DB: ZOMATO→ETERNAL, LIC→LICI, CENTURYTEX→ABREL. Fallback: BSE per-stock CSVs (README §C).
3. **Nifty 200 files** for the exact Nifty-200 PIT version: current list + changes from Aug 2020
   (README §A, §B). Membership rebuilt backward must equal 200 names on every date.
4. **09:16 paper circuit-lock policy** (`paper/manager.py` ~L531, ~L647): current check falsely
   defers exits for liquid stocks that gap ≥4.8% (1-minute snapshot looks flat). The other
   assistant's patch (skip the check for live quotes) would create phantom fills in genuine locks.
   Correct fix needs each stock's NSE price band (most Nifty-200 names are F&O, no fixed 5% band).
   **Ask the user which policy before changing.**
5. **Commit**: user said not to commit. Ask before committing anything.

## 7. How to resume / reproduce
Research scripts expect a working folder `SCR` (hard-coded at top of each script — edit it) with:
```
ohlcv.parquet                # Upstox research data snapshot
wt_src/src/                  # copy of the repo's src/ used for runs
```
Recreate them:
```python
# python -c with these lines, SCR = your folder
import pandas as pd; from pymongo import MongoClient
c = MongoClient("mongodb://localhost:27017/")["stock_scanner"]["upstox_daily_ohlcv"]
df = pd.DataFrame(list(c.find({"symbol": {"$nin": ["^NSEI", "^CRSLDX"]}},
     {"_id": 0, "symbol": 1, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})))
df["date"] = pd.to_datetime(df.date); df.to_parquet(SCR + "ohlcv.parquet")
```
and `cp -r src "$SCR/wt_src/"`. Then e.g.:
```
python ab_holdrule.py prep && python ab_holdrule.py run      # hold-rule A/B (~6–10 min, 8 procs)
python near_true_test.py prep && python near_true_test.py run
python ../../../scripts/ingest_external_prices.py crosscheck --n 40 --end 2026-10-23
```
`fetch_sample_BLOCKED_do_not_run.py` is kept only as a record: running it was blocked as
bot-check circumvention of NSE; do not retry scripted NSE/BSE site access.

## 8. Suggested order tomorrow
1. **User approved (2026-10-28): apply the hold-rule fix to `trend_momentum.py` with a regression
   test, do NOT commit.** Implement exactly the `HoldFix` behaviour from `scripts/ab_holdrule.py`:
   in `select_entry_candidates`, an already-held stock stays in the ranked pool while
   close >= EMA100 (no liquidity/Supertrend/1.03×EMA checks for held names); new candidates keep
   the full entry filter. Update the stale docstring at the same time (ranking is blended 6M/12M ÷
   vol; daily exit is EMA100 only). Regression test: a held stock with Supertrend bearish, or
   close between EMA100 and 1.03×EMA100, must stay in the ranked pool and not get a
   REBALANCE_DROP, while the same stock not held must be excluded. Note paper trading uses this
   strategy, so the change affects live paper behaviour — tell the user. Run the full suite.
2. Locate the Kaggle file with the user; validate; ingest the 20 stocks; re-run §2c (15 and 6 pos).
3. Ask the user about the 09:16 policy; implement with tests.
4. If user provides Nifty 200 files: build exact Nifty-200 PIT membership and re-run.
5. Freeze a written spec of the chosen rules before any paper trading; paper-trade only, no real money.

## 9. Honesty guardrails for whoever continues
- Every 2005–2026 period has now been looked at; any new rule tested on it is in-sample.
  Require improvement in two windows and pre-register; never present a fitted tweak as proof.
- Report t-stats and concentration with every CAGR. Don't call the edge "proven".
- Results are price-only vs a price index; say so. ₹1-lakh affordability uses back-adjusted prices
  (optimistic for stocks that later split).
- Not investment advice; don't recommend specific funds/allocations.
