#!/usr/bin/env python3
r"""
aonia_fixings_pull.py  -  realized AONIA (RBA interbank overnight cash rate)
fixings + SYNTHETIC compounded index (ACT/365F) for seasoned AUD OIS.

RBA blocks automated fetching of rba.gov.au (confirmed 2026-07-07), so unlike
the other pullers the PRIMARY source here is Bloomberg: RBACOR Index PX_LAST
daily history via blpapi — the launcher runs this with --bbg-incremental so
each start_pricer.bat advances the store from the last saved date (terminal
must be up, which the bridge requires anyway). --seed csv also supported
(e.g. a manual download of RBA statistical table F1.1).

No official AONIA compounded index is consumed — the engine only uses index
RATIOS, so self-compounding is equivalent (same as TONA/CORRA).

Output: aonia_fixings.json {asof, fixings:[{date,rate,index}]} — served by
citivelocity_feed.py at /aonia_fixings.
"""
import argparse, csv as _csv, datetime as dt, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "aonia_fixings.json")


def load_existing(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return {x["date"]: float(x["rate"]) for x in json.load(f).get("fixings", []) if x.get("rate") is not None}
    except Exception as e:
        print(f"[aonia] WARN reading {path}: {e}", file=sys.stderr); return {}


def pull_bbg(start, end, host="localhost", port=8194, ticker="RBACOR Index"):
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
        raise SystemExit("no AONIA fixings — run with --bbg (terminal up) or --seed")
    fix, idx = [], 1.0
    for k, d in enumerate(ds):
        fix.append({"date": d, "rate": rates[d], "index": round(idx, 12)})
        if k + 1 < len(ds):
            n = (dt.date.fromisoformat(ds[k + 1]) - dt.date.fromisoformat(d)).days
            idx *= 1.0 + rates[d] / 100.0 * n / 365.0
    return {"asof": ds[-1], "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Bloomberg RBACOR Index (RBA cash rate / AONIA); index self-compounded ACT/365F (synthetic, ratios only)",
            "index_base_date": ds[0], "fixings": fix}


def main():
    p = argparse.ArgumentParser(description="AONIA fixings + synthetic compounded index (blpapi RBACOR)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--seed", default=None, help="csv date,rate backfill (e.g. RBA F1.1 export)")
    p.add_argument("--bbg", action="store_true", help="full backfill from --start")
    p.add_argument("--bbg-incremental", action="store_true", help="advance from last saved date (launcher mode)")
    p.add_argument("--start", default="2016-01-01"); p.add_argument("--end", default=dt.date.today().isoformat())
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=8194)
    a = p.parse_args()
    rates = load_existing(a.out)
    print(f"[aonia] existing: {len(rates)}" + (f" (last {max(rates)})" if rates else ""))
    if a.seed:
        for row in _csv.reader(open(a.seed, encoding="utf-8-sig")):
            try: rates[dt.date.fromisoformat(row[0].strip()).isoformat()] = float(row[1])
            except Exception: continue
    try:
        if a.bbg:
            rates.update(pull_bbg(a.start, a.end, a.host, a.port))
        elif a.bbg_incremental:
            start = (dt.date.fromisoformat(max(rates)) + dt.timedelta(days=1)).isoformat() if rates else a.start
            if start <= a.end:
                rates.update(pull_bbg(start, a.end, a.host, a.port))
    except Exception as e:
        print(f"[aonia] WARN blpapi pull failed ({e}); serving what we have", file=sys.stderr)
    payload = build_payload(rates)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[aonia] wrote {a.out}: {len(payload['fixings'])} fixings, asof {payload['asof']}")


if __name__ == "__main__":
    main()
