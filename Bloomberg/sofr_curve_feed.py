#!/usr/bin/env python3
"""
sofr_curve_feed.py
==================
Pulls the live USD SOFR OIS curve from Bloomberg via the Python Desktop API
(`blpapi`) and exposes it to the Swap Pricer artifact.

It reproduces the EXACT ticker grid from the RateHelpers_SOFR tab of
Swaption Pricer.xlsm:
    overnight :  SOFRRATE Index            -> LAST_PRICE / 100
    swaps     :  USOSFR<tok> Curncy        -> MID        / 100

Two modes
---------
1. Snapshot (default): one ReferenceDataRequest, write JSON to --out and print it.
       python sofr_curve_feed.py --out sofr_curve.json

2. Server: serves BOTH the pricer UI and the data feed from one origin (so the
   in-app "LOAD BBG" button works with no CORS / mixed-content issues), cached and
   periodically refreshed so you don't hammer the terminal.
       python sofr_curve_feed.py --serve --http-port 8196 --interval 60
   Then open  http://localhost:8196/  and click "LOAD BBG".
   (Requires SwapPricer.html next to this script, or pass --ui <path>.)
   The raw JSON is also at  http://localhost:8196/curve .

Requirements
------------
- A logged-in Bloomberg Terminal on this machine (Desktop API / DAPI on :8194),
  or a B-PIPE/SAPI session (pass --host/--port accordingly).
- blpapi:  pip install --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi

JSON payload shape (consumed by the artifact)
---------------------------------------------
{
  "asof": "2026-06-23T14:32:05Z",
  "source": "USD SOFR OIS via Bloomberg blpapi (USOSFR.. Curncy MID, SOFRRATE Index)",
  "quotes": [
     {"tenor": "1D", "rate": 4.31,  "kind": "depo", "ticker": "SOFRRATE Index"},
     {"tenor": "1M", "rate": 4.569, "kind": "swap", "ticker": "USOSFRA Curncy"},
     ...
  ],
  "missing": ["50Y", ...]
}
"""

import argparse
import json
import os
import sys
import time
import threading
import webbrowser
import re
import urllib.request
import urllib.parse
import calendar
import math
from datetime import datetime, timezone, date as _date, timedelta as _timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# Curve grid + ticker construction  (identical to RateHelpers_SOFR, col N tokens)
# ----------------------------------------------------------------------------
# (tenor, kind, bbg_token)   token feeds  "USOSFR<token> Curncy"  for swaps.
GRID = [
    ("1D",  "depo", None),   # SOFRRATE Index
    ("1M",  "swap", "A"),
    ("2M",  "swap", "B"),
    ("3M",  "swap", "C"),
    ("6M",  "swap", "F"),
    ("9M",  "swap", "I"),
    ("1Y",  "swap", "1"),
    ("15M", "swap", "1C"),
    ("18M", "swap", "1F"),
    ("21M", "swap", "1I"),
    ("2Y",  "swap", "2"),
    ("27M", "swap", "2C"),
    ("30M", "swap", "2F"),
    ("3Y",  "swap", "3"),
    ("4Y",  "swap", "4"),
    ("5Y",  "swap", "5"),
    ("6Y",  "swap", "6"),
    ("7Y",  "swap", "7"),
    ("8Y",  "swap", "8"),
    ("9Y",  "swap", "9"),
    ("10Y", "swap", "10"),
    ("12Y", "swap", "12"),
    ("15Y", "swap", "15"),
    ("20Y", "swap", "20"),
    ("25Y", "swap", "25"),
    ("30Y", "swap", "30"),
    ("35Y", "swap", "35"),
    ("40Y", "swap", "40"),
    ("50Y", "swap", "50"),
]

ON_TICKER = "SOFRRATE Index"
ON_FIELD = "LAST_PRICE"
SWAP_FIELD = "MID"

# ----------------------------------------------------------------------------
# JPY TONA OIS grid  (JYSO<token> Curncy; same token grammar as USOSFR).
# UNRUN — verify tickers on the box with --print-tickers / a terminal.
# No 15M/21M/27M/30M/50Y points (thin/absent for JPY); matches the UI's JPY grid.
# O/N anchor: BOJ uncollateralized overnight call rate (TONA) = MUTKCALM Index.
# ----------------------------------------------------------------------------
GRID_JPY = [
    ("1D",  "depo", None),   # MUTKCALM Index
    ("1M",  "swap", "A"),
    ("2M",  "swap", "B"),
    ("3M",  "swap", "C"),
    ("6M",  "swap", "F"),
    ("9M",  "swap", "I"),
    ("1Y",  "swap", "1"),
    ("18M", "swap", "1F"),
    ("2Y",  "swap", "2"),
    ("3Y",  "swap", "3"),
    ("4Y",  "swap", "4"),
    ("5Y",  "swap", "5"),
    ("6Y",  "swap", "6"),
    ("7Y",  "swap", "7"),
    ("8Y",  "swap", "8"),
    ("9Y",  "swap", "9"),
    ("10Y", "swap", "10"),
    ("12Y", "swap", "12"),
    ("15Y", "swap", "15"),
    ("20Y", "swap", "20"),
    ("25Y", "swap", "25"),
    ("30Y", "swap", "30"),
    ("35Y", "swap", "35"),
    ("40Y", "swap", "40"),
]

# GBP SONIA OIS grid (BPSWS<tok> Curncy) — like JPY plus 50Y (SONIA liquid to 50y).
# CAD CORRA OIS grid (CDSO<tok> Curncy) — mirrors JPY (to 40Y).
# O/N anchors: SONIA fixing = SONIO/N Index; CORRA fixing = CAONREPO Index.
# UNRUN — verify tokens with --print-tickers / DES on the box (2026-07-06).
GRID_GBP = GRID_JPY + [("50Y", "swap", "50")]
GRID_CAD = list(GRID_JPY)

