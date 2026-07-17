#!/usr/bin/env python3
r"""
corra_fixings_pull.py  -  realized CORRA fixings + SYNTHETIC compounded index
for seasoned/backdated CAD swaps (mirrors boj\tona_fixings_pull.py).

Source: Bank of Canada Valet API (verified live 2026-07-06):
    https://www.bankofcanada.ca/valet/observations/group/corra/json?start_date=YYYY-MM-DD
CORRA (%) = series "AVG.INTWO". The BoC group carries no compounded-index
series, so this compounds daily fixings itself, ACT/365F — the engine only
consumes index RATIOS, so the base date is irrelevant (same as TONA).

Fallbacks: --bbg (CAONREPO Index PX_LAST history via blpapi) or --seed csv.
Output: corra_fixings.json  {asof, fixings:[{date,rate,index}]}  — served by
citivelocity_feed.py at /corra_fixings (start_pricer.bat wires the path).
CORRA history: BoC publishes from 1997 (methodology enhanced 2020-06-15).
"""
import argparse, datetime as dt, json, os, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "corra_fixings.json")
VALET = "https://www.bankofcanada.ca/valet/observations/group/corra/json?start_date={start}"
SERIES = "AVG.INTWO"


def _get_json(url, retries=3, timeout=30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "corra-fixings-pull/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


def load_existing(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return {x["date"]: float(x["rate"]) for x in json.load(f).get("fixings", []) if x.get("rate") is not None}
    except Exception as e:
        print(f"[corra] WARN reading {path}: {e}", file=sys.stderr); return {}


def pull_valet(rates, lookback_days):
    start = (dt.date.fromisoformat(max(rates)) + dt.timedelta(days=1)) if rates \
        else dt.date.today() - dt.timedelta(days=lookback_days)
    data = _get_json(VALET.format(start=start.isoformat()))
    got = 0
    for ob in data.get("observations", []):
        v = (ob.get(SERIES) or {}).get("v")
        if v is not None:
            rates[ob["d"]] = float(v); got += 1
    print(f"[corra] Valet: +{got} fixings from {start}")
    return rates


def pull_bbg(start, end, host="localhost", port=8194, ticker="CAONREPO Index"):
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
        req.set("startDate", start.replace("-", "")); req.set("endDate", end.replace("-", ""))
        req.set("periodicitySelection", "DAILY")
        session.sendRequest(req)
        deadline = time.time() + 120; done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData").getElement("fieldData")
                for i in range(arr.numValues()):
                    fd = arr.getValueAsElement(i)
                    d = fd.getElementAsDatetime("date")
                    out[f"{d.year:04d}-{d.month:02d}-{d.day:02d}"] = float(fd.getElementAsFloat("PX_LAST"))
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()
    if not out:
        raise RuntimeError(f"blpapi returned no history for {ticker}")
    return out


def build_payload(rates):
    ds = sorted(rates)
    if not ds:
        raise SystemExit("no CORRA fixings — Valet unreachable and no --bbg/--seed?")
    fix, idx = [], 1.0
    for k, d in enumerate(ds):
        fix.append({"date": d, "rate": rates[d], "index": round(idx, 12)})
        if k + 1 < len(ds):
            n = (dt.date.fromisoformat(ds[k + 1]) - dt.date.fromisoformat(d)).days
            idx *= 1.0 + rates[d] / 100.0 * n / 365.0
    return {"asof": ds[-1], "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "BoC Valet AVG.INTWO (CORRA %); index = self-compounded ACT/365F (synthetic, ratios only)",
            "index_base_date": ds[0], "fixings": fix}


def main():
    p = argparse.ArgumentParser(description="CORRA fixings + synthetic compounded index (BoC Valet)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--days", type=int, default=3800, help="initial lookback if store empty (default ~10y)")
    p.add_argument("--seed", default=None, help="csv date,rate backfill")
    p.add_argument("--bbg", action="store_true", help="backfill via blpapi CAONREPO Index")
    p.add_argument("--start", default="2016-01-01"); p.add_argument("--end", default=dt.date.today().isoformat())
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=8194)
    a = p.parse_args()
    rates = load_existing(a.out)
    print(f"[corra] existing: {len(rates)}" + (f" (last {max(rates)})" if rates else ""))
    if a.seed:
        import csv as _csv
        for row in _csv.reader(open(a.seed, encoding="utf-8-sig")):
            try: rates[dt.date.fromisoformat(row[0].strip()).isoformat()] = float(row[1])
            except Exception: continue
    if a.bbg:
        rates.update(pull_bbg(a.start, a.end, a.host, a.port))
    try:
        rates = pull_valet(rates, a.days)
    except Exception as e:
        print(f"[corra] WARN Valet pull failed ({e}); serving what we have", file=sys.stderr)
    payload = build_payload(rates)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[corra] wrote {a.out}: {len(payload['fixings'])} fixings, asof {payload['asof']}")


if __name__ == "__main__":
    main()
