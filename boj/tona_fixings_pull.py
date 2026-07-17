#!/usr/bin/env python3
r"""
tona_fixings_pull.py  -  maintain realized TONA fixings + a SYNTHETIC compounded
index so the pricer can value seasoned / backdated JPY TONA swaps (mirrors
nyfed\sofr_fixings_pull.py for SOFR).

Unlike the NY Fed, the BOJ publishes the *rate* only (the official TONA
Compounded Index is QUICK-licensed), so this script compounds daily finals
itself, ACT/365F:
        index[d_{k+1}] = index[d_k] * (1 + rate_k/100 * caldays(d_k, d_{k+1}) / 365)
starting at 1.0 on the earliest date held.  legsSeasoned() only ever uses index
RATIOS (Index_b / Index_a), so the base date is irrelevant and differences vs
the official index are sub-0.1bp.

Rate sources, merged by date (later sources win):
  1. the existing output JSON (incremental runs preserve history)
  2. --seed <csv>   one-time backfill from a BOJ Time-Series Data Search export
                    (any csv with rows date,rate; date yyyy-mm-dd or yyyy/mm/dd)
  3. --bbg          one-time deep backfill via blpapi: MUTKCALM Index PX_LAST
                    daily history from --start (default 2016-01-01). Run on the
                    box where blpapi + the terminal live.
  4. daily FINAL results XLSX from the BOJ mutan page (default, incremental):
       https://www.boj.or.jp/en/statistics/market/short/mutan/d_release/md/<yyyy>/md<yyyymmdd>.xlsx
     Final for business day D is published ~10:00 JST on D+1. Weekdays that 404
     are treated as Tokyo holidays and skipped. NOTE: this URL scheme starts
     2025-10; older history must come from --seed or --bbg.

Output: tona_fixings.json  (ascending by date; same shape the pricer reads for SOFR)
  { "asof": "2026-07-03", "generated_at": "...Z", "source": "...",
    "index_base_date": "<first date>", "fixings": [ {date, rate, index}, ... ] }

Served to the pricer by citivelocity_feed.py at /tona_fixings
(start_pricer.bat wires --tona-fixings to this file).

UNRUN in the sandbox (firewalled); verify on the box. If the XLSX layout parse
fails it logs the cell dump — adjust _rate_from_xlsx accordingly.
"""
import argparse, datetime as dt, io, json, os, re, sys, time, urllib.request, urllib.error, zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "tona_fixings.json")
DAILY_URL = "https://www.boj.or.jp/en/statistics/market/short/mutan/d_release/md/{yyyy}/md{yyyymmdd}.xlsx"
NEW_SCHEME_FROM = dt.date(2025, 10, 1)   # older files live on www3.boj.or.jp (different scheme) — use --seed/--bbg


