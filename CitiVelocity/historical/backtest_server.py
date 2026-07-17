#!/usr/bin/env python3
"""
backtest_server.py -- ad-hoc historical short-straddle backtest for the pricer's
Backtester tab. duckdb-backed (reads the local historical parquet store), loopback
HTTP + CORS, same bridge pattern as the other feeds.

Endpoints:
  GET /eodcurve?ccy=JPY
    Latest EOD par curve + 1y-fwd suite for a ccy from the historical store —
    the pricer's FULL LOAD uses this as CALIBRATION FALLBACK when the Citi live
    stream has no ticks for that ccy (e.g. JPY_TONAR CLOSE points appear not to
    republish outside Tokyo hours; observed 2026-07-06).
    -> { ccy, asof, quotes:[{tenor,kind,rate,ts}], forwards:[{expiry,tenor,rate,ts}] }
  GET /backtest?expiry=1M&tail=10Y&notional=10000000&interval=weekly&lookback=1y
    expiry   in {1M,3M,6M}
    tail     in {5Y,10Y,30Y}
    notional dollars per straddle sold (default 10,000,000)
    interval in {weekly,daily}   (weekly = first business day of each ISO week)
    lookback in {1y,5y}
  -> JSON: { meta, hairs:[[[t_ms,pnl],...],...], cum:[[t_ms,cum$],...], sharpe, summary }

Model (see Skillsmd knowledge docs / the tab):
  * ATM straddle struck at SPOT par rate (rough ATM=spot assumption).
  * Normal (Bachelier) vol from the store's ATM-NORMAL series (bp/yr).
  * Flat-yield analytic annuity A = (1-(1+S)^-tau)/S from the tail rate.
  * Premium$   = 0.7979 * sigmaN * sqrt(T) * A * N
  * Hair MTM   = frozen ENTRY vol, decayed to remaining T, current spot & annuity
                 (needs no sub-1m vol; converges exactly to intrinsic at expiry).
  * Program P&L (short) = premium - MTM; cumulative = aggregate MTM equity curve.
  * Sharpe = mean/std of WEEKLY $ P&L increments * sqrt(52) (raw dollars).

Delivered UNRUN in the sandbox (the parquet read + serving happen on the box).
"""
from __future__ import annotations
import json, math, os
from datetime import date, datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARQUET = os.path.join(SCRIPT_DIR, "data", "**", "*.parquet")

SQRT_2PI = math.sqrt(2 * math.pi)
STRADDLE_K = 2.0 / SQRT_2PI          # 0.79788: ATM normal straddle coeff
EXPIRY_MONTHS = {"1M": 1, "3M": 3, "6M": 6}
TAIL_YEARS = {"5Y": 5, "10Y": 10, "30Y": 30}

# --- unit assumptions (surfaced in meta so they're verifiable on the box) ---
RATE_TO_DECIMAL = 0.01        # store PAR value is in percent -> decimal
VOL_TO_ABS = 1e-4             # store NORMAL vol is bp/yr -> absolute rate/yr


# ============================================================================
# Pure math (unit-tested; no data/network)
# ============================================================================
def norm_cdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def norm_pdf(x): return math.exp(-0.5 * x * x) / SQRT_2PI


def straddle_rate(F, K, T, sig):
    """Bachelier ATM(-ish) straddle value in RATE terms (per unit annuity/notional).
    Converges to |F-K| as T->0 regardless of sig."""
    if T <= 0 or sig <= 0:
        return abs(F - K)
    s = sig * math.sqrt(T)
    d = (F - K) / s
    return (F - K) * (2 * norm_cdf(d) - 1) + 2 * s * norm_pdf(d)


def annuity(S, tau):
    """Flat-yield annual annuity of a tau-year swap at flat rate S (decimal)."""
    if S is None:
        return float(tau)
    if abs(S) < 1e-9:
        return float(tau)
    return (1.0 - (1.0 + S) ** (-tau)) / S


def _add_months(d: date, m: int) -> date:
    y, mo = d.year, d.month - 1 + m
    y += mo // 12
    mo = mo % 12 + 1
    dd = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1])
    return date(y, mo, dd)


