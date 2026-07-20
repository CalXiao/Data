#!/usr/bin/env python3
"""
fedfunds_futures_pull.py  -  30-day Fed Funds futures strip (implied policy path).

Pulls the generic fed funds futures curve daily. The implied average fed funds rate for
each contract month is 100 − price; the strip is the market's expected policy path, from
which the Hobbes rates_events toolkit backs out how many hikes/cuts were priced heading
into each FOMC (the precise version of the 2Y−target proxy it uses today).

RUN ON A MACHINE WITH A LOGGED-IN BLOOMBERG TERMINAL (blpapi, :8194). Then commit + push
(refresh_and_push.ps1 runs this alongside the other feeds).

    python fedfunds_futures_pull.py                    # 2000-01-01 -> today
    python fedfunds_futures_pull.py --start 2015-01-01 --n 16

TICKERS ARE BEST-EFFORT — the generic front contracts are "FF1 Comdty" ... "FFn Comdty"
(30-day fed funds, CBOT). VERIFY on the terminal (FF1 Comdty DES). For meeting-dated
probabilities the desk tool WIRP is the reference; this strip is the raw input.

Output — fedfunds_futures.csv:  date, contract, price, implied_rate
"""
import argparse
import csv
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "fedfunds_futures.csv")
LOG = os.path.join(HERE, "fedfunds_futures.log")


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _session():
    import blpapi
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost"); opts.setServerPort(8194)
    s = blpapi.Session(opts)
    if not s.start():
        raise RuntimeError("Failed to start Bloomberg session (Terminal running?)")
    if not s.openService("//blp/refdata"):
        raise RuntimeError("Failed to open //blp/refdata")
    return s, blpapi


def _history(session, blpapi, ticker, field, start, end):
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("HistoricalDataRequest")
    req.append("securities", ticker); req.append("fields", field)
    req.set("startDate", start.replace("-", "")); req.set("endDate", end.replace("-", ""))
    req.set("periodicitySelection", "DAILY")
    session.sendRequest(req)
    out = {}
    while True:
        ev = session.nextEvent(500)
        for msg in ev:
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            if sd.hasElement("securityError"):
                raise RuntimeError(str(sd.getElement("securityError")))
            fd = sd.getElement("fieldData")
            for i in range(fd.numValues()):
                pt = fd.getValue(i)
                d = pt.getElementAsDatetime("date")
                if pt.hasElement(field):
                    out[f"{d.year:04d}-{d.month:02d}-{d.day:02d}"] = pt.getElementAsFloat(field)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--n", type=int, default=16, help="number of generic contracts FF1..FFn")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        session, blpapi = _session()
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: {e}"); return 1

    rows, ok = [], 0
    for i in range(1, args.n + 1):
        ticker = f"FF{i} Comdty"
        try:
            hist = _history(session, blpapi, ticker, "PX_LAST", args.start, args.end)
            for date, px in sorted(hist.items()):
                rows.append({"date": date, "contract": f"FF{i}", "price": px,
                             "implied_rate": round(100.0 - px, 4)})
            ok += 1
            log(f"{ticker:12s} ok ({len(hist)} obs)")
        except Exception as e:  # noqa: BLE001
            log(f"{ticker:12s} SKIPPED: {str(e)[:60]}")

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "contract", "price", "implied_rate"])
        w.writeheader(); w.writerows(rows)
    log(f"wrote {args.out}  ({len(rows)} rows, {ok} contracts)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
