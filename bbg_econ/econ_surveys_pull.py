#!/usr/bin/env python3
"""
econ_surveys_pull.py  -  Bloomberg economic-release ACTUAL + CONSENSUS history.

Pulls, for each US macro release, the historical actual print and the Bloomberg
survey (consensus) fields — median / mean / std / high / low / #forecasts — so the
econ_surprises study (Hobbes) can compute surprise = actual - median and run the
seasonality-of-surprises the desk asked for.

RUN THIS ON A MACHINE WITH A LOGGED-IN BLOOMBERG TERMINAL (needs blpapi, :8194).
Then commit + push the Data repo (see refresh_and_push.ps1) so azhang can pull it.

    python econ_surveys_pull.py                    # 2000-01-01 -> today
    python econ_surveys_pull.py --start 2015-01-01
    python econ_surveys_pull.py --out econ_surveys.csv

On "whisper": Bloomberg has *consensus* (BN_SURVEY_*), not a distinct macro "whisper"
number (whisper is an equities-earnings concept). We pull consensus; a `whisper`
column is left in the output for a hand/other source if you have one.

Output (data contract consumed by Hobbes/econ_surprises/consensus.py):
    econ_surveys.csv  — long format, one row per (indicator, release date):
      indicator, ticker, date, actual, median, average, std, high, low, n_forecasts

  `date` is the Bloomberg history date for the release ticker. Whether that is the
  release date or the reference-period date VARIES by ticker — VERIFY one series on
  the terminal (GP/DES); the consumer maps it to the reference month.

TICKERS ARE BEST-EFFORT — verify each with `ECO <GO>` (pick the release -> its ticker)
or `<TICKER> DES <GO>`. Flip status to "ok" once confirmed. A wrong ticker just skips
that indicator with a warning; it won't sink the run.
"""
import argparse
import csv
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "econ_surveys.csv")
LOG = os.path.join(HERE, "econ_surveys.log")

# indicator name (matches Hobbes econ_surprises where sensible) -> ticker, status
INDICATORS = {
    # Employment
    "NFP":          dict(ticker="NFP TCH Index",  status="likely"),   # nonfarm payrolls net chg, SA
    "NFP_priv":     dict(ticker="NFP PCH Index",  status="VERIFY"),   # private payrolls net chg
    "ADP":          dict(ticker="ADP CHNG Index", status="likely"),   # ADP employment chg
    "UNRATE":       dict(ticker="USURTOT Index",  status="VERIFY"),   # unemployment rate
    # CPI
    "CPI_MoM":      dict(ticker="CPI CHNG Index", status="likely"),   # CPI MoM SA
    "CPI_YoY":      dict(ticker="CPI YOY Index",  status="likely"),   # CPI YoY NSA
    "CoreCPI_MoM":  dict(ticker="CPI XCHG Index", status="VERIFY"),   # core CPI MoM SA
    "CoreCPI_YoY":  dict(ticker="CPI XYOY Index", status="likely"),   # core CPI YoY
    # PPI (final demand)
    "PPI_MoM":      dict(ticker="FDIDFDMO Index", status="VERIFY"),   # PPI final demand MoM SA
    "PPI_YoY":      dict(ticker="FDIUFDYO Index", status="VERIFY"),   # PPI final demand YoY
    # PCE
    "PCE_MoM":      dict(ticker="PCE DEFM Index", status="VERIFY"),   # PCE deflator MoM
    "PCE_YoY":      dict(ticker="PCE DEFY Index", status="VERIFY"),   # PCE deflator YoY
    "CorePCE_MoM":  dict(ticker="PCE CMOM Index", status="VERIFY"),   # core PCE MoM SA
    "CorePCE_YoY":  dict(ticker="PCE CYOY Index", status="VERIFY"),   # core PCE YoY
}

# Bloomberg fields: actual + consensus. Names marked VERIFY if unsure.
FIELD_ACTUAL = "PX_LAST"                 # actual release; alt: "ACTUAL_RELEASE"
FIELD_MAP = {                            # output column -> BBG field
    "median":      "BN_SURVEY_MEDIAN",
    "average":     "BN_SURVEY_AVERAGE",
    "std":         "BN_SURVEY_STANDARD_DEVIATION",   # VERIFY
    "high":        "BN_SURVEY_HIGH",
    "low":         "BN_SURVEY_LOW",
    "n_forecasts": "BN_SURVEY_NUMBER_OF_FORECASTS",  # VERIFY
}
OUT_COLS = ["indicator", "ticker", "date", "actual",
            "median", "average", "std", "high", "low", "n_forecasts", "whisper"]


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _start_session():
    import blpapi
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    s = blpapi.Session(opts)
    if not s.start():
        raise RuntimeError("Failed to start Bloomberg session (is the Terminal running?)")
    if not s.openService("//blp/refdata"):
        raise RuntimeError("Failed to open //blp/refdata")
    return s, blpapi


def _history(session, blpapi, ticker, fields, start, end):
    """HistoricalDataRequest -> {date: {field: value}}."""
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("HistoricalDataRequest")
    req.append("securities", ticker)
    for f in fields:
        req.append("fields", f)
    req.set("startDate", start.replace("-", ""))
    req.set("endDate", end.replace("-", ""))
    req.set("periodicitySelection", "MONTHLY")
    session.sendRequest(req)

    rows: dict = {}
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
                key = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
                row = rows.setdefault(key, {})
                for f in fields:
                    if pt.hasElement(f):
                        try:
                            row[f] = pt.getElementAsFloat(f)
                        except Exception:  # noqa: BLE001
                            row[f] = pt.getElementAsString(f)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        session, blpapi = _start_session()
    except Exception as e:  # noqa: BLE001
        log(f"FATAL: {e}")
        return 1

    fields = [FIELD_ACTUAL, *FIELD_MAP.values()]
    all_rows, ok, bad = [], [], []
    for name, spec in INDICATORS.items():
        ticker = spec["ticker"]
        try:
            hist = _history(session, blpapi, ticker, fields, args.start, args.end)
            for date, vals in sorted(hist.items()):
                row = {"indicator": name, "ticker": ticker, "date": date,
                       "actual": vals.get(FIELD_ACTUAL), "whisper": ""}
                for col, fld in FIELD_MAP.items():
                    row[col] = vals.get(fld)
                all_rows.append(row)
            ok.append(name)
            log(f"{name:12s} {ticker:16s} ok  ({len(hist)} obs)")
        except Exception as e:  # noqa: BLE001
            bad.append(name)
            log(f"{name:12s} {ticker:16s} SKIPPED [{spec['status']}]: {str(e)[:70]}")

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(all_rows)
    log(f"wrote {args.out}  ({len(all_rows)} rows; ok={ok}; skipped={bad})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