def _weekly_first(dates):
    """Indices of the first date in each ISO (year,week)."""
    seen, idx = set(), []
    for i, d in enumerate(dates):
        k = d.isocalendar()[:2]
        if k not in seen:
            seen.add(k); idx.append(i)
    return idx


def run_backtest(dates, vol_bp, rate_pct, expiry, tail, notional, interval):
    """dates: list[date] (sorted, business days). vol_bp: ATM normal vol bp/yr.
    rate_pct: spot par rate in percent. Returns dict payload (no I/O)."""
    n = len(dates)
    months = EXPIRY_MONTHS[expiry]; tau = TAIL_YEARS[tail]
    sig = [v * VOL_TO_ABS for v in vol_bp]
    S = [r * RATE_TO_DECIMAL for r in rate_pct]
    tms = [int(datetime(d.year, d.month, d.day).timestamp() * 1000) for d in dates]

    sale_idx = _weekly_first(dates) if interval == "weekly" else list(range(n))
    equity = [0.0] * n            # aggregate MTM equity curve
    hairs, premiums, terminals = [], [], []

    for a in sale_idx:
        exp_d = _add_months(dates[a], months)
        # b = last data index at/just-before expiry (within series)
        b = a
        while b + 1 < n and dates[b + 1] <= exp_d:
            b += 1
        if b <= a:
            continue  # not enough life in-sample yet
        sig0, K = sig[a], S[a]
        T0 = max((exp_d - dates[a]).days / 365.25, 1e-6)   # act/365.25 to actual expiry
        prem = STRADDLE_K * sig0 * math.sqrt(T0) * annuity(K, tau) * notional
        premiums.append(prem)
        hair = []
        for j in range(a, b + 1):
            Tt = max((exp_d - dates[j]).days / 365.25, 0.0)
            val = straddle_rate(S[j], K, Tt, sig0) * annuity(S[j], tau) * notional
            pnl = prem - val
            hair.append([tms[j], round(pnl)])
            equity[j] += pnl
        term = hair[-1][1]
        terminals.append(term)
        for j in range(b + 1, n):     # lock terminal P&L forward
            equity[j] += term
        hairs.append(hair)

    cum = [[tms[k], round(equity[k])] for k in range(n)]

    # weekly $ P&L increments -> Sharpe
    wk = _weekly_first(dates)
    wk_equity = [equity[i] for i in wk]
    incs = [wk_equity[i] - wk_equity[i - 1] for i in range(1, len(wk_equity))]
    sharpe = None
    mean_w = std_w = 0.0
    if len(incs) > 2:
        mean_w = sum(incs) / len(incs)
        var = sum((x - mean_w) ** 2 for x in incs) / (len(incs) - 1)
        std_w = math.sqrt(var)
        sharpe = (mean_w / std_w * math.sqrt(52.0)) if std_w > 1e-9 else None

    hit = sum(1 for t in terminals if t > 0) / len(terminals) if terminals else None
    return {
        "meta": {
            "expiry": expiry, "tail": tail, "notional": notional, "interval": interval,
            "n_sales": len(hairs), "start": str(dates[0]), "end": str(dates[-1]),
            "rate_units": "percent->decimal", "vol_units": "bp/yr->abs",
            "sample_vol_bp": round(vol_bp[-1], 2) if vol_bp else None,
            "sample_rate_pct": round(rate_pct[-1], 4) if rate_pct else None,
            "model": "short ATM(=spot) straddle, flat-yield annuity, frozen-vol decay MTM, unhedged",
        },
        "hairs": hairs, "cum": cum, "sharpe": sharpe,
        "summary": {
            "total_pnl": round(equity[-1]) if equity else 0,
            "avg_premium": round(sum(premiums) / len(premiums)) if premiums else 0,
            "hit_rate": round(hit, 3) if hit is not None else None,
            "mean_weekly": round(mean_w), "std_weekly": round(std_w),
        },
    }


