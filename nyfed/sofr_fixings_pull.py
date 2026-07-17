#!/usr/bin/env python3
"""
sofr_fixings_pull.py  -  backfill realized SOFR fixings + SOFR Index from the
NY Fed (the benchmark's source of truth) so the pricer can value seasoned /
backdated SOFR swaps.

Two NY Fed datasets are merged by effective date:
  - secured/sofr     -> daily overnight SOFR (percentRate)
  - secured/sofrai   -> SOFR Averages & Index (index, average30/90/180day)

The SOFR Index is the clean object for valuing a vanilla swap's realized float
leg: the compounded SOFR factor over any window [a, b] is exactly
        Index_b / Index_a
and the annualized compounded rate is
        (Index_b / Index_a - 1) * 360 / (cal_days_between(a, b))
This matches the cleared USD SOFR OIS convention (daily compounding, ACT/360),
so no manual re-compounding of daily fixings is required for completed periods.

Output: sofr_fixings.json  (ascending by date)
  {
    "asof": "2026-06-24",            # latest published effective date
    "generated_at": "...Z",
    "source": "FRBNY markets API",
    "index_base_date": "2018-04-02", # SOFR Index epoch (value 1.00000000)
    "fixings": [ {date, rate, index, avg30, avg90, avg180}, ... ]
  }

Usage:
  python sofr_fixings_pull.py                 # last ~400 calendar days
  python sofr_fixings_pull.py --days 730       # last 2 years
  python sofr_fixings_pull.py --start 2024-01-01 --end 2026-06-24
  python sofr_fixings_pull.py --out somewhere\\sofr_fixings.json
"""
import argparse, datetime as dt, json, os, sys, time, urllib.request, urllib.error

BASE = "https://markets.newyorkfed.org/api/rates/secured"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "sofr_fixings.json")


def _get(url, retries=4, timeout=30):
    """GET JSON with simple exponential-backoff retry."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                        "User-Agent": "sofr-fixings-pull/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n  {last}")


def fetch_range(dataset, start, end):
    """dataset in {'sofr','sofrai'}; returns list of refRates dicts."""
    url = f"{BASE}/{dataset}/search.json?startDate={start}&endDate={end}"
    data = _get(url)
    return data.get("refRates", [])


def build(start, end):
    rate_rows = fetch_range("sofr", start, end)
    ai_rows = fetch_range("sofrai", start, end)

    by_date = {}
    for row in rate_rows:
        if row.get("type") != "SOFR":
            continue
        d = row["effectiveDate"]
        by_date.setdefault(d, {})["rate"] = row.get("percentRate")
    for row in ai_rows:
        if row.get("type") != "SOFRAI":
            continue
        d = row["effectiveDate"]
        rec = by_date.setdefault(d, {})
        rec["index"] = row.get("index")
        rec["avg30"] = row.get("average30day")
        rec["avg90"] = row.get("average90day")
        rec["avg180"] = row.get("average180day")

    fixings = []
    for d in sorted(by_date):
        rec = by_date[d]
        fixings.append({
            "date": d,
            "rate": rec.get("rate"),
            "index": rec.get("index"),
            "avg30": rec.get("avg30"),
            "avg90": rec.get("avg90"),
            "avg180": rec.get("avg180"),
        })
    return fixings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400,
                    help="calendar days back from today (default 400 ~ 1y + buffer)")
    ap.add_argument("--start", help="explicit YYYY-MM-DD (overrides --days)")
    ap.add_argument("--end", help="explicit YYYY-MM-DD (default today)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    end = args.end or dt.date.today().isoformat()
    if args.start:
        start = args.start
    else:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=args.days)).isoformat()

    print(f"[sofr-pull] {start} -> {end}", file=sys.stderr)
    fixings = build(start, end)
    if not fixings:
        print("[sofr-pull] ERROR: no rows returned", file=sys.stderr)
        sys.exit(1)

    asof = fixings[-1]["date"]
    out = {
        "asof": asof,
        "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "FRBNY markets API (secured/sofr + secured/sofrai)",
        "index_base_date": "2018-04-02",
        "fixings": fixings,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    n_rate = sum(1 for x in fixings if x["rate"] is not None)
    n_idx = sum(1 for x in fixings if x["index"] is not None)
    print(f"[sofr-pull] wrote {len(fixings)} rows ({n_rate} rates, {n_idx} index) "
          f"asof {asof} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