# ----------------------------------------------------------------------------
# EUR / AUD dual-curve grids (2026-07-07). Each ccy has TWO curves:
#   OIS (discounting): EESWE<tok> / ADSO<tok>, O/N anchors ESTRON / RBACOR.
#   IRS (projection):  EUSA<tok> (vs 6M EURIBOR) / AUD split roots — ADSW<n>Q
#     quarterly vs 3M BBSW for 1-3Y, ADSWAP<n> semi vs 6M BBSW from 4Y (SWPM
#     confirmed 2026-07-07). IRS front anchors = IBOR fixings (kind "fix").
# Rows of length 4 are LITERAL (tenor, kind, ticker, field) — no token grammar.
# UNRUN — verify with --print-tickers / DES on the box.
# ----------------------------------------------------------------------------
GRID_EUR_OIS = GRID_JPY + [("50Y", "swap", "50")]
GRID_AUD_OIS = list(GRID_JPY)
GRID_EUR_IRS = [("6M", "fix", "EUR006M Index", "LAST_PRICE"),
                ("3M", "fix", "EUR003M Index", "LAST_PRICE"),
                # 1Y trades vs 3M EURIBOR (market convention; matches Citi's 3s
                # 1Y mark + the engine's <1.5y rule). EUSW<n>V3 = vs-3M family;
                # EUSA1 (vs 6M) was WRONG here — showed the 1y 3s6s basis as Δ.
                # UNRUN: verify EUSW1V3 resolves on DES (2026-07-07).
                ("1Y", "swap", "EUSW1V3 Curncy", "MID")] + \
    [(t, "swap", tok) for t, tok in [("2Y", "2"), ("3Y", "3"), ("4Y", "4"), ("5Y", "5"),
                                     ("6Y", "6"), ("7Y", "7"), ("8Y", "8"), ("9Y", "9"), ("10Y", "10"),
                                     ("12Y", "12"), ("15Y", "15"), ("20Y", "20"), ("25Y", "25"),
                                     ("30Y", "30"), ("35Y", "35"), ("40Y", "40"), ("50Y", "50")]]
# EUR vs-3M strip (EUSW<n>V3, confirmed EUSW5V3 resolves 2026-07-07): builds the
# TRUE 3M projection curve — Citi's 1y-tail fwds are 3s, so the overlay compares
# 3s-to-3s and the 6M RV chain gets basis-consistent adjusted marks (see engine).
GRID_EUR_IRS3 = [("3M", "fix", "EUR003M Index", "LAST_PRICE")] + \
    [(t, "swap", f"EUSW{n}V3 Curncy", "MID") for t, n in
     [("1Y", 1), ("2Y", 2), ("3Y", 3), ("4Y", 4), ("5Y", 5), ("7Y", 7), ("10Y", 10),
      ("15Y", 15), ("20Y", 20), ("30Y", 30)]]

GRID_AUD_IRS = [("3M", "fix", "BBSW3M Index", "LAST_PRICE"), ("6M", "fix", "BBSW6M Index", "LAST_PRICE"),
                ("1Y", "swap", "ADSW1Q Curncy", "MID"), ("2Y", "swap", "ADSW2Q Curncy", "MID"),
                ("3Y", "swap", "ADSW3Q Curncy", "MID")] + \
    [(t, "swap", tok) for t, tok in [("4Y", "4"), ("5Y", "5"), ("6Y", "6"), ("7Y", "7"), ("8Y", "8"),
                                     ("9Y", "9"), ("10Y", "10"), ("12Y", "12"), ("15Y", "15"),
                                     ("20Y", "20"), ("25Y", "25"), ("30Y", "30"), ("40Y", "40")]]

# per-ccy curve config; /curve?ccy=JPY|GBP|CAD selects, default USD (backward compatible)
CCY_CONF = {
    "USD": {"grid": GRID,     "swap_fmt": "USOSFR{} Curncy", "on": (ON_TICKER, ON_FIELD),
            "source": "USD SOFR OIS via Bloomberg blpapi (USOSFR.. Curncy MID, SOFRRATE Index)"},
    "JPY": {"grid": GRID_JPY, "swap_fmt": "JYSO{} Curncy",   "on": ("MUTKCALM Index", "LAST_PRICE"),
            "source": "JPY TONA OIS via Bloomberg blpapi (JYSO.. Curncy MID, MUTKCALM Index)"},
    "GBP": {"grid": GRID_GBP, "swap_fmt": "BPSWS{} Curncy",  "on": ("SONIO/N Index", "LAST_PRICE"),
            "source": "GBP SONIA OIS via Bloomberg blpapi (BPSWS.. Curncy MID, SONIO/N Index)"},
    "CAD": {"grid": GRID_CAD, "swap_fmt": "CDSO{} Curncy",   "on": ("CAONREPO Index", "LAST_PRICE"),
            "source": "CAD CORRA OIS via Bloomberg blpapi (CDSO.. Curncy MID, CAONREPO Index)"},
    # EUR/AUD dual-curve: ?ccy=EUR -> OIS discount curve; ?ccy=EUR_IRS -> EURIBOR projection curve.
    "EUR":     {"grid": GRID_EUR_OIS, "swap_fmt": "EESWE{} Curncy", "on": ("ESTRON Index", "LAST_PRICE"),
                "source": "EUR ESTR OIS via Bloomberg blpapi (EESWE.. Curncy MID, ESTRON Index)"},
    "EUR_IRS": {"grid": GRID_EUR_IRS, "swap_fmt": "EUSA{} Curncy",  "on": None,
                "source": "EUR IRS vs 6M EURIBOR via Bloomberg blpapi (EUSA.. Curncy MID, EUR006M Index)"},
    "EUR_IRS3": {"grid": GRID_EUR_IRS3, "swap_fmt": "EUSW{}V3 Curncy", "on": None,
                 "source": "EUR IRS vs 3M EURIBOR via Bloomberg blpapi (EUSW..V3 Curncy MID, EUR003M Index)"},
    "AUD":     {"grid": GRID_AUD_OIS, "swap_fmt": "ADSO{} Curncy",  "on": ("RBACOR Index", "LAST_PRICE"),
                "source": "AUD AONIA OIS via Bloomberg blpapi (ADSO.. Curncy MID, RBACOR Index)"},
    "AUD_IRS": {"grid": GRID_AUD_IRS, "swap_fmt": "ADSWAP{} Curncy", "on": None,
                "source": "AUD IRS vs BBSW via Bloomberg blpapi (ADSW..Q 1-3Y, ADSWAP.. 4Y+, BBSW3M/6M Index)"},
}