# ============================================================================
# Data load (duckdb; runs on the box)
# ============================================================================
def load_series(expiry, tail):
    import duckdb
    con = duckdb.connect(":memory:")
    g = PARQUET.replace("\\", "/")
    # ATM NORMAL vol (prefer RFR/ATM_RFR underlying; fall back to any basis)
    vq = (f"select date, value from read_parquet('{g}', hive_partitioning=true) "
          f"where product='vol' and currency='USD' and vol_type='NORMAL' "
          f"and expiry=? and tenor=? {{}} order by date")
    rows = con.execute(vq.format("and basis='RFR'"), [expiry, tail]).fetchall()
    if not rows:
        rows = con.execute(vq.format(""), [expiry, tail]).fetchall()
    vol = {str(d): float(v) for d, v in rows if v is not None}
    rr = con.execute(f"select date, value from read_parquet('{g}', hive_partitioning=true) "
                     f"where product='swap_par' and currency='USD' and tenor=? order by date",
                     [tail]).fetchall()
    rate = {str(d): float(v) for d, v in rr if v is not None}
    con.close()
    common = sorted(set(vol) & set(rate))
    dates = [datetime.strptime(s[:10], "%Y-%m-%d").date() for s in common]
    return dates, [vol[s] for s in common], [rate[s] for s in common]


def eod_curve(ccy, leg=None):
    """Latest-date par curve + 1y-fwd suite for one ccy from the parquet store.
    row_number window keeps the freshest observation per series (store advances
    daily via run_daily.bat, so 'EOD' = yesterday's close on a healthy box).
    leg (2026-07-07, dual-curve ccys): EUR/AUD hold TWO families under one
    currency — OIS (index_name set, e.g. EUROSTR) and SWAP_LIBOR (index_name
    empty). leg='ois'/'irs' filters accordingly; leg=None keeps the legacy
    unfiltered behavior (safe for single-family ccys)."""
    import duckdb
    con = duckdb.connect(":memory:")
    g = PARQUET.replace("\\", "/")
    legf = {"ois": " and index_name is not null and index_name <> ''",
            "irs": " and (index_name is null or index_name = '')"}.get(leg or "", "")
    par = con.execute(
        f"select tenor, value, date from ("
        f" select tenor, value, date, row_number() over (partition by tenor order by date desc) rn"
        f" from read_parquet('{g}', hive_partitioning=true)"
        f" where product='swap_par' and currency=? and value is not null{legf}) t where rn=1", [ccy]).fetchall()
    fwd = con.execute(
        f"select expiry, value, date from ("
        f" select expiry, value, date, row_number() over (partition by expiry order by date desc) rn"
        f" from read_parquet('{g}', hive_partitioning=true)"
        f" where product='forward_swap' and currency=? and tenor='1Y'"
        f" and regexp_matches(expiry, '^[0-9]+Y$') and value is not null{legf}) t where rn=1", [ccy]).fetchall()
    con.close()
    quotes = [{"tenor": t, "kind": "swap", "rate": float(v), "ts": str(d)[:10]} for t, v, d in par]
    forwards = [{"expiry": e, "tenor": "1Y", "rate": float(v), "ts": str(d)[:10]} for e, v, d in fwd]
    if not quotes:
        return {"error": f"no swap_par rows for ccy '{ccy}' in the store"}
    asof = max([q["ts"] for q in quotes] + [f["ts"] for f in forwards])
    return {"ccy": ccy, "asof": asof,
            "source": "historical store EOD (latest date per series; percent units, same as the stream)",
            "quotes": quotes, "forwards": forwards}


def backtest(expiry, tail, notional, interval, lookback, start=None, end=None):
    dates, vol_bp, rate_pct = load_series(expiry, tail)
    if not dates:
        return {"error": f"no data for {expiry}x{tail} (checked vol+PAR in {PARQUET})"}
    if start:                                  # explicit window overrides the lookback preset
        lo = date.fromisoformat(start)
        hi = date.fromisoformat(end) if end else dates[-1]
        keep = [i for i, d in enumerate(dates) if lo <= d <= hi]
    else:
        yrs = 5 if str(lookback).startswith("5") else 1
        cutoff = dates[-1] - timedelta(days=int(yrs * 365.25) + 7)
        keep = [i for i, d in enumerate(dates) if d >= cutoff]
    if len(keep) < 2:
        return {"error": f"window {start or lookback}..{end or 'last'} has <2 data points in range"}
    dates = [dates[i] for i in keep]; vol_bp = [vol_bp[i] for i in keep]; rate_pct = [rate_pct[i] for i in keep]
    return run_backtest(dates, vol_bp, rate_pct, expiry, tail, notional, interval)


