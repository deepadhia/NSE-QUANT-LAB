#!/usr/bin/env python3
"""
NSE Bhavcopy Downloader, Archiver, and Ingestor.

Provides end-to-end official data pipeline for Point-in-Time (PIT) backtesting:
1. Download official NSE daily equity bhavcopies (2006-01-01 to present) directly from nsearchives.nseindia.com.
2. Archive daily files into fast, compressed annual Parquet files.
3. Ingest standardized daily OHLCV + Turnover into MongoDB.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Generator, Iterable, List, Optional, Set, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nse_bhavcopy")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
FIRST_DATE = dt.date(2006, 1, 1)
UDIFF_START_DATE = dt.date(2024, 7, 8)
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM"}


def get_bhav_url(target_date: dt.date) -> str:
    """Return official NSE URL for equity bhavcopy on target_date."""
    if target_date >= UDIFF_START_DATE:
        return (
            f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{target_date:%Y%m%d}_F_0000.csv.zip"
        )
    mon = target_date.strftime("%b").upper()
    return (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
        f"{target_date.year}/{mon}/cm{target_date:%d}{mon}{target_date.year}bhav.csv.zip"
    )


def curl_download(url: str, dest_path: str, timeout: int = 45, max_attempts: int = 4) -> int:
    """
    Download url to dest_path atomically using curl.exe.
    Returns HTTP status code (200, 404, etc.) or 0 on failure.
    """
    tmp_path = dest_path + ".part"
    for attempt in range(max_attempts):
        args = [
            "curl",
            "-s",
            "-L",
            "-m",
            str(timeout),
            "-A",
            DEFAULT_UA,
            "-w",
            "%{http_code}",
            "-o",
            tmp_path,
            url,
        ]
        try:
            res = subprocess.run(args, capture_output=True)
            code_str = res.stdout.decode("ascii", errors="ignore").strip()
            code = int(code_str) if code_str.isdigit() else 0
        except Exception:
            code = 0

        if code == 200:
            if os.path.exists(tmp_path):
                os.replace(tmp_path, dest_path)
            return 200
        if code == 404:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return 404

        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        time.sleep(2 * (attempt + 1))

    return code


# =====================================================================
# 1. DOWNLOAD COMMAND
# =====================================================================

def run_download(
    out_dir: str,
    start_year: int = 2006,
    end_date_str: Optional[str] = None,
    workers: int = 8,
) -> None:
    """Download all missing bhavcopies from start_year to end_date."""
    bhav_root = os.path.join(out_dir, "bhav")
    os.makedirs(bhav_root, exist_ok=True)
    closed_path = os.path.join(bhav_root, "_closed.txt")

    closed_days: Set[str] = set()
    if os.path.exists(closed_path):
        with open(closed_path, "r", encoding="utf-8") as f:
            closed_days = set(f.read().split())

    today = dt.date.today()
    end_date = dt.date.fromisoformat(end_date_str) if end_date_str else today
    cur = dt.date(start_year, 1, 1)

    targets: List[dt.date] = []
    while cur <= end_date:
        d_str = cur.isoformat()
        year_str = str(cur.year)
        dest = os.path.join(bhav_root, year_str, f"{cur:%Y%m%d}.csv.zip")
        if d_str not in closed_days and not os.path.exists(dest):
            targets.append(cur)
        cur += dt.timedelta(days=1)

    logger.info(
        f"Download plan: {len(targets)} candidate dates to query "
        f"({start_year} -> {end_date.isoformat()}; already closed: {len(closed_days)})"
    )
    if not targets:
        logger.info("All dates up to date!")
        return

    failed_dates: List[Tuple[dt.date, int]] = []
    downloaded_count = 0
    closed_new_count = 0

    def download_one(d: dt.date) -> Tuple[dt.date, int]:
        year_dir = os.path.join(bhav_root, str(d.year))
        os.makedirs(year_dir, exist_ok=True)
        dest = os.path.join(year_dir, f"{d:%Y%m%d}.csv.zip")
        url = get_bhav_url(d)
        code = curl_download(url, dest)
        return d, code

    with ThreadPoolExecutor(max_workers=workers) as executor, open(
        closed_path, "a", encoding="utf-8"
    ) as cf:
        futures = {executor.submit(download_one, d): d for d in targets}
        total = len(targets)
        done = 0

        for fut in as_completed(futures):
            done += 1
            d, code = fut.result()
            if code == 200:
                downloaded_count += 1
            elif code == 404:
                # If older than 5 days, record as permanently closed/holiday
                if (today - d).days >= 5:
                    cf.write(d.isoformat() + "\n")
                    closed_new_count += 1
            else:
                failed_dates.append((d, code))

            if done % 100 == 0 or done == total:
                cf.flush()
                logger.info(
                    f"Progress: {done}/{total} ({(done/total)*100:.1f}%) | "
                    f"New downloaded: {downloaded_count} | New closed: {closed_new_count} | Failed: {len(failed_dates)}"
                )

    if failed_dates:
        logger.warning(f"{len(failed_dates)} dates failed to download (transient errors). Re-run to retry.")
    else:
        logger.info(f"Download complete! Successfully processed all {len(targets)} dates.")


# =====================================================================
# 2. PARSE AND ARCHIVE (PARQUET) COMMAND
# =====================================================================

def parse_bhavcopy_zip(zip_path: str) -> List[dict]:
    """Parse a single bhavcopy zip file (legacy or UDiFF) into standard records."""
    records = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            if not names:
                return records
            raw_text = z.read(names[0]).decode("latin-1")
    except Exception as e:
        logger.error(f"Error opening zip {zip_path}: {e}")
        return records

    reader = csv.reader(io.StringIO(raw_text))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return records

    # Detect legacy vs UDiFF
    if header and header[0] == "SYMBOL":
        # Legacy format: SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN
        col_idx = {h: i for i, h in enumerate(header)}
        for r in reader:
            if len(r) < 9:
                continue
            series = r[col_idx.get("SERIES", 1)].strip()
            if series not in EQUITY_SERIES:
                continue
            sym = r[col_idx["SYMBOL"]].strip()
            try:
                opn = float(r[col_idx["OPEN"]])
                hgh = float(r[col_idx["HIGH"]])
                low = float(r[col_idx["LOW"]])
                cls = float(r[col_idx["CLOSE"]])
                prev = float(r[col_idx["PREVCLOSE"]]) if "PREVCLOSE" in col_idx else cls
                vol = int(float(r[col_idx["TOTTRDQTY"]]))
                val = float(r[col_idx["TOTTRDVAL"]]) if "TOTTRDVAL" in col_idx else opn * vol
            except (ValueError, IndexError):
                continue

            if cls <= 0 or vol < 0:
                continue

            # Timestamp: e.g. "04-JAN-2016" or date string
            ts_str = r[col_idx["TIMESTAMP"]].strip() if "TIMESTAMP" in col_idx else ""
            isin = r[col_idx["ISIN"]].strip() if "ISIN" in col_idx and len(r) > col_idx["ISIN"] else ""
            trades = int(float(r[col_idx["TOTALTRADES"]])) if "TOTALTRADES" in col_idx and len(r) > col_idx["TOTALTRADES"] and r[col_idx["TOTALTRADES"]].strip() else 0

            records.append({
                "symbol": sym,
                "series": series,
                "open": opn,
                "high": hgh,
                "low": low,
                "close": cls,
                "prev_close": prev,
                "volume": vol,
                "turnover": val,
                "trades": trades,
                "isin": isin,
                "date_raw": ts_str,
            })
    else:
        # Modern UDiFF format
        col_idx = {h: i for i, h in enumerate(header)}
        seg_idx = col_idx.get("Sgmt")
        sym_idx = col_idx.get("TckrSymb")
        srs_idx = col_idx.get("SctySrs")
        opn_idx = col_idx.get("OpnPric")
        hgh_idx = col_idx.get("HghPric")
        low_idx = col_idx.get("LwPric")
        cls_idx = col_idx.get("ClsPric")
        prv_idx = col_idx.get("PrvsClsgPric")
        vol_idx = col_idx.get("TtlTradgVol")
        val_idx = col_idx.get("TtlTrdVal")
        dt_idx = col_idx.get("TradDt")
        isin_idx = col_idx.get("ISIN")
        txs_idx = col_idx.get("TtlNbOfTxsExctd")

        for r in reader:
            if seg_idx is not None and (len(r) <= seg_idx or r[seg_idx].strip() != "CM"):
                continue
            if srs_idx is not None and (len(r) <= srs_idx or r[srs_idx].strip() not in EQUITY_SERIES):
                continue
            if sym_idx is None or len(r) <= sym_idx:
                continue
            sym = r[sym_idx].strip()
            series = r[srs_idx].strip() if srs_idx is not None else "EQ"
            try:
                opn = float(r[opn_idx])
                hgh = float(r[hgh_idx])
                low = float(r[low_idx])
                cls = float(r[cls_idx])
                prev = float(r[prv_idx]) if prv_idx is not None and r[prv_idx].strip() else cls
                vol = int(float(r[vol_idx])) if vol_idx is not None and r[vol_idx].strip() else 0
                val = float(r[val_idx]) if val_idx is not None and r[val_idx].strip() else opn * vol
            except (ValueError, IndexError):
                continue

            if cls <= 0 or vol < 0:
                continue

            ts_str = r[dt_idx].strip() if dt_idx is not None else ""
            isin = r[isin_idx].strip() if isin_idx is not None and len(r) > isin_idx else ""
            trades = int(float(r[txs_idx])) if txs_idx is not None and len(r) > txs_idx and r[txs_idx].strip() else 0

            records.append({
                "symbol": sym,
                "series": series,
                "open": opn,
                "high": hgh,
                "low": low,
                "close": cls,
                "prev_close": prev,
                "volume": vol,
                "turnover": val,
                "trades": trades,
                "isin": isin,
                "date_raw": ts_str,
            })

    return records


def build_annual_parquet(
    bhav_root: str,
    parquet_root: str,
    years: Optional[List[int]] = None,
) -> None:
    """Parse all downloaded bhavcopy zips and build compressed annual Parquet files."""
    os.makedirs(parquet_root, exist_ok=True)
    available_years = sorted(
        int(d) for d in os.listdir(bhav_root) if d.isdigit() and os.path.isdir(os.path.join(bhav_root, d))
    )
    if years:
        available_years = [y for y in available_years if y in years]

    logger.info(f"Building annual Parquet archives for years: {available_years}")

    for yr in available_years:
        yr_dir = os.path.join(bhav_root, str(yr))
        zip_files = sorted(f for f in os.listdir(yr_dir) if f.endswith(".csv.zip"))
        if not zip_files:
            continue

        out_pq = os.path.join(parquet_root, f"bhavcopy_{yr}.parquet")
        logger.info(f"Processing year {yr}: {len(zip_files)} daily files -> {out_pq}")

        all_records = []
        for zf in zip_files:
            zpath = os.path.join(yr_dir, zf)
            # Date from filename: YYYYMMDD.csv.zip
            file_date_str = zf[:8]
            d_obj = dt.datetime.strptime(file_date_str, "%Y%m%d").date()
            recs = parse_bhavcopy_zip(zpath)
            for r in recs:
                r["date"] = d_obj.isoformat()
                r.pop("date_raw", None)
            all_records.extend(recs)

        if not all_records:
            logger.warning(f"No records parsed for year {yr}")
            continue

        df = pd.DataFrame(all_records)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values(by=["date", "symbol"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        df.to_parquet(out_pq, compression="zstd", index=False)
        logger.info(
            f"Year {yr} completed: {len(df):,} rows, {df['symbol'].nunique()} unique symbols, "
            f"file size: {os.path.getsize(out_pq) / 1024 / 1024:.2f} MB"
        )


# =====================================================================
# 3. MONGO INGESTION COMMAND
# =====================================================================

def ingest_to_mongo(
    parquet_root: str,
    mongo_uri: str = "mongodb://localhost:27017/",
    db_name: str = "stock_scanner",
    collection_name: str = "nse_bhavcopy_daily",
    batch_size: int = 25000,
    symbols: Optional[List[str]] = None,
) -> None:
    """Ingest annual Parquet data into MongoDB with compound index on (symbol, date)."""
    from pymongo import MongoClient, ReplaceOne
    from pymongo.errors import BulkWriteError

    client = MongoClient(mongo_uri)
    db = client[db_name]
    col = db[collection_name]

    is_empty = (col.estimated_document_count() == 0)
    if not is_empty:
        # Existing collection: ensure indexes first
        col.create_index([("symbol", 1), ("date", 1)], unique=True)
        col.create_index([("date", 1), ("turnover", -1)])
        col.create_index([("symbol", 1)])

    pq_files = sorted(Path(parquet_root).glob("bhavcopy_*.parquet"))
    logger.info(f"Found {len(pq_files)} annual Parquet files to ingest into {db_name}.{collection_name}")

    sym_filter = set(symbols) if symbols else None
    total_inserted = 0

    for pq_path in pq_files:
        df = pd.read_parquet(pq_path)
        if sym_filter:
            df = df[df["symbol"].isin(sym_filter)]
        if df.empty:
            continue

        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        records = df.to_dict("records")
        logger.info(f"Ingesting {pq_path.name}: {len(records):,} records...")

        if is_empty:
            # High-speed bulk insert into unindexed/fresh collection
            for i in range(0, len(records), batch_size):
                batch = records[i : i + batch_size]
                try:
                    col.insert_many(batch, ordered=False)
                    total_inserted += len(batch)
                except BulkWriteError as bwe:
                    inserted = bwe.details.get("nInserted", 0)
                    total_inserted += inserted
        else:
            # Idempotent upsert for updating existing collections
            ops = []
            for r in records:
                ops.append(
                    ReplaceOne(
                        {"symbol": r["symbol"], "date": r["date"]},
                        r,
                        upsert=True,
                    )
                )
                if len(ops) >= batch_size:
                    col.bulk_write(ops, ordered=False)
                    total_inserted += len(ops)
                    ops = []
            if ops:
                col.bulk_write(ops, ordered=False)
                total_inserted += len(ops)

        logger.info(f"Finished {pq_path.name}. Total ingested so far: {total_inserted:,}")

    if is_empty:
        logger.info("Creating MongoDB indexes on nse_bhavcopy_daily...")
        col.create_index([("symbol", 1), ("date", 1)], unique=True)
        col.create_index([("date", 1), ("turnover", -1)])
        col.create_index([("symbol", 1)])
        logger.info("Indexes created successfully.")

    logger.info(f"MongoDB ingestion complete! Total records in {collection_name}: {col.count_documents({}):,}")



# =====================================================================
# CLI PARSER
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Official NSE Bhavcopy Historical Data Pipeline")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. Download
    p_dl = subparsers.add_parser("download", help="Download raw daily bhavcopies from NSE archive")
    p_dl.add_argument("--out-dir", default="data/external/nse_archive", help="Destination root directory")
    p_dl.add_argument("--start-year", type=int, default=2006, help="First year to download (default: 2006)")
    p_dl.add_argument("--end-date", default=None, help="Last date to download (YYYY-MM-DD, default: today)")
    p_dl.add_argument("--workers", type=int, default=8, help="Concurrent worker threads (default: 8)")

    # 2. Archive
    p_arc = subparsers.add_parser("archive", help="Build annual Parquet archives from downloaded bhavcopies")
    p_arc.add_argument("--bhav-dir", default="data/external/nse_archive/bhav", help="Raw bhavcopy directory")
    p_arc.add_argument("--out-dir", default="data/external/nse_archive/parquet", help="Parquet output directory")
    p_arc.add_argument("--years", nargs="*", type=int, help="Specific years to process (optional)")

    # 3. Ingest
    p_ing = subparsers.add_parser("ingest", help="Ingest Parquet archives into MongoDB")
    p_ing.add_argument("--parquet-dir", default="data/external/nse_archive/parquet", help="Parquet directory")
    p_ing.add_argument("--mongo-uri", default="mongodb://localhost:27017/", help="MongoDB URI")
    p_ing.add_argument("--collection", default="nse_bhavcopy_daily", help="Target collection name")
    p_ing.add_argument("--batch-size", type=int, default=15000, help="Bulk write batch size")

    # 4. Status
    p_stat = subparsers.add_parser("status", help="Check download and archive status")
    p_stat.add_argument("--root-dir", default="data/external/nse_archive", help="Archive root directory")

    args = parser.parse_args()

    if args.subcommand == "download":
        run_download(
            out_dir=args.out_dir,
            start_year=args.start_year,
            end_date_str=args.end_date,
            workers=args.workers,
        )
    elif args.subcommand == "archive":
        build_annual_parquet(
            bhav_root=args.bhav_dir,
            parquet_root=args.out_dir,
            years=args.years,
        )
    elif args.subcommand == "ingest":
        ingest_to_mongo(
            parquet_root=args.parquet_dir,
            mongo_uri=args.mongo_uri,
            collection_name=args.collection,
            batch_size=args.batch_size,
        )
    elif args.subcommand == "status":
        bhav_root = os.path.join(args.root_dir, "bhav")
        pq_root = os.path.join(args.root_dir, "parquet")
        closed_file = os.path.join(bhav_root, "_closed.txt")
        closed_count = 0
        if os.path.exists(closed_file):
            with open(closed_file, "r") as f:
                closed_count = len(f.read().split())

        years = sorted(d for d in os.listdir(bhav_root) if d.isdigit()) if os.path.exists(bhav_root) else []
        print(f"Bhavcopy root: {bhav_root}")
        print(f"Recorded closed/holidays: {closed_count:,}")
        total_zips = 0
        for yr in years:
            zcnt = len(os.listdir(os.path.join(bhav_root, yr)))
            total_zips += zcnt
            pq_path = os.path.join(pq_root, f"bhavcopy_{yr}.parquet")
            pq_str = f"Parquet: {os.path.getsize(pq_path)/1024/1024:.1f} MB" if os.path.exists(pq_path) else "No Parquet"
            print(f"  Year {yr}: {zcnt:>3} files | {pq_str}")
        print(f"Total downloaded daily zips: {total_zips:,}")


if __name__ == "__main__":
    main()