FX_TICKERS = {"USDJPY": "USDJPY Curncy", "GBPUSD": "GBPUSD Curncy", "USDCAD": "USDCAD Curncy",
              "EURUSD": "EURUSD Curncy", "AUDUSD": "AUDUSD Curncy"}   # /fx (PX_LAST, MID fallback)

# ----------------------------------------------------------------------------
# ICAP swaption normal-vol surface (merged in from icap_vol_feed.py so the BBG
# bridge serves BOTH /curve and /surface; the unified LOAD pulls them together).
#   USSNA<exp><tail> ICPL Curncy -> PX_LAST  (RatesMon.xlsm M23:V40, 12Y dropped)
# ----------------------------------------------------------------------------
SURF_EXP = ["1M","2M","3M","6M","9M","1Y","18M","2Y","3Y","4Y","5Y","7Y","10Y","15Y","20Y","30Y"]
SURF_TEN = ["1Y","2Y","3Y","5Y","7Y","10Y","15Y","20Y","30Y"]
EXP_TOKEN = {"1M":"A","2M":"B","3M":"C","6M":"F","9M":"I","1Y":"1","18M":"1F","2Y":"2","3Y":"3","4Y":"4","5Y":"5","7Y":"7","10Y":"10","15Y":"15","20Y":"20","30Y":"30"}
TAIL_NUM = {"1Y":"1","2Y":"2","3Y":"3","5Y":"5","7Y":"7","10Y":"10","15Y":"15","20Y":"20","30Y":"30"}
LONG_LETTER = {"10Y":"J","15Y":"O","20Y":"T","30Y":"Z"}
SURF_SUFFIX = " ICPL Curncy"
SURF_FIELD = "PX_LAST"


def icap_ticker(exp, tail):
    long_tail = tail in LONG_LETTER
    if exp == "18M":
        body = "1F" + (LONG_LETTER[tail] if long_tail else TAIL_NUM[tail])
    elif exp in ("10Y", "15Y", "20Y", "30Y"):
        body = (LONG_LETTER[exp] if long_tail else EXP_TOKEN[exp]) + TAIL_NUM[tail]
    else:
        body = EXP_TOKEN[exp] + TAIL_NUM[tail]
    return f"USSNA{body}{SURF_SUFFIX}"


def build_surface_map():
    fwd = {}
    for e in SURF_EXP:
        for t in SURF_TEN:
            fwd[f"{e}|{t}"] = icap_ticker(e, t)
    return fwd


def swap_ticker(token: str, ccy: str = "USD") -> str:
    return CCY_CONF[ccy]["swap_fmt"].format(token)


def build_ticker_map(ccy: str = "USD"):
    """Return ordered list of (tenor, kind, ticker, field) for the given curve key.
    Grid rows of length 4 carry a LITERAL ticker+field (mixed-root grids like AUD IRS)."""
    conf = CCY_CONF[ccy]
    out = []
    for row in conf["grid"]:
        if len(row) == 4:
            tenor, kind, ticker, field = row
            out.append((tenor, kind, ticker, field))
        else:
            tenor, kind, tok = row
            if kind == "depo":
                out.append((tenor, kind, conf["on"][0], conf["on"][1]))
            else:
                out.append((tenor, kind, swap_ticker(tok, ccy), SWAP_FIELD))
    return out


# ----------------------------------------------------------------------------
# Bloomberg pull
# ----------------------------------------------------------------------------
def fetch_curve(host="localhost", port=8194, timeout_s=30, ccy="USD"):
    """Return (quotes_list, missing_list). Raises on session failure."""
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit(
            "blpapi is not installed. Install with:\n"
            "  pip install --index-url "
            "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi\n"
            f"(import error: {e})"
        )

    rows = build_ticker_map(ccy)
    # group securities by field so each request asks one field
    by_field = {}
    for tenor, kind, ticker, field in rows:
        by_field.setdefault(field, set()).add(ticker)

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} "
                           "(is the Terminal running / DAPI enabled?)")
    values = {}  # (ticker, field) -> float
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        refdata = session.getService("//blp/refdata")
        for field, secs in by_field.items():
            request = refdata.createRequest("ReferenceDataRequest")
            for s in sorted(secs):
                request.getElement("securities").appendValue(s)
            request.getElement("fields").appendValue(field)
            session.sendRequest(request)
            deadline = time.time() + timeout_s
            done = False
            while not done and time.time() < deadline:
                ev = session.nextEvent(500)
                for msg in ev:
                    if not msg.hasElement("securityData"):
                        continue
                    arr = msg.getElement("securityData")
                    for i in range(arr.numValues()):
                        sd = arr.getValueAsElement(i)
                        tk = sd.getElementAsString("security")
                        if sd.hasElement("securityError"):
                            continue
                        fd = sd.getElement("fieldData")
                        if fd.hasElement(field):
                            try:
                                values[(tk, field)] = float(fd.getElementAsFloat(field))
                            except Exception:
                                pass
                if ev.eventType() == blpapi.Event.RESPONSE:
                    done = True
    finally:
        session.stop()

    quotes, missing = [], []
    for tenor, kind, ticker, field in rows:
        raw = values.get((ticker, field))  # Bloomberg returns the quote in percent
        if raw is None:
            missing.append(tenor)
        quotes.append({
            "tenor": tenor, "kind": kind, "ticker": ticker, "field": field,
            "rate": raw,   # percent, e.g. 4.569 ; the pricer grid is also in percent
        })
    return quotes, missing


def make_payload(quotes, missing, ccy="USD"):
    return {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ccy": ccy,
        "source": CCY_CONF[ccy]["source"],
        "quotes": quotes,
        "missing": missing,
    }


