# data/external — survivorship-gap data (Nifty 200 PIT, 2016 → today)

Raw third-party files live here and are **not committed** (see `.gitignore`).
Download them by hand in a normal browser; do not script NSE/BSE site access.

```
data/external/
├── README.md                      # this file (committed)
├── reports/                       # JSON reports written by scripts (auto)
├── nifty200/
│   ├── ind_nifty200list.csv       # (A) current constituents
│   └── changes/                   # (B) index inclusion/exclusion sources
└── bse_prices/
    └── <NSE_SYMBOL>.csv           # (C) one file per stock, e.g. DHFL.csv
```

## (A) Current Nifty 200 constituents — 1 file
niftyindices.com → Indices → Equity → Broad Based → **NIFTY 200** → download the
constituent list (`ind_nifty200list.csv`). Save as `nifty200/ind_nifty200list.csv`.
Note the download date in the filename or a text file next to it.

Used to rebuild membership backward through the change log; the rebuilt list must
contain exactly 200 names on every date, which also checks the change log is complete.

## (B) Nifty 200 changes, July 2020 → today
The `pit_reconstitution_events` collection has genuine NSE archive events only up to
2020-07-31. Rows dated 2021-03-31 to 2024-09-30 (source `NSE_Official_Circular`) are a
hand-typed **Nifty 500** inclusion list with no exclusions — not usable for Nifty 200.

Put in `nifty200/changes/`:
1. The original NSE/Nifty Indices inclusion–exclusion archive file used to build
   `pit_reconstitution_events` (if you still have it) — it may already cover later years.
2. Otherwise, every Nifty Indices press release that changes **NIFTY 200**, from the
   Aug-2020 semi-annual review onward: the semi-annual reviews (announced ~Feb/Aug,
   effective last trading day of Mar/Sep) **and** ad-hoc replacements (mergers, demergers,
   delistings, e.g. HDFC/HDFC Bank 2023, Tata Motors demerger 2025).
   Name each `YYYY-MM-DD_<short-title>.pdf` using the effective date.

## (C0) Preferred alternative to (C): Kaggle combined NSE bhavcopy 2016–2024
https://www.kaggle.com/datasets/yashyennam/nse-bhavcopy-csv-jan-2016-dec-2024
(uploader-stated Apache-2.0, ~483 MB, last updated 2024-12-15, no stated provenance).
Log in to Kaggle, download `Combined_Data_2016_2024.csv`, and save it unmodified as
`kaggle_bhavcopy/Combined_Data_2016_2024.csv`. It is third-party data: before use it is
cross-checked against Upstox on surviving stocks and against known events (e.g. DHFL
21-Sep-2018). Covers the 20 still-missing stocks up to Dec 2024 if it is a true bhavcopy.

## (C) BSE daily price history — fallback if (C0) fails validation
bseindia.com → Markets → Equity → stock price history for the scrip; Period **Daily**,
From **01-Jan-2015** To the last available date; download CSV.
Save as `bse_prices/<NSE_SYMBOL>.csv` (left column below). If the company was renamed,
search BSE by the old or new name; note the name you used.

| NSE symbol | Company | Why needed |
|---|---|---|
| DHFL | Dewan Housing Finance | collapsed / delisted |
| VIDEOIND | Videocon Industries | collapsed / delisted |
| AMTEKAUTO | Amtek Auto | collapsed / delisted |
| COX&KINGS | Cox & Kings | collapsed / suspended (save as `COX_KINGS.csv`) |
| JPASSOCIAT | Jaiprakash Associates | collapsed |
| RELCAPITAL | Reliance Capital | collapsed / delisted |
| RNAVAL | Reliance Naval & Engineering (Reliance Defence) | collapsed |
| SINTEX | Sintex Industries | collapsed / delisted |
| DHANI | Dhani Services (Indiabulls Ventures) | not on yfinance |
| IBREALEST | Indiabulls Real Estate (now renamed) | not on yfinance |
| ALBK | Allahabad Bank | merged into Indian Bank (2020) |
| ANDHRABANK | Andhra Bank | merged into Union Bank (2020) |
| ORIENTBANK | Oriental Bank of Commerce | merged into PNB (2020) |
| SYNDIBANK | Syndicate Bank | merged into Canara Bank (2020) |
| BHARATFIN | Bharat Financial Inclusion | merged into IndusInd Bank (2019) |
| GRUH | Gruh Finance | merged into Bandhan Bank (2019) |
| GSKCONS | GSK Consumer Healthcare | merged into HUL (2020) |
| CAIRN | Cairn India | merged into Vedanta (2017) |
| ABNUVO | Aditya Birla Nuvo | merged into Grasim (2017) |
| IDFC | IDFC Ltd | merged into IDFC First Bank (2024) |
| TV18BRDCST | TV18 Broadcast | merged into Network18 |
| TATAMTRDVR | Tata Motors DVR | converted to ordinary shares (2024) |

Merger/conversion years above are from memory; verify against the BSE data's last date.

**No download needed** (renamed; already in `upstox_daily_ohlcv` under the new symbol):
ZOMATO → ETERNAL, LIC → LICI, CENTURYTEX → ABREL. To be recorded in an alias map.

BSE prices are expected to be **unadjusted** for splits/bonuses; the parser will be
written after the first real file arrives, and adjustment gaps are caught by
`price_validation.validate_ohlcv` (`large_overnight_gap`).

## Already done automatically (yfinance)
`python scripts/ingest_external_prices.py ingest --from-pit-gaps` filled 41 of the 66
missing ex-members into the `external_daily_ohlcv` collection (validated; placeholder
rows removed and listed in the report). `crosscheck` compares yfinance with Upstox on
stocks present in both — see `reports/`.