# ============================================================================
# HTTP server (loopback + CORS + port fallback)
# ============================================================================
def serve(http_port=8197):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        def _hdr(self, code):
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store"); self.end_headers()
        def do_OPTIONS(self): self._hdr(204)
        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            path = u.path.rstrip("/")
            if path not in ("/backtest", "/health", "/eodcurve"):
                self._hdr(404); self.wfile.write(json.dumps({"error": "not found"}).encode()); return
            if path == "/health":
                self._hdr(200); self.wfile.write(json.dumps({"ok": True}).encode()); return
            if path == "/eodcurve":
                try:
                    out = eod_curve((q.get("ccy", ["USD"])[0]).strip().upper() or "USD",
                                    (q.get("leg", [None])[0] or None))
                    self._hdr(200 if "error" not in out else 500)
                    self.wfile.write(json.dumps(out).encode())
                except Exception as e:
                    self._hdr(500); self.wfile.write(json.dumps({"error": str(e)}).encode())
                return
            try:
                g = lambda k, d: (q.get(k, [d])[0])
                out = backtest(g("expiry", "1M").upper(), g("tail", "10Y").upper(),
                               float(g("notional", "10000000")), g("interval", "weekly").lower(),
                               g("lookback", "1y").lower(), g("start", None), g("end", None))
                self._hdr(200 if "error" not in out else 500)
                self.wfile.write(json.dumps(out).encode())
            except Exception as e:
                self._hdr(500); self.wfile.write(json.dumps({"error": str(e)}).encode())
        def log_message(self, *a): pass

    httpd = None
    for cand in [http_port, 8797, 8996, 9331, 0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), H); break
        except OSError as e:
            print(f"[backtest] port {cand} unavailable ({e}); trying next...")
    if httpd is None:
        raise SystemExit("[backtest] could not bind any port.")
    bound = httpd.server_address[1]
    print(f"[backtest] http://localhost:{bound}/backtest?expiry=1M&tail=10Y&lookback=1y")
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Historical short-straddle backtest server")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--http-port", type=int, default=8197)
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest or not args.serve:
        # synthetic 2y of business days; vol flat 100bp, rate random-walk around 4%
        import random
        random.seed(1)
        d0 = date(2024, 1, 1); ds = []
        d = d0
        while d < date(2026, 1, 1):
            if d.weekday() < 5: ds.append(d)
            d += timedelta(days=1)
        vol = [100.0] * len(ds)
        r = 4.0; rr = []
        for _ in ds:
            r += random.gauss(0, 0.03); rr.append(r)
        out = run_backtest(ds, vol, rr, "1M", "10Y", 10_000_000, "weekly")
        assert straddle_rate(0.03, 0.03, 0.0, 0.01) == 0.0
        assert abs(annuity(0.03, 10) - 8.530) < 0.01, annuity(0.03, 10)
        # ATM premium sanity: 0.7979*0.01*sqrt(1/12)*A*N
        prem = STRADDLE_K * 0.01 * math.sqrt(1/12) * annuity(0.04, 10) * 10_000_000
        assert 100_000 < prem < 300_000, prem
        assert out["meta"]["n_sales"] > 40, out["meta"]["n_sales"]
        assert len(out["cum"]) == len(ds)
        assert out["hairs"] and all(h[0][1] == 0 or abs(h[0][1]) < 5 for h in out["hairs"])  # PnL~0 at inception
        print("backtest_server self-test passed:",
              "n_sales", out["meta"]["n_sales"], "| sharpe", out["sharpe"],
              "| total_pnl", out["summary"]["total_pnl"], "| hit", out["summary"]["hit_rate"])
    if args.serve:
        serve(args.http_port)