def fetch_fx(host="localhost", port=8194, timeout_s=20):
    """USDJPY (and any future pairs in FX_TICKERS) via PX_LAST, PX_MID fallback."""
    import blpapi  # noqa (same install note as fetch_curve)
    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port}")
    vals = {}
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        refdata = session.getService("//blp/refdata")
        request = refdata.createRequest("ReferenceDataRequest")
        for tk in FX_TICKERS.values():
            request.getElement("securities").appendValue(tk)
        for f in ("PX_LAST", "PX_MID"):
            request.getElement("fields").appendValue(f)
        session.sendRequest(request)
        deadline = time.time() + timeout_s
        done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    for f in ("PX_LAST", "PX_MID"):
                        if fd.hasElement(f):
                            try:
                                vals.setdefault(tk, float(fd.getElementAsFloat(f)))
                            except Exception:
                                pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()
    pairs = {name: vals.get(tk) for name, tk in FX_TICKERS.items()}
    return {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Bloomberg blpapi PX_LAST/PX_MID",
        "pairs": pairs,
    }


def fetch_surface(host="localhost", port=8194, fields=("PX_LAST", "CHG_NET_1D", "CHG_NET_3D"), timeout_s=30):
    """Return (surfaces_by_field, ticker_map, missing). One request, multiple fields:
    surfaces_by_field = { field: { exp: { tail: value|None } } }."""
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit(
            "blpapi is not installed. Install with:\n"
            "  pip install --index-url "
            "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi\n"
            f"(import error: {e})"
        )
    fwd = build_surface_map()
    fwdFp = {k: v.replace("USSNA", "USSFA", 1) for k, v in fwd.items()}   # ICAP published forward premium (cents)
    securities = sorted(set(fwd.values()) | set(fwdFp.values()))
    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} "
                           "(is the Terminal running / DAPI enabled?)")
    values = {f: {} for f in fields}     # field -> {ticker: float}
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        refdata = session.getService("//blp/refdata")
        request = refdata.createRequest("ReferenceDataRequest")
        for s in securities:
            request.getElement("securities").appendValue(s)
        for f in fields:
            request.getElement("fields").appendValue(f)
        session.sendRequest(request)
        deadline = time.time() + timeout_s
        done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f):
                            try:
                                values[f][tk] = float(fd.getElementAsFloat(f))
                            except Exception:
                                pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()
    surfaces = {}
    for f in fields:
        surf = {e: {t: None for t in SURF_TEN} for e in SURF_EXP}
        for key, tk in fwd.items():
            e, t = key.split("|")
            surf[e][t] = values[f].get(tk)
        surfaces[f] = surf
    fpsurf = {e: {t: None for t in SURF_TEN} for e in SURF_EXP}   # USSFA forward premium (cents)
    for key, tk in fwdFp.items():
        e, t = key.split("|")
        fpsurf[e][t] = values.get("PX_LAST", {}).get(tk)
    missing = [key for key, tk in fwd.items() if values.get("PX_LAST", {}).get(tk) is None]
    return surfaces, fpsurf, fwd, missing


def make_surface_payload(surfaces, fpsurf, ticker_map, missing):
    return {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ICAP/Parameta via Bloomberg blpapi (USSNA vol + USSFA fwd-prem, ICPL Curncy)",
        "fields": {"surface": "PX_LAST", "chg1d": "CHG_NET_1D", "chg3d": "CHG_NET_3D", "fp": "PX_LAST (USSFA)"},
        "surface": surfaces.get("PX_LAST", {}),
        "chg1d": surfaces.get("CHG_NET_1D", {}),
        "chg3d": surfaces.get("CHG_NET_3D", {}),
        "fp": fpsurf,
        "tickers": ticker_map, "missing": missing,
    }


# ----------------------------------------------------------------------------
# FOMC meeting dates — best-effort scrape of the Fed calendar, with a baked
# fallback. Decision date = last day of each meeting's range (~always a Wed).
# ----------------------------------------------------------------------------
FOMC_FALLBACK = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
_FED_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def fetch_fomc(timeout_s=10):
    """Scrape the Fed FOMC calendar -> sorted list of ISO decision dates, or None
    on any failure / implausible parse (caller then uses FOMC_FALLBACK)."""
    try:
        req = urllib.request.Request(_FED_FOMC_URL, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=timeout_s).read().decode("utf-8", "ignore")
    except Exception:
        return None
    try:
        heads = list(re.finditer(r"(\d{4})\s+FOMC\s+Meetings", html))
        if not heads:
            return None
        out = []
        for i, h in enumerate(heads):
            yr = int(h.group(1))
            seg = html[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(html)]
            pairs = re.findall(
                r"fomc-meeting__month[^>]*>(.*?)</div>.*?fomc-meeting__date[^>]*>(.*?)</div>",
                seg, re.S | re.I)
            for mraw, draw in pairs:
                months = [_MONTHS[t[:3].lower()] for t in re.findall(r"[A-Za-z]+", re.sub(r"<[^>]+>", " ", mraw))
                          if t[:3].lower() in _MONTHS]
                nums = re.findall(r"\d+", re.sub(r"<[^>]+>", " ", draw))
                if not months or not nums:
                    continue
                try:
                    out.append(_date(yr, months[-1], int(nums[-1])))   # last day of the range
                except ValueError:
                    continue
        if len(out) < 6:
            return None
        if sum(1 for d in out if d.weekday() == 2) < 0.7 * len(out):   # mostly Wednesdays
            return None
        today = _date.today()
        lo, hi = _date(today.year - 1, 1, 1), _date(today.year + 3, 12, 31)
        out = sorted(set(d for d in out if lo <= d <= hi))
        return [d.isoformat() for d in out] if len(out) >= 6 else None
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Fed-funds / SOFR live pricing: EFFR (FEDL01), FOMC meeting path (USSOFED1..9),
# and the front 12 quarterly SOFR futures (whites/reds/greens). PX_LAST for the
# rates, PX_MID (fallback PX_LAST) for the futures.
# ----------------------------------------------------------------------------
_Q_MONTH = {"H": 3, "M": 6, "U": 9, "Z": 12}     # 3M-SOFR reference-quarter START month
_Q_END = {"H": 6, "M": 9, "U": 12, "Z": 3}       # reference-quarter END (~last-trade) month


