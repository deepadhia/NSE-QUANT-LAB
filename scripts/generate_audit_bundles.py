"""
generate_audit_bundles.py -- Package clean audit ZIP and TXT bundles for external review (Argo)
================================================================================================
Generates:
  1. nse_quant_lab_code_audit.zip      (Full source, scripts, tests, configs, docs - no data, no secrets)
  2. audit_bundle.txt                  (All repository code in a single searchable text file)
  3. src_and_scripts_only.txt          (Focused bundle of src/ and scripts/ for quick review)
  4. core_strategies_and_execution.txt (Targeted bundle of strategies, paper manager, data loader, orders)
"""

import os
import sys
import zipfile
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

ZIP_NAME = "nse_quant_lab_code_audit.zip"
TXT_FULL = "audit_bundle.txt"
TXT_SRC = "src_and_scripts_only.txt"
TXT_CORE = "core_strategies_and_execution.txt"

ALLOWED_DIRS = {"src", "scripts", "tests", "deploy", "docs", "research", ".agents"}
ALLOWED_EXTS = {".py", ".md", ".json", ".yaml", ".yml", ".ini", ".toml", ".txt", ".sh", ".service", ".csv"}
EXCLUDED_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "node_modules", "external",
    "data", ".gemini", "scratch", "venv", ".venv", "dist", "build"
}
EXCLUDED_FILES = {
    ".env", ".env.local", ".env.production", ZIP_NAME, TXT_FULL, TXT_SRC, TXT_CORE,
    "deploy.zip", "nse-quant-v8.zip"
}
BULK_PREFIXES = ("nav_", "trades_", "orders_", "navfull_", "execs_")


def collect_files():
    files_to_pack = []

    # 1. Root files
    for f in os.listdir(REPO_ROOT):
        fp = REPO_ROOT / f
        if fp.is_file():
            if f in EXCLUDED_FILES or f.startswith(".env."):
                continue
            ext = fp.suffix.lower()
            if ext in ALLOWED_EXTS or f in [".gitignore", ".env.example", "LICENSE"]:
                files_to_pack.append(fp)

    # 2. Subdirectories
    for top_dir in sorted(ALLOWED_DIRS):
        dir_path = REPO_ROOT / top_dir
        if not dir_path.exists():
            continue
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for f in files:
                fp = pathlib.Path(root) / f
                if f in EXCLUDED_FILES or f.startswith(".env.") or fp.suffix.lower() in [".parquet", ".pkl", ".gz", ".zip"]:
                    continue
                # Skip bulk per-run research outputs (nav, trades, orders) to keep bundle fast & concise
                if "results" in fp.parts and f.startswith(("nav_", "trades_", "orders_")):
                    continue
                ext = fp.suffix.lower()
                if ext in ALLOWED_EXTS or f in [".gitignore", ".env.example"]:
                    files_to_pack.append(fp)

    return sorted(files_to_pack)


