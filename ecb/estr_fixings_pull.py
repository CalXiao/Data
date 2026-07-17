#!/usr/bin/env python3
r"""
estr_fixings_pull.py  -  realized €STR fixings + the OFFICIAL ECB Compounded
€STR Index for seasoned EUR OIS (and the discounting leg of EUR IRS).

Source: ECB Data Portal API (data-api.ecb.europa.eu), dataset EST:
    rate : /service/data/EST/B.EU000A2X2A25.WT?format=csvdata&startPeriod=...
    index: /service/data/EST/B.EU000A2QQF16.CI?format=csvdata&startPeriod=...
UNRUN caveat (2026-07-07): the endpoint returned an empty body through the
sandbox fetcher (attachment-served, same class as BoE iadb) — verify on the
box; the Accept/UA headers below are the documented cure. The INDEX series key
is best-knowledge — if it 404s, the puller logs it and self-compounds ACT/360
(€STR convention) from the rate series; ratios match the official index.

Fallbacks: --bbg (ESTRON Index PX_LAST history) or --seed csv.
Output: estr_fixings.json {asof, fixings:[{date,rate,index}]} — served by
citivelocity_feed.py at /estr_fixings (start_pricer.bat wires the path).
"""
import argparse, csv as _csv, datetime as dt, json, os, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "estr_fixings.json")
API = "https://data-api.ecb.europa.eu/service/data/EST/{key}?format=csvdata&startPeriod={start}"
KEY_RATE, KEY_INDEX = "B.EU000A2X2A25.WT", "B.EU000A2QQF16.CI"


def _get(url, retries=3, timeout=45):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "estr-fixings-pull/1.0",
                                                       "Accept": "text/csv, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


def _parse_sdmx_csv(txt):
    """ECB csvdata: header row with TIME_PERIOD and OBS_VALUE columns."""
    out = {}
    if not txt:
        return out
    rows = list(_csv.reader(txt.splitlines()))
    if not rows:
        return out
    hdr = [h.strip().upper() for h in rows[0]]
    try:
        ti, vi = hdr.index("TIME_PERIOD"), hdr.index("OBS_VALUE")
    except ValueError:
        return out
    for r in rows[1:]:
        try:
            d = dt.date.fromisoformat(r[ti][:10]); out[d.isoformat()] = float(r[vi])
        except (ValueError, IndexError):
            continue
    return out


def load_existing(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            out = {}
            for x in json.load(f).get("fixings", []):
                rec = {}
                if x.get("rate") is not None: rec["rate"] = float(x["rate"])
                if x.get("index") is not None and not x.get("index_synthetic"): rec["index"] = float(x["index"])
                if rec: out[x["date"]] = rec
            return out
    except Exception as e:
        print(f"[estr] WARN reading {path}: {e}", file=sys.stderr); return {}


def pull_ecb(store, lookback_days):
    start = (dt.date.fromisoformat(max(store)) + dt.timedelta(days=1)) if store \
        else dt.date.today() - dt.timedelta(days=lookback_days)
    rates = _parse_sdmx_csv(_get(API.format(key=KEY_RATE, start=start.isoformat())))
    if not rates:
        raise RuntimeError("ECB rate series returned no rows — check headers/endpoint on the box")
    idx_txt = _get(API.format(key=KEY_INDEX, start=start.isoformat()))
    idxs = _parse_sdmx_csv(idx_txt)
    if idx_txt is None:
        print("[estr] WARN: index series key 404 — self-compounding (verify KEY_INDEX)", file=sys.stderr)
    for d, r in rates.items():
        store.setdefault(d, {})["rate"] = r
    for d, i in idxs.items():
        store.setdefault(d, {})["index"] = i
    print(f"[estr] ECB: +{len(rates)} rates, +{len(idxs)} official index values from {start}")
    return store


def pull_bbg(start, end, host="localhost", port=8194, ticker="ESTRON Index"):
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


def build_payload(store):
    ds = sorted(d for d in store if store[d].get("rate") is not None)
    if not ds:
        raise SystemExit("no ESTR fixings from any source — use --bbg or --seed for the first run")
    fix, idx = [], None
    for k, d in enumerate(ds):
        rec = store[d]; synth = False
        if rec.get("index") is not None:
            idx = rec["index"]
        elif idx is None:
            idx = 1.0; synth = True
        else:
            synth = True
        fix.append({"date": d, "rate": rec["rate"], "index": round(idx, 12), **({"index_synthetic": True} if synth else {})})
        if k + 1 < len(ds) and store[ds[k + 1]].get("index") is None:
            n = (dt.date.fromisoformat(ds[k + 1]) - dt.date.fromisoformat(d)).days
            idx = idx * (1.0 + rec["rate"] / 100.0 * n / 360.0)   # ESTR compounds ACT/360
    n_off = sum(1 for f in fix if not f.get("index_synthetic"))
    return {"asof": ds[-1], "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": f"ECB Data Portal EST (rate {KEY_RATE}; index {KEY_INDEX}: {n_off} official, rest self-compounded ACT/360)",
            "index_base_date": ds[0], "fixings": fix}


def main():
    p = argparse.ArgumentParser(description="ESTR fixings + Compounded ESTR Index (ECB Data Portal)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--days", type=int, default=3200, help="initial lookback (ESTR starts 2019-10-01)")
    p.add_argument("--seed", default=None, help="csv date,rate[,index] backfill")
    p.add_argument("--bbg", action="store_true", help="backfill rates via blpapi ESTRON Index")
    p.add_argument("--start", default="2019-10-01"); p.add_argument("--end", default=dt.date.today().isoformat())
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=8194)
    a = p.parse_args()
    store = load_existing(a.out)
    print(f"[estr] existing: {len(store)}" + (f" (last {max(store)})" if store else ""))
    if a.seed:
        for row in _csv.reader(open(a.seed, encoding="utf-8-sig")):
            try:
                rec = store.setdefault(dt.date.fromisoformat(row[0].strip()).isoformat(), {})
                rec["rate"] = float(row[1])
                if len(row) > 2 and row[2]: rec["index"] = float(row[2])
            except Exception:
                continue
    if a.bbg:
        for d, r in pull_bbg(a.start, a.end, a.host, a.port).items():
            store.setdefault(d, {})["rate"] = r
    try:
        store = pull_ecb(store, a.days)
    except Exception as e:
        print(f"[estr] WARN ECB pull failed ({e}); serving what we have", file=sys.stderr)
    payload = build_payload(store)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[estr] wrote {a.out}: {len(payload['fixings'])} fixings, asof {payload['asof']}")


if __name__ == "__main__":
    main()