def _third_wed(y, m):
    weds = [w[2] for w in calendar.monthcalendar(y, m) if w[2] != 0]
    return _date(y, m, weds[2])


def build_sfr_strip(n=12, as_of=None):
    """Front n quarterly 3M-SOFR futures tagged white/red/green. Includes the contract
    currently inside its reference quarter (already past its start IMM but still trading,
    e.g. SFRM6 between mid-Jun and mid-Sep) by filtering on the reference-quarter END."""
    as_of = as_of or _date.today()
    qs = []
    for y in range(as_of.year - 1, as_of.year + 5):
        for L in ("H", "M", "U", "Z"):
            start = _third_wed(y, _Q_MONTH[L])
            end = _third_wed(y + 1 if L == "Z" else y, _Q_END[L])   # last-trade ~ end of reference quarter
            qs.append((start, end, f"SFR{L}{y % 100:02d} Comdty"))  # 2-digit year (SFRU8 is ambiguous 2018/2028)
    qs = sorted((q for q in qs if q[1] > as_of), key=lambda q: q[0])[:n]   # still-trading -> reference qtr not yet ended
    strip = lambda i: "white" if i < 4 else "red" if i < 8 else "green"
    return [{"ticker": t, "strip": strip(i)} for i, (st, en, t) in enumerate(qs)]


def fetch_fedsofr(host="localhost", port=8194, timeout_s=30):
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit(
            "blpapi is not installed. Install with:\n"
            "  pip install --index-url "
            "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi\n"
            f"(import error: {e})")
    strip = build_sfr_strip(12)
    meeting_tks = [f"USSOFED{i} Curncy" for i in range(1, 10)]
    basis_tks = [f"USSFRFF{i} GFUS Curncy" for i in range(1, 17)]   # SOFR-FedFunds basis (GFUS mid), per quarterly contract
    secs = ["FEDL01 Index"] + meeting_tks + [f["ticker"] for f in strip] + basis_tks
    fields = ["PX_LAST", "PX_MID"]
    opts = blpapi.SessionOptions(); opts.setServerHost(host); opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} "
                           "(is the Terminal running / DAPI enabled?)")
    vals = {}
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        refdata = session.getService("//blp/refdata")
        req = refdata.createRequest("ReferenceDataRequest")
        for sec in secs:
            req.getElement("securities").appendValue(sec)
        for f in fields:
            req.getElement("fields").appendValue(f)
        session.sendRequest(req)
        deadline = time.time() + timeout_s; done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i); tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f):
                            try:
                                vals[(tk, f)] = float(fd.getElementAsFloat(f))
                            except Exception:
                                pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()
    meetings = [{"ticker": f"USSOFED{i}", "rate": vals.get((f"USSOFED{i} Curncy", "PX_LAST"))} for i in range(1, 10)]
    futures = []
    for f in strip:
        px = vals.get((f["ticker"], "PX_MID"))
        if px is None:
            px = vals.get((f["ticker"], "PX_LAST"))
        futures.append({"ticker": f["ticker"].replace(" Comdty", ""), "strip": f["strip"], "px": px})
    basis = [vals.get((f"USSFRFF{i} GFUS Curncy", "PX_MID")) for i in range(1, 17)]   # index 0 -> USSFRFF1 (front quarterly)
    return {"effr": vals.get(("FEDL01 Index", "PX_LAST")), "meetings": meetings, "futures": futures, "basis": basis}


# ----------------------------------------------------------------------------
# SOFR option strike vols: ATM future mid + the OTM-wing implied vol (IVOL_MID)
# at +/-max_ticks*tick around ATM. fut = 2-digit future base (SFRU28); opt =
# 1-digit option base (SFRU8). Option ticker: "<opt><C|P> <strike4dp> Comdty".
# ----------------------------------------------------------------------------
def fetch_sofrvol(host="localhost", port=8194, fut="", opt="", max_ticks=16, tick=0.0625, timeout_s=30):
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit(
            "blpapi is not installed. Install with:\n"
            "  pip install --index-url "
            "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi\n"
            f"(import error: {e})")
    opts = blpapi.SessionOptions(); opts.setServerHost(host); opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} (Terminal/DAPI?)")

    def _pull(secs, fields):
        refdata = session.getService("//blp/refdata")
        req = refdata.createRequest("ReferenceDataRequest")
        for sec in secs:
            req.getElement("securities").appendValue(sec)
        for f in fields:
            req.getElement("fields").appendValue(f)
        session.sendRequest(req)
        vals = {}; deadline = time.time() + timeout_s; done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i); tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f):
                            try:
                                vals[(tk, f)] = float(fd.getElementAsFloat(f))
                            except Exception:
                                pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
        return vals

    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        futsec = f"{fut} Comdty"
        v1 = _pull([futsec], ["PX_MID", "PX_LAST"])
        atm = v1.get((futsec, "PX_MID"))
        if atm is None:
            atm = v1.get((futsec, "PX_LAST"))
        if atm is None:
            return {"error": f"no price for {futsec}", "atm": None, "fut": fut, "contract": opt, "strikes": []}
        atmK = round(atm / tick) * tick
        specs = []
        for k in range(-max_ticks, max_ticks + 1):
            strike = round(atmK + k * tick, 4)
            side = "P" if k < 0 else "C"
            specs.append((k, strike, side, f"{opt}{side} {strike:.4f} Comdty"))
        v2 = _pull([sp[3] for sp in specs], ["IVOL_MID", "OPEN_INT"])
        strikes = [{"k": k, "offsetBp": round(k * tick * 100, 4), "strike": strike, "side": side,
                    "iv": v2.get((tkr, "IVOL_MID")), "oi": v2.get((tkr, "OPEN_INT"))} for (k, strike, side, tkr) in specs]
    finally:
        session.stop()
    return {"atm": atm, "atmK": round(atmK, 4), "tick": tick, "contract": opt, "fut": fut, "strikes": strikes}