def _get(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tona-fixings-pull/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None          # holiday / not yet published
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


# ----------------------------------------------------------------------------
# XLSX parsing (stdlib only — the daily file is a small single-sheet workbook)
# ----------------------------------------------------------------------------
def _xlsx_cells(blob):
    """Return list of rows; each row is a list of resolved cell strings."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    sheet = next((n for n in z.namelist() if re.match(r"xl/worksheets/sheet1\.xml$", n)), None) \
        or next(n for n in z.namelist() if n.startswith("xl/worksheets/"))
    root = ET.fromstring(z.read(sheet))
    rows = []
    for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        cells = []
        for c in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            v = c.find("m:v", ns)
            txt = v.text if v is not None else None
            if txt is not None and c.get("t") == "s":
                try:
                    txt = shared[int(txt)]
                except Exception:
                    pass
            cells.append(txt if txt is not None else "")
        rows.append(cells)
    return rows


def _rate_from_xlsx(blob):
    """Find the O/N average rate in the daily 'Uncollateralized Overnight Call
    Rate (Final)' workbook: the row whose label contains 'average', first numeric
    cell to its right. Returns float (percent) or raises with a cell dump."""
    rows = _xlsx_cells(blob)
    for cells in rows:
        for j, c in enumerate(cells):
            if isinstance(c, str) and re.search(r"average", c, re.I):
                for x in cells[j + 1:]:
                    try:
                        return float(str(x).replace(",", ""))
                    except (TypeError, ValueError):
                        continue
    dump = "\n".join(" | ".join(str(c)[:24] for c in r[:8]) for r in rows[:20])
    raise RuntimeError("could not locate 'average' rate in daily XLSX; first rows:\n" + dump)


# ----------------------------------------------------------------------------
# rate sources
# ----------------------------------------------------------------------------
def load_existing(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {x["date"]: float(x["rate"]) for x in data.get("fixings", []) if x.get("rate") is not None}
    except Exception as e:
        print(f"[tona] WARN: could not read existing {path}: {e}", file=sys.stderr)
        return {}


def load_seed_csv(path):
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            parts = [p.strip().strip('"') for p in line.replace("\t", ",").split(",")]
            if len(parts) < 2:
                continue
            m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", parts[0])
            if not m:
                continue
            try:
                rate = float(parts[1])
            except ValueError:
                continue
            out[f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"] = rate
    if not out:
        raise SystemExit(f"--seed {path}: no date,rate rows recognized")
    return out


def pull_bbg(start, end, host="localhost", port=8194, ticker="MUTKCALM Index"):
    """Deep backfill via blpapi HistoricalDataRequest (PX_LAST, daily)."""
    import blpapi
    opts = blpapi.SessionOptions(); opts.setServerHost(host); opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"blpapi session failed on {host}:{port}")
    out = {}
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("openService //blp/refdata failed")
        svc = session.getService("//blp/refdata")
        req = svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(ticker)
        req.getElement("fields").appendValue("PX_LAST")
        req.set("startDate", start.replace("-", ""))
        req.set("endDate", end.replace("-", ""))
        req.set("periodicitySelection", "DAILY")
        session.sendRequest(req)
        done = False
        deadline = time.time() + 120
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                fdArr = msg.getElement("securityData").getElement("fieldData")
                for i in range(fdArr.numValues()):
                    fd = fdArr.getValueAsElement(i)
                    d = fd.getElementAsDatetime("date")
                    out[f"{d.year:04d}-{d.month:02d}-{d.day:02d}"] = float(fd.getElementAsFloat("PX_LAST"))
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()
    if not out:
        raise RuntimeError(f"blpapi returned no history for {ticker}")
    return out


def pull_daily(rates, lookback_days):
    """Incremental: fetch daily FINAL XLSX for missing weekdays up to yesterday."""
    today = dt.date.today()
    if rates:
        start = dt.date.fromisoformat(max(rates)) + dt.timedelta(days=1)
    else:
        start = today - dt.timedelta(days=lookback_days)
    start = max(start, NEW_SCHEME_FROM)
    got, misses = 0, 0
    d = start
    while d < today:                      # final for D publishes D+1 ~10:00 JST
        if d.weekday() < 5:
            url = DAILY_URL.format(yyyy=f"{d.year:04d}", yyyymmdd=d.strftime("%Y%m%d"))
            try:
                blob = _get(url)
                if blob is None:
                    misses += 1           # Tokyo holiday or not yet published
                else:
                    rates[d.isoformat()] = _rate_from_xlsx(blob)
                    got += 1
            except Exception as e:
                print(f"[tona] WARN {d}: {e}", file=sys.stderr)
        d += dt.timedelta(days=1)
    print(f"[tona] daily XLSX: +{got} fixings ({misses} weekday 404s = holidays/unpublished) from {start}")
    return rates


# ----------------------------------------------------------------------------
# synthetic compounded index (ACT/365F, base 1.0 at earliest date)
# ----------------------------------------------------------------------------
def build_payload(rates):
    ds = sorted(rates)
    if not ds:
        raise SystemExit("no TONA fixings from any source — use --seed or --bbg for the first run")
    fix = []
    idx = 1.0
    for k, d in enumerate(ds):
        fix.append({"date": d, "rate": rates[d], "index": round(idx, 12)})
        if k + 1 < len(ds):
            n = (dt.date.fromisoformat(ds[k + 1]) - dt.date.fromisoformat(d)).days
            idx *= 1.0 + rates[d] / 100.0 * n / 365.0
    return {
        "asof": ds[-1],
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "BOJ uncollateralized O/N call rate (TONA) finals; index = self-compounded ACT/365F (synthetic, ratios only)",
        "index_base_date": ds[0],
        "fixings": fix,
    }


def main():
    p = argparse.ArgumentParser(description="TONA fixings + synthetic compounded index (BOJ)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--seed", default=None, help="csv of date,rate for one-time backfill (stat-search export)")
    p.add_argument("--bbg", action="store_true", help="deep backfill via blpapi MUTKCALM Index PX_LAST")
    p.add_argument("--start", default="2016-01-01", help="--bbg backfill start (default 2016-01-01)")
    p.add_argument("--end", default=dt.date.today().isoformat(), help="--bbg backfill end (default today)")
    p.add_argument("--days", type=int, default=400, help="incremental lookback if the store is empty (default 400)")
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=8194)
    args = p.parse_args()

    rates = load_existing(args.out)
    print(f"[tona] existing: {len(rates)} fixings" + (f" (last {max(rates)})" if rates else ""))
    if args.seed:
        seed = load_seed_csv(args.seed)
        rates.update(seed); print(f"[tona] seed csv: +{len(seed)}")
    if args.bbg:
        hist = pull_bbg(args.start, args.end, args.host, args.port)
        rates.update(hist); print(f"[tona] blpapi: +{len(hist)}")
    rates = pull_daily(rates, args.days)

    payload = build_payload(rates)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[tona] wrote {args.out}: {len(payload['fixings'])} fixings, asof {payload['asof']}")


if __name__ == "__main__":
    main()
