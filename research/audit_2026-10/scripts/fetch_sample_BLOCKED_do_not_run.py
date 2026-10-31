"""Fetch 5 sample NSE bhavcopy days (1 req/sec) into an isolated directory and record a manifest."""
import os, sys, time, json, hashlib, zipfile, io
import urllib.request, http.cookiejar
from datetime import datetime, timezone

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ARCH = "https://archives.nseindia.com"
DAYS = [  # (label, url)
    ("2005-01-03_legacy", f"{ARCH}/content/historical/EQUITIES/2005/JAN/cm03JAN2005bhav.csv.zip"),
    ("2012-06-01_legacy", f"{ARCH}/content/historical/EQUITIES/2012/JUN/cm01JUN2012bhav.csv.zip"),
    ("2019-04-16_legacy", f"{ARCH}/content/historical/EQUITIES/2019/APR/cm16APR2019bhav.csv.zip"),
    ("2024-07-05_legacy", f"{ARCH}/content/historical/EQUITIES/2024/JUL/cm05JUL2024bhav.csv.zip"),
    ("2026-10-23_udiff", f"{ARCH}/content/cm/BhavCopy_NSE_CM_0_0_0_20261023_F_0000.csv.zip"),
]

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}

def get(url):
    req = urllib.request.Request(url, headers=hdr)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}", b""

st, _ = get("https://www.nseindia.com/")
print("homepage warm-up:", st, "cookies:", len(jar))
manifest = []
for label, url in DAYS:
    time.sleep(1.0)
    st, body = get(url)
    rec = {"label": label, "url": url, "status": st, "bytes": len(body),
           "fetched_utc": datetime.now(timezone.utc).isoformat()}
    if st == 200 and body:
        try:
            zipfile.ZipFile(io.BytesIO(body)).testzip()
            path = os.path.join(OUT, label + ".zip")
            with open(path, "wb") as f:
                f.write(body)
            rec["sha256"] = hashlib.sha256(body).hexdigest()
        except zipfile.BadZipFile:
            rec["status"] = "NOT_A_ZIP"
    print(json.dumps(rec))
    manifest.append(rec)
with open(os.path.join(OUT, "_manifest.json"), "w") as f:
    json.dump(manifest, f, indent=2)