_QM = {"H": 3, "M": 6, "U": 9, "Z": 12}     # quarterly contract month (option expiry month)
_STRIKE_TICK = 0.125                          # back-month SOFR options list 12.5bp strikes


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bachelier_call(F, K, sig, T):
    """Premium of a call on the futures PRICE under a normal (Bachelier) model."""
    if sig <= 0 or T <= 0:
        return max(F - K, 0.0)
    v = sig * math.sqrt(T); d = (F - K) / v
    return (F - K) * _norm_cdf(d) + v * _norm_pdf(d)


def _bach_iv(C, F, K, T):
    """Implied normal vol (price points / yr) from a call premium; None if degenerate.
    abpv (bp) = returned sigma * 100, since 1 future price point = 100bp of rate."""
    if C is None or F is None or K is None or T is None or T <= 0:
        return None
    if C <= max(F - K, 0.0) + 1e-9:
        return None
    lo, hi = 1e-6, 10.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _bachelier_call(F, K, mid, T) > C:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def fetch_sofrvolstrip(host="localhost", port=8194, n=16, tick=0.0625, timeout_s=45):
    """Whites/Reds/Greens/Blues vol summary. Per front-16 quarterly (options still live):
    underlying rate, ATM abpv vol, 20d realised vol (from the future's daily settles),
    change-on-day of ATM vol, ATM-option price and previous settle, and option expiry."""
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit("blpapi is not installed (see fetch_curve docstring).")
    import math
    import statistics
    opts = blpapi.SessionOptions(); opts.setServerHost(host); opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} (Terminal/DAPI?)")

    def _ref(secs, fields):
        refdata = session.getService("//blp/refdata")
        req = refdata.createRequest("ReferenceDataRequest")
        for s in secs:
            req.getElement("securities").appendValue(s)
        for f in fields:
            req.getElement("fields").appendValue(f)
        session.sendRequest(req)
        vals = {}; deadline = time.time() + timeout_s; done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i); tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f):
                            try:
                                vals[(tk, f)] = float(fd.getElementAsFloat(f))
                            except Exception:
                                pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
        return vals

    def _hist(secs, field, days=45):
        refdata = session.getService("//blp/refdata")
        req = refdata.createRequest("HistoricalDataRequest")
        for s in secs:
            req.getElement("securities").appendValue(s)
        req.getElement("fields").appendValue(field)
        req.set("periodicitySelection", "DAILY")
        end = _date.today(); start = end - _timedelta(days=days)
        req.set("startDate", start.strftime("%Y%m%d")); req.set("endDate", end.strftime("%Y%m%d"))
        session.sendRequest(req)
        series = {}; deadline = time.time() + timeout_s; done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                sd = msg.getElement("securityData")
                tk = sd.getElementAsString("security")
                if sd.hasElement("fieldData"):
                    fda = sd.getElement("fieldData"); out = []
                    for j in range(fda.numValues()):
                        bar = fda.getValueAsElement(j)
                        if bar.hasElement(field):
                            try:
                                out.append(float(bar.getElementAsFloat(field)))
                            except Exception:
                                pass
                    series[tk] = out
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
        return series

    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        # front-16 quarterlies whose OPTIONS are still listed (expiry > today), like the pricer
        today = _date.today()
        qs = []
        for y in range(today.year - 1, today.year + 6):
            for L in ("H", "M", "U", "Z"):
                exp = _third_wed(y, _QM[L]) - _timedelta(days=5)   # Fri before 3rd Wed
                qs.append((L, y, exp))
        qs = sorted((q for q in qs if q[2] > today), key=lambda q: q[2])[:n]
        meta = []
        for i, (L, y, exp) in enumerate(qs):
            fut = f"SFR{L}{y % 100:02d}"
            meta.append({"name": f"SFR{L}{y % 10}", "futsec": f"{fut} Comdty", "opt": f"SFR{L}{y % 10}",
                         "strip": ("white" if i < 4 else "red" if i < 8 else "green" if i < 12 else "blue"),
                         "expiry": exp.isoformat(), "exp": exp})
        pv = _ref([m["futsec"] for m in meta], ["PX_MID", "PX_LAST"])
        for m in meta:
            px = pv.get((m["futsec"], "PX_MID"))
            if px is None:
                px = pv.get((m["futsec"], "PX_LAST"))
            m["px"] = px
            m["atmK"] = round(px / _STRIKE_TICK) * _STRIKE_TICK if px is not None else None
            m["atmopt"] = f"{m['opt']}C {m['atmK']:.4f} Comdty" if px is not None else None
        atmsecs = [m["atmopt"] for m in meta if m["atmopt"]]
        ov = _ref(atmsecs, ["IVOL_MID", "PX_MID", "PX_LAST", "PX_SETTLE"]) if atmsecs else {}
        fhist = _hist([m["futsec"] for m in meta], "PX_SETTLE", days=45)
        ohist = _hist(atmsecs, "IVOL_MID", days=12) if atmsecs else {}
        osettle = _hist(atmsecs, "PX_SETTLE", days=8) if atmsecs else {}   # for settle-implied vol & COD
        rows = []
        for m in meta:
            px = m["px"]; a = m["atmopt"]
            fwd = (100 - px) if px is not None else None
            T = max((m["exp"] - today).days / 365.0, 1.0 / 365.0)
            iv = ov.get((a, "IVOL_MID")) if a else None
            osett = osettle.get(a, []) if a else []
            atmVol = (iv * fwd) if (iv is not None and fwd is not None) else None
            if atmVol is None and px is not None and a and osett:           # IVOL blank (back months) -> imply from settle
                s = _bach_iv(osett[-1], px, m["atmK"], T)
                atmVol = s * 100 if s else None
            price = (ov.get((a, "PX_MID")) if a else None)
            if price is None and a:
                price = ov.get((a, "PX_LAST"))
            prevSettle = ov.get((a, "PX_SETTLE")) if a else None
            settles = fhist.get(m["futsec"], [])
            realized = None
            if len(settles) >= 6:
                rr = [100 - s for s in settles]
                chg = [(rr[i] - rr[i - 1]) * 100 for i in range(1, len(rr))][-20:]   # daily rate moves, bp
                if len(chg) >= 3:
                    realized = statistics.stdev(chg) * math.sqrt(252)
            # change-on-day of ATM vol: prefer historical IVOL_MID; else EOD settle-implied normal vol
            ivs = ohist.get(a, []) if a else []
            cod = None
            if len(ivs) >= 2 and fwd is not None:
                cod = (ivs[-1] - ivs[-2]) * fwd
            elif len(osett) >= 2 and len(settles) >= 2 and px is not None and a:
                st = _bach_iv(osett[-1], settles[-1], m["atmK"], T)
                sp = _bach_iv(osett[-2], settles[-2], m["atmK"], T + 1.0 / 365.0)
                if st is not None and sp is not None:
                    cod = (st - sp) * 100
            rows.append({"name": m["name"], "strip": m["strip"], "expiry": m["expiry"],
                         "rate": (100 - px) if px is not None else None, "atmVol": atmVol,
                         "realized20": realized, "cod": cod, "price": price, "prevSettle": prevSettle})
    finally:
        session.stop()
    return {"rows": rows, "tick": tick}