def main():
    files_to_pack = collect_files()
    print(f">> Discovered {len(files_to_pack)} clean files across {ALLOWED_DIRS} and root.")

    zip_path = REPO_ROOT / ZIP_NAME
    txt_full_path = REPO_ROOT / TXT_FULL
    txt_src_path = REPO_ROOT / TXT_SRC
    txt_core_path = REPO_ROOT / TXT_CORE

    # 1. Build ZIP
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in files_to_pack:
            rel = fp.relative_to(REPO_ROOT)
            zf.write(fp, arcname=str(rel).replace("\\", "/"))

    # 2. Build audit_bundle.txt
    with open(txt_full_path, "w", encoding="utf-8") as out:
        out.write("=" * 80 + "\n")
        out.write("NSE-QUANT-LAB COMPREHENSIVE AUDIT BUNDLE\n")
        out.write("Generated for external independent review (Argo)\n")
        out.write(f"Total Files Included: {len(files_to_pack)}\n")
        out.write("=" * 80 + "\n\n")

        for fp in files_to_pack:
            rel = str(fp.relative_to(REPO_ROOT)).replace("\\", "/")
            out.write("=" * 80 + "\n")
            out.write(f"=== FILE: {rel} ===\n")
            out.write("=" * 80 + "\n")
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                out.write(content)
            except Exception as e:
                out.write(f"[Error reading file: {e}]")
            out.write("\n\n")

    # 3. Build src_and_scripts_only.txt
    src_scripts_files = [fp for fp in files_to_pack if str(fp.relative_to(REPO_ROOT)).startswith(("src", "scripts"))]
    with open(txt_src_path, "w", encoding="utf-8") as out:
        out.write("=" * 80 + "\n")
        out.write("NSE-QUANT-LAB: SRC & SCRIPTS CODE REVIEW BUNDLE\n")
        out.write(f"Total Files: {len(src_scripts_files)}\n")
        out.write("=" * 80 + "\n\n")

        for fp in src_scripts_files:
            rel = str(fp.relative_to(REPO_ROOT)).replace("\\", "/")
            out.write(f"=== FILE: {rel} ===\n")
            out.write(fp.read_text(encoding="utf-8", errors="replace"))
            out.write("\n\n")

    # 4. Build core_strategies_and_execution.txt
    core_prefixes = (
        "src/nse_quant_lab/strategies/",
        "src/nse_quant_lab/paper/",
        "src/nse_quant_lab/data_loader.py",
        "src/nse_quant_lab/upstox_feed.py",
        "src/nse_quant_lab/config.py",
        "src/nse_quant_lab/cost_model.py",
        "src/nse_quant_lab/registry.py",
        "scripts/run_daily_pipeline.py",
        "scripts/execute_paper_orders.py",
        "scripts/ingest_daily_upstox_candles.py",
        "scripts/seed_upstox_history.py",
        "scripts/verify_production_health.py",
        "tests/test_minervini.py",
        "tests/test_trend_momentum_pit.py",
        "tests/test_alpha_semivol_pit.py",
        "tests/test_backtest_paper_parity.py",
        "tests/test_multi_strategy_incubator.py",
        "tests/test_data_loader.py",
        "docs/STRATEGY_ONBOARDING_AND_PARITY_PLAYBOOK.md",
    )
    core_files = [
        fp for fp in files_to_pack
        if str(fp.relative_to(REPO_ROOT)).replace("\\", "/").startswith(core_prefixes)
    ]
    with open(txt_core_path, "w", encoding="utf-8") as out:
        out.write("=" * 80 + "\n")
        out.write("NSE-QUANT-LAB: CORE STRATEGIES, EXECUTION & REALISTIC FILLS BUNDLE\n")
        out.write(f"Total Files: {len(core_files)}\n")
        out.write("=" * 80 + "\n\n")

        for fp in core_files:
            rel = str(fp.relative_to(REPO_ROOT)).replace("\\", "/")
            out.write(f"=== FILE: {rel} ===\n")
            out.write(fp.read_text(encoding="utf-8", errors="replace"))
            out.write("\n\n")

    # Verification: check for real secrets (never allow actual secret assignments)
    env_file = REPO_ROOT / ".env"
    sensitive_keys = {"UPSTOX_API_SECRET", "UPSTOX_REFRESH_TOKEN", "UPSTOX_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN"}
    real_secrets = []
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in sensitive_keys and len(v) > 8:
                    real_secrets.append(v)
                if k == "MONGO_URI" and "@" in v:
                    # extract password part if present
                    try:
                        creds = v.split("@")[0].split("//")[1]
                        if ":" in creds:
                            pwd = creds.split(":", 1)[1]
                            if len(pwd) > 4:
                                real_secrets.append(pwd)
                    except Exception:
                        pass

    for path in [txt_full_path, txt_src_path, txt_core_path]:
        text = path.read_text(encoding="utf-8")
        for s in real_secrets:
            assert s not in text, f"Security violation: Real secret leaked in {path.name}!"

    print("\n" + "=" * 70)
    print("ALL AUDIT BUNDLES GENERATED AND VERIFIED SUCCESSFULLY")
    print("=" * 70)
    print(f"1. ZIP Archive:     {ZIP_NAME} ({zip_path.stat().st_size:,} bytes | {len(files_to_pack)} files)")
    print(f"2. Full Text:       {TXT_FULL} ({txt_full_path.stat().st_size:,} bytes)")
    print(f"3. Src & Scripts:   {TXT_SRC} ({txt_src_path.stat().st_size:,} bytes | {len(src_scripts_files)} files)")
    print(f"4. Core Strategies: {TXT_CORE} ({txt_core_path.stat().st_size:,} bytes | {len(core_files)} files)")
    print("=" * 70)


if __name__ == "__main__":
    main()
