#!/usr/bin/env python3
r"""
sonia_fixings_pull.py  -  realized SONIA fixings + the OFFICIAL BoE SONIA
Compounded Index for seasoned/backdated GBP swaps.

Source: BoE Interactive Statistical Database (IADB) CSV endpoint:
    https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp?csv.x=yes
      &Datefrom=DD/Mon/YYYY&Dateto=DD/Mon/YYYY
      &SeriesCodes=IUDSOIA,IUDZOS2&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N
  IUDSOIA = SONIA rate (%) ; IUDZOS2 = SONIA Compounded Index (official).
Unlike JPY/CAD there is an OFFICIAL index — used directly when present; any
date missing an index value is bridged by self-compounding ACT/365F (ratios
match the official construction).

UNRUN caveat (2026-07-06): the iadb endpoint returned an empty body through the
sandbox fetcher — it serves CSV as an attachment and may need the UA header
below; verify on the box. Fallbacks: --bbg (SONIO/N Index history, index then
synthetic) or --seed csv.

Output: sonia_fixings.json {asof, fixings:[{date,rate,index}]} — served by
citivelocity_feed.py at /sonia_fixings (start_pricer.bat wires the path).
SONIA Compounded Index published from 2018-04-23; SONIA rate history to 1997.
"""
import argparse, datetime as dt, json, os, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "sonia_fixings.json")
IADB = ("https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp?csv.x=yes"
        "&Datefrom={d0}&Dateto={d1}&SeriesCodes=IUDSOIA,IUDZOS2&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONNUM = {m.lower(): i + 1 for i, m in enumerate(_MON)}


def _fmt(d): return f"{d.day:02d}/{_MON[d.month - 1]}/{d.year}"


def _get(url, retries=3, timeout=45):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sonia-fixings-pull)",
                                                       "Accept": "text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


def _parse_date(tok):
    tok = tok.strip().strip('"')
    for f in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(tok, f).date()
        except ValueError:
            continue
    m = tok.split()
    if len(m) == 3 and m[1].lower()[:3] in _MONNUM:
        try:
            return dt.date(int(m[2]), _MONNUM[m[1].lower()[:3]], int(m[0]))
        except ValueError:
            pass
    return None


def pull_iadb(store, lookback_days):
    start = (dt.date.fromisoformat(max(store)) + dt.timedelta(days=1)) if store \
        else dt.date.today() - dt.timedelta(days=lookback_days)
    txt = _get(IADB.format(d0=_fmt(start), d1=_fmt(dt.date.today())))
    got = 0
    for line in txt.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2:
            continue
        d = _parse_date(parts[0])
        if d is None:
            continue          # header / junk
        rec = store.setdefault(d.isoformat(), {})
        try:
            rec["rate"] = float(parts[1])
        except (ValueError, IndexError):
            pass
        try:
            if len(parts) > 2 and parts[2] != "":
                rec["index"] = float(parts[2])
        except ValueError:
            pass
        got += 1
    if got == 0:
        raise RuntimeError("iadb returned no parseable rows — check the URL/headers on the box "
                           "(see docstring), or use --bbg / --seed")
    print(f"[sonia] iadb: {got} rows from {start}")
    return store


def pull_bbg(start, end, host="localhost", port=8194, ticker="SONIO/N Index"):
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
        print(f"[sonia] WARN reading {path}: {e}", file=sys.stderr); return {}


def build_payload(store):
    ds = sorted(d for d in store if store[d].get("rate") is not None)
    if not ds:
        raise SystemExit("no SONIA fixings from any source")
    # official index where present; self-compound ACT/365F to bridge gaps
    fix, idx = [], None
    for k, d in enumerate(ds):
        rec = store[d]
        synth = False
        if rec.get("index") is not None:
            idx = rec["index"]
        elif idx is None:
            idx = 1.0; synth = True
        else:
            synth = True                      # carried forward synthetically below
        fix.append({"date": d, "rate": rec["rate"], "index": round(idx, 12), **({"index_synthetic": True} if synth else {})})
        if k + 1 < len(ds) and store[ds[k + 1]].get("index") is None:
            n = (dt.date.fromisoformat(ds[k + 1]) - dt.date.fromisoformat(d)).days
            idx = idx * (1.0 + rec["rate"] / 100.0 * n / 365.0)
    n_off = sum(1 for f in fix if not f.get("index_synthetic"))
    return {"asof": ds[-1], "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": f"BoE IADB IUDSOIA (rate) + IUDZOS2 (official SONIA Compounded Index; {n_off} official, rest self-compounded ACT/365F)",
            "index_base_date": ds[0], "fixings": fix}


def main():
    p = argparse.ArgumentParser(description="SONIA fixings + Compounded Index (BoE IADB)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--days", type=int, default=3800, help="initial lookback if store empty (default ~10y)")
    p.add_argument("--seed", default=None, help="csv date,rate[,index] backfill")
    p.add_argument("--bbg", action="store_true", help="backfill rates via blpapi SONIO/N Index")
    p.add_argument("--start", default="2016-01-01"); p.add_argument("--end", default=dt.date.today().isoformat())
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=8194)
    a = p.parse_args()
    store = load_existing(a.out)
    print(f"[sonia] existing: {len(store)}" + (f" (last {max(store)})" if store else ""))
    if a.seed:
        import csv as _csv
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
        store = pull_iadb(store, a.days)
    except Exception as e:
        print(f"[sonia] WARN iadb pull failed ({e}); serving what we have", file=sys.stderr)
    payload = build_payload(store)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"[sonia] wrote {a.out}: {len(payload['fixings'])} fixings, asof {payload['asof']}")


if __name__ == "__main__":
    main()