# ----------------------------------------------------------------------------
# HTTP server (cached, periodic refresh, CORS-enabled) -- mirrors icap_vol_feed
# ----------------------------------------------------------------------------
class _Cache:
    def __init__(self, host, port, interval, ccy="USD"):
        self.host, self.port, self.interval, self.ccy = host, port, interval, ccy
        self.lock = threading.Lock()
        self.payload = None
        self.last = 0.0

    def get(self, force=False):
        with self.lock:
            stale = (time.time() - self.last) > self.interval
            if force or stale or self.payload is None:
                try:
                    quotes, missing = fetch_curve(self.host, self.port, ccy=self.ccy)
                    self.payload = make_payload(quotes, missing, ccy=self.ccy)
                    self.last = time.time()
                except Exception as e:
                    if self.payload is None:
                        self.payload = {"error": str(e), "quotes": []}
            return self.payload


def serve(host, port, http_port, interval, ui_path, open_browser):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    caches = {c: _Cache(host, port, interval, ccy=c) for c in CCY_CONF}
    cache = caches["USD"]

    # USDJPY spot cache (same blpapi session params; refreshed with LOAD)
    fx_lock = threading.Lock()
    fx_state = {"payload": None, "last": 0.0}

    def fx_get(force=False):
        with fx_lock:
            stale = (time.time() - fx_state["last"]) > interval
            if force or stale or fx_state["payload"] is None:
                try:
                    fx_state["payload"] = fetch_fx(host, port)
                    fx_state["last"] = time.time()
                except Exception as e:
                    if fx_state["payload"] is None:
                        fx_state["payload"] = {"error": str(e), "pairs": {}}
            return fx_state["payload"]

    # ICAP surface cache (own lock/timestamp; same blpapi host/port as the curve)
    surf_lock = threading.Lock()
    surf_state = {"payload": None, "last": 0.0}

    def surf_get(force=False):
        with surf_lock:
            stale = (time.time() - surf_state["last"]) > interval
            if force or stale or surf_state["payload"] is None:
                try:
                    surfaces, fpsurf, tk, missing = fetch_surface(host, port)
                    surf_state["payload"] = make_surface_payload(surfaces, fpsurf, tk, missing)
                    surf_state["last"] = time.time()
                except Exception as e:
                    if surf_state["payload"] is None:
                        surf_state["payload"] = {"error": str(e), "surface": {}}
            return surf_state["payload"]

    # FOMC dates cache (scraped daily from the Fed; falls back to a baked list)
    fomc_lock = threading.Lock()
    fomc_state = {"payload": None, "last": 0.0}

    def fomc_get(force=False):
        with fomc_lock:
            stale = (time.time() - fomc_state["last"]) > 86400   # refresh at most once a day
            if force or stale or fomc_state["payload"] is None:
                dates = fetch_fomc()
                src = "fed" if dates else "fallback"
                fomc_state["payload"] = {
                    "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": src, "dates": dates or list(FOMC_FALLBACK),
                }
                fomc_state["last"] = time.time()
            return fomc_state["payload"]

    # Fed-funds / SOFR live pricing cache (market data -> refreshes with LOAD)
    fedsofr_lock = threading.Lock()
    fedsofr_state = {"payload": None, "last": 0.0}

    def fedsofr_get(force=False):
        with fedsofr_lock:
            stale = (time.time() - fedsofr_state["last"]) > interval
            if force or stale or fedsofr_state["payload"] is None:
                try:
                    d = fetch_fedsofr(host, port)
                    d["asof"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fedsofr_state["payload"] = d
                    fedsofr_state["last"] = time.time()
                except Exception as e:
                    if fedsofr_state["payload"] is None:
                        fedsofr_state["payload"] = {"error": str(e), "effr": None, "meetings": [], "futures": []}
            return fedsofr_state["payload"]

    # SOFR option strike-vol cache, keyed per contract (button-triggered LOAD SOFR VOL)
    sofrvol_lock = threading.Lock()
    sofrvol_state = {}

    def sofrvol_get(fut, opt, force=False):
        key = fut + "|" + opt
        with sofrvol_lock:
            ent = sofrvol_state.get(key)
            stale = ent is None or (time.time() - ent["last"]) > interval
            if force or stale:
                try:
                    d = fetch_sofrvol(host, port, fut, opt)
                    d["asof"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    sofrvol_state[key] = {"payload": d, "last": time.time()}
                except Exception as e:
                    if ent is None:
                        sofrvol_state[key] = {"payload": {"error": str(e), "atm": None, "strikes": []}, "last": time.time()}
            return sofrvol_state[key]["payload"]

    sofrvolstrip_lock = threading.Lock()
    sofrvolstrip_state = {"payload": None, "last": 0.0}

    def sofrvolstrip_get(force=False):
        with sofrvolstrip_lock:
            stale = (time.time() - sofrvolstrip_state["last"]) > interval
            if force or stale or sofrvolstrip_state["payload"] is None:
                try:
                    sofrvolstrip_state["payload"] = fetch_sofrvolstrip(host, port)
                    sofrvolstrip_state["last"] = time.time()
                except Exception as e:
                    if sofrvolstrip_state["payload"] is None:
                        sofrvolstrip_state["payload"] = {"error": str(e), "rows": []}
            return sofrvolstrip_state["payload"]

    class Handler(BaseHTTPRequestHandler):
        def _headers(self, code, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _send(self, code, body):
            self._headers(code, "application/json")
            self.wfile.write(body.encode("utf-8"))

        def _send_html(self):
            if ui_path and os.path.isfile(ui_path):
                with open(ui_path, "rb") as f:
                    data = f.read()
                self._headers(200, "text/html; charset=utf-8")
                self.wfile.write(data)
            else:
                self._headers(404, "text/html; charset=utf-8")
                self.wfile.write(
                    (f"<h3>UI file not found</h3><p>Expected SwapPricer.html at "
                     f"<code>{ui_path}</code>. Put it next to this script, or pass --ui &lt;path&gt;.</p>"
                     f"<p>The data feed is live at <a href='/curve'>/curve</a>.</p>").encode("utf-8"))

        def do_OPTIONS(self):
            self._headers(204, "application/json")

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path in ("/curve", "/health"):
                force = "force" in self.path or "refresh" in self.path
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ccy = (q.get("ccy", ["USD"])[0]).strip().upper() or "USD"
                if ccy not in caches:
                    self._send(400, json.dumps({"error": f"unknown ccy '{ccy}' (have {sorted(caches)})", "quotes": []}))
                else:
                    self._send(200, json.dumps(caches[ccy].get(force=force)))
            elif path == "/fx":
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(fx_get(force=force)))
            elif path == "/surface":
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(surf_get(force=force)))
            elif path == "/fomc":
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(fomc_get(force=force)))
            elif path == "/fedsofr":
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(fedsofr_get(force=force)))
            elif path == "/sofrvol":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                fut = (q.get("fut", [""])[0]).strip()
                opt = (q.get("opt", [""])[0]).strip()
                force = "force" in self.path or "refresh" in self.path
                if not fut or not opt:
                    self._send(400, json.dumps({"error": "need fut & opt params", "strikes": []}))
                else:
                    self._send(200, json.dumps(sofrvol_get(fut, opt, force=force)))
            elif path == "/sofrvolstrip":
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(sofrvolstrip_get(force=force)))
            elif path == "/snapshot":
                snap = os.path.join(SCRIPT_DIR, "sofr_curve.json")
                if os.path.isfile(snap):
                    with open(snap, encoding="utf-8") as fh:
                        self._send(200, fh.read())
                else:
                    self._send(404, json.dumps({"error": f"sofr_curve.json not found in {SCRIPT_DIR}"}))
            elif path in ("", "/index.html", "/swappricer.html", "/SwapPricer.html"):
                self._send_html()
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def log_message(self, *a):
            pass

    # Loopback only; fall through alternate ports on Windows excluded-range 10013.
    candidates = [http_port, 8788, 8900, 9322, 0]
    httpd = None
    for cand in candidates:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            break
        except OSError as e:
            print(f"[sofr_curve_feed] port {cand} unavailable ({e}); trying next...")
    if httpd is None:
        raise SystemExit("[sofr_curve_feed] could not bind any port. "
                         "Run with --http-port <N> using a known-free port.")
    bound = httpd.server_address[1]
    base = f"http://localhost:{bound}"
    print("=" * 60)
    print(f"[sofr_curve_feed] UI    : {base}/")
    print(f"[sofr_curve_feed] feed  : {base}/curve (USD) + /curve?ccy=JPY + /fx (USDJPY) + {base}/surface (ICAP vols)")
    print(f"[sofr_curve_feed] blpapi: {host}:{port}  refresh {interval}s")
    if not (ui_path and os.path.isfile(ui_path)):
        print(f"[sofr_curve_feed] NOTE: UI file not found at {ui_path} — save SwapPricer.html there.")
    print(f"[sofr_curve_feed] >>> OPEN THIS IN A BROWSER:  {base}/")
    print("=" * 60)
    if open_browser:
        try:
            webbrowser.open(base + "/")
        except Exception:
            pass
    for c in caches.values():
        c.get(force=True)  # warm USD + JPY
    httpd.serve_forever()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="USD SOFR OIS curve feed via Bloomberg blpapi")
    p.add_argument("--host", default="localhost", help="blpapi server host (default localhost / DAPI)")
    p.add_argument("--port", type=int, default=8194, help="blpapi server port (default 8194)")
    p.add_argument("--out", default=None, help="write JSON snapshot to this path")
    p.add_argument("--serve", action="store_true", help="run HTTP server for the artifact to fetch")
    p.add_argument("--http-port", type=int, default=8196, help="HTTP server port (default 8196)")
    p.add_argument("--interval", type=int, default=60, help="server cache refresh seconds (default 60)")
    p.add_argument("--ui", default=os.path.join(SCRIPT_DIR, "UnifiedPricer.html"),
                   help="path to the pricer HTML to serve at '/' (default: alongside this script)")
    p.add_argument("--no-open", action="store_true", help="do not auto-open the browser when serving")
    p.add_argument("--print-tickers", action="store_true", help="just print the ticker grid and exit")
    args = p.parse_args()

    if args.print_tickers:
        for c in CCY_CONF:
            print(f"--- {c} ---")
            for tenor, kind, ticker, field in build_ticker_map(c):
                print(f"{tenor:5s} {kind:5s} {ticker:20s} {field}")
        for name, tk in FX_TICKERS.items():
            print(f"--- FX --- {name}: {tk}")
        return

    if args.serve:
        serve(args.host, args.port, args.http_port, args.interval,
              args.ui, not args.no_open)
        return

    # snapshot mode
    quotes, missing = fetch_curve(args.host, args.port)
    payload = make_payload(quotes, missing)
    text = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {args.out}")
    print(text)
    if missing:
        print(f"\nWARNING: {len(missing)} tenors had no data: {', '.join(missing)}", file=sys.stderr)


if __name__ == "__main__":
    main()
