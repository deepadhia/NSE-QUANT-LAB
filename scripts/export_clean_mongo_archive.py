#!/usr/bin/env python3
"""
export_clean_mongo_archive.py -- Exports nse_clean_archive database to a compressed single-file dump.

Usage:
  python scripts/export_clean_mongo_archive.py [--out nse_clean_archive_20yr.dump.gz]
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TARGET_DB = "nse_clean_archive"
DEFAULT_OUT = "nse_clean_archive_20yr.dump.gz"


def main():
    parser = argparse.ArgumentParser(description="Export nse_clean_archive to compressed dump.")
    parser.add_argument("--db", default=TARGET_DB, help="Database to export")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output archive path")
    parser.add_argument("--host", default="localhost", help="Mongo host")
    parser.add_argument("--port", default="27017", help="Mongo port")
    args = parser.parse_args()
    
    mongodump_bin = shutil.which("mongodump")
    if not mongodump_bin:
        # Common windows install paths
        candidates = [
            r"C:\Program Files\MongoDB\Tools\100\bin\mongodump.exe",
            r"C:\Program Files\MongoDB\Server\7.0\bin\mongodump.exe",
            r"C:\Program Files\MongoDB\Server\6.0\bin\mongodump.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                mongodump_bin = c
                break
                
    if not mongodump_bin:
        print("[ERROR] mongodump not found in PATH or standard directories.")
        sys.exit(1)
        
    out_path = Path(args.out).resolve()
    print("=" * 70)
    print(f"EXPORTING DATABASE '{args.db}' TO {out_path.name}")
    print("=" * 70)
    print(f"Using binary: {mongodump_bin}")
    
    cmd = [
        mongodump_bin,
        f"--host={args.host}:{args.port}",
        f"--db={args.db}",
        "--gzip",
        f"--archive={str(out_path)}"
    ]
    
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"\n[SUCCESS] Export completed in {time.time() - t0:.1f}s!")
        print(f"Archive File: {out_path}")
        print(f"File Size: {size_mb:.1f} MB")
        print("\nAnyone can restore this database using:")
        print(f"  mongorestore --gzip --archive={out_path.name}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] mongodump failed: {e.stderr}")
        sys.exit(1)


if __name__ == "__main__":
    main()

