#!/usr/bin/env python3
r"""
rv_server.py — RV structure screen server for the pricer's RV tab (:8201).
Same bridge pattern as backtest_server.py: duckdb over the local parquet store,
loopback HTTP + CORS, lazy compute with a daily cache.

GET /rvscreen?ccy=USD[&refresh=1]
  -> { asof, ccy, computed_at, n_dates, rows: [ {structure, kind, last, d1,
       z2y, hl2y, vr20, d1s, c10, rz10, c210, rz210, crz, vol, carry3m,
       roll3m, total3m, carry_to_vol, gates: {z, hl, vr}, flag}, ... ] }

Definitions (reconciled 2026-07-08, see SESSION_HANDOFF):
  - levels: structures.py universe (conventional weights), bp.
  - z2y: (last - 2y-window mean)/2y sd — SAMPLE z, regime-naive by design;
    the PCA-residual z is v2 and gets its own column when built.
  - hl2y: AR(1) half-life on the 2y window (days).
  - vr20: Var(20d)/(20*Var(1d)) on the 2y window — <0.85 = anti-persistent.
  - carry3m/roll3m: weighted combos of Citi ROLL_CARRY.3M legs (CARRY≡0 on the
    fwd grid — all economics in ROLL, receiver-positive static slide, bp).
    Spot-pillar structures have no ROLL_CARRY tags -> null.
  - carry_to_vol: total3m / (vol * sqrt(63)) — slide per unit of 3m noise.
  - flag "verify": hl2y < 10d — long-end fast reverters need the sparse-node
    caution (recon p95 tail lives there).
Gates: |z2y|>1.25, hl2y<120, vr20<0.85.
"""
import argparse, json, math, os, threading, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, HERE)
from structures import build_universe, level  # noqa

G = os.path.join(HERE, "data", "product={p}", "currency={c}", "year=*", "*.parquet").replace("\\", "/")


def _hl(lv):
    if len(lv) < 120:
        return None
    x = lv[:-1]; dx = [lv[i + 1] - lv[i] for i in range(len(lv) - 1)]
    mx = sum(x) / len(x); mdx = sum(dx) / len(dx)
    var = sum((a - mx) ** 2 for a in x)
    if var <= 0:
        return None
    b = sum((a - mx) * (c - mdx) for a, c in zip(x, dx)) / var
    return math.log(2) / -b if b < 0 else None


def _vr(lv, k=20):
    if len(lv) < k + 60:
        return None
    d1 = [lv[i + 1] - lv[i] for i in range(len(lv) - 1)]
    dk = [lv[i + k] - lv[i] for i in range(len(lv) - k)]
    v1 = sum(x * x for x in d1) / len(d1)
    return (sum(x * x for x in dk) / len(dk)) / (k * v1) if v1 > 0 else None


# ---- directionality / residual richness (2026-07-13, dealer-sheet parity) ----
# Rolling-window OLS vs the 10Y outright (duration beta) and the 2s10s slope.
# The RESIDUAL z is the poor-man's PCA: what's left of the dislocation after
# the level/slope factor is stripped. Correlation is computed on DAILY CHANGES
# (levels correlation is spuriously high for cointegrated series).
REG_WIN = 63          # 3M, matching the reference sheet


def _corr_d(a, b):
    """Pearson corr of first differences of two aligned series."""
    if len(a) < 21:
        return None
    da = [a[i + 1] - a[i] for i in range(len(a) - 1)]
    db = [b[i + 1] - b[i] for i in range(len(b) - 1)]
    n = len(da)
    ma = sum(da) / n; mb = sum(db) / n
    va = sum((x - ma) ** 2 for x in da); vb = sum((x - mb) ** 2 for x in db)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(da, db))
    return cov / (va * vb) ** 0.5


def _resid_z(y, x):
    """OLS y = a + b*x over the window; z of the LAST residual vs the window's
    residual dispersion. Requires >=40 points; None if x is degenerate or the
    residual has no dispersion (beta explains everything -> no RV left)."""
    n = len(y)
    if n < 40:
        return None
    mx = sum(x) / n; my = sum(y) / n
    vx = sum((u - mx) ** 2 for u in x)
    if vx <= 0:
        return None
    b = sum((u - mx) * (v - my) for u, v in zip(x, y)) / vx
    a = my - b * mx
    res = [v - (a + b * u) for u, v in zip(x, y)]
    sr = (sum(r * r for r in res) / n) ** 0.5
    return res[-1] / sr if sr > 1e-9 else None


def _pair(series_vals, bench_vals, win=REG_WIN):
    """Most recent `win` dates where BOTH the structure and the benchmark have
    values; returns (ys, xs) chronological."""
    ys, xs = [], []
    for i in range(len(series_vals) - 1, -1, -1):
        if series_vals[i] is not None and bench_vals[i] is not None:
            ys.append(series_vals[i]); xs.append(bench_vals[i])
            if len(ys) == win:
                break
    return ys[::-1], xs[::-1]


def compute(ccy):
    import duckdb
    con = duckdb.connect(":memory:")
    par_rows = con.execute(f"select date, tenor, value from read_parquet('{G.format(p='swap_par', c=ccy)}') "
                           f"where value is not null").fetchall()
    fwd_rows = con.execute(f"select date, expiry, tenor, value from read_parquet('{G.format(p='forward_swap', c=ccy)}') "
                           f"where value is not null").fetchall()
    # latest Citi ROLL per (expiry, tenor) — CARRY is identically 0 on the fwd grid
    rc_rows = con.execute(
        f"select expiry, tenor, value from ("
        f" select expiry, tenor, value, tag, row_number() over (partition by tag order by date desc) rn"
        f" from read_parquet('{G.format(p='roll_carry', c=ccy)}')"
        f" where value is not null and tag like '%.ROLL') t where rn=1").fetchall()
    # full ROLL history (2y) for the carry-richness z (is the slide itself fat
    # or thin vs its own history — an independent signal from the level z)
    rc_hist_rows = con.execute(
        f"select date, expiry, tenor, value from read_parquet('{G.format(p='roll_carry', c=ccy)}')"
        f" where value is not null and tag like '%.ROLL'").fetchall()
    con.close()
    par = defaultdict(dict); fwd = defaultdict(dict)
    for d, t, v in par_rows: par[str(d)[:10]][t] = v
    for d, e, t, v in fwd_rows: fwd[str(d)[:10]][(e, t)] = v
    roll_leg = {(e, t): v for e, t, v in rc_rows}
    rch = defaultdict(dict)
    for d, e, t, v in rc_hist_rows: rch[str(d)[:10]][(e, t)] = v
    rch_dates = sorted(rch)[-504:]

    av_par = set().union(*[set(m) for m in par.values()]) if par else set()
    av_fwd = set().union(*[set(m) for m in fwd.values()]) if fwd else set()
    uni, _ = build_universe(av_fwd, av_par)

    dates = sorted(set(par) & set(fwd)) if fwd else sorted(par)
    series = {n: [] for n in uni}
    for d in dates:
        for n, legs in uni.items():
            series[n].append(level(legs, par.get(d, {}), fwd.get(d, {})))
    # benchmark series aligned to the same date grid (bp): 10Y level, 2s10s slope
    bench_L, bench_S = [], []
    for d in dates:
        p = par.get(d, {})
        l10, l2 = p.get("10Y"), p.get("2Y")
        bench_L.append(l10 * 100.0 if l10 is not None else None)
        bench_S.append((l10 - l2) * 100.0 if (l10 is not None and l2 is not None) else None)
    rows = []
    excluded = []
    for n, legs in uni.items():
        lv = [v for v in series[n] if v is not None]
        if len(lv) < 250:   # was 500 — surfaced as silent cuts (2026-07-08)
            excluded.append({"structure": n, "reason": f"history {len(lv)}d < 250d"})
            continue
        lv2 = lv[-504:]
        m2 = sum(lv2) / len(lv2)
        s2 = (sum((x - m2) ** 2 for x in lv2) / len(lv2)) ** 0.5
        if s2 < 1e-6:
            excluded.append({"structure": n, "reason": "zero variance"})
            continue
        d1 = [lv[i + 1] - lv[i] for i in range(len(lv) - 1)]
        vol = (sum(x * x for x in d1) / len(d1)) ** 0.5
        z = (lv[-1] - m2) / s2
        hl = _hl(lv2); vr = _vr(lv2)
        spot = all(e is None for e, t, w in legs)
        roll3m = None
        if not spot and all((e, t) in roll_leg for e, t, w in legs):
            roll3m = sum(w * roll_leg[(e, t)] for e, t, w in legs)
        total = roll3m  # CARRY == 0 on the fwd grid (reconciled); spot -> null

        # sigma-move: yesterday's EOD-to-EOD change in daily sigmas
        d1s = (lv[-1] - lv[-2]) / vol if vol > 0 else None
        # directionality vs 10Y level + 2s10s slope (63d): corr of changes,
        # z of the levels-OLS residual (level-/slope-hedged richness)
        yL, xL = _pair(series[n], bench_L)
        yS, xS = _pair(series[n], bench_S)
        c10, rz10 = _corr_d(yL, xL), _resid_z(yL, xL)
        c210, rz210 = _corr_d(yS, xS), _resid_z(yS, xS)
        # carry richness: latest structure ROLL vs its own 2y history
        crz = rlo2 = rhi2 = None
        if not spot and all((e, t) in roll_leg for e, t, w in legs):
            rs = []
            for d in rch_dates:
                m = rch[d]
                if all((e, t) in m for e, t, w in legs):
                    rs.append(sum(w * m[(e, t)] for e, t, w in legs))
            if len(rs) >= 120:
                rlo2, rhi2 = min(rs), max(rs)   # 2y ROLL range for the rich/cheap bar
                rm = sum(rs) / len(rs)
                rsd = (sum((x - rm) ** 2 for x in rs) / len(rs)) ** 0.5
                if rsd > 1e-9:
                    crz = (rs[-1] - rm) / rsd
        ctv = (total / (vol * math.sqrt(63))) if (total is not None and vol > 0) else None
        kind = ("spot" if spot else ("curve" if len(legs) == 2 else "fly"))
        rows.append({
            "structure": n, "kind": kind, "last": round(lv[-1], 1), "d1": round(lv[-1] - lv[-2], 2),
            # legs + 2y stats so the TAB can price LIVE off the app's calibrated
            # curve and compute a live z (2026-07-08: EOD 'last' was mistaken for live)
            "legs": [[e, t, w] for e, t, w in legs],
            "mean2y": round(m2, 2), "sd2y": round(s2, 3), "n_hist": len(lv),
            "lo2y": round(min(lv2), 1), "hi2y": round(max(lv2), 1),   # 2y level range (rich/cheap bar)
            # shorter-horizon level ranges for the selectable bar (3m/6m/1y)
            "lo3m": round(min(lv[-63:]), 1), "hi3m": round(max(lv[-63:]), 1),
            "lo6m": round(min(lv[-126:]), 1), "hi6m": round(max(lv[-126:]), 1),
            "lo1y": round(min(lv[-252:]), 1), "hi1y": round(max(lv[-252:]), 1),
            "rlo2y": round(rlo2, 2) if rlo2 is not None else None,
            "rhi2y": round(rhi2, 2) if rhi2 is not None else None,
            "z2y": round(z, 2), "hl2y": round(hl, 0) if hl else None, "vr20": round(vr, 2) if vr is not None else None,
            "d1s": round(d1s, 1) if d1s is not None else None,
            "c10": round(c10, 2) if c10 is not None else None,
            "rz10": round(rz10, 2) if rz10 is not None else None,
            "c210": round(c210, 2) if c210 is not None else None,
            "rz210": round(rz210, 2) if rz210 is not None else None,
            "crz": round(crz, 2) if crz is not None else None,
            "vol": round(vol, 2), "carry3m": 0.0 if roll3m is not None else None,
            "roll3m": round(roll3m, 2) if roll3m is not None else None,
            "total3m": round(total, 2) if total is not None else None,
            "carry_to_vol": round(ctv, 2) if ctv is not None else None,
            "gates": {"z": abs(z) > 1.25, "hl": bool(hl and hl < 120), "vr": bool(vr is not None and vr < 0.85)},
            "flag": "verify" if (hl and hl < 10) else "",
        })
    rows.sort(key=lambda r: -(abs(r["z2y"]) * (2 if all(r["gates"].values()) else 1)))
    return {"asof": dates[-1] if dates else None, "ccy": ccy, "n_dates": len(dates),
            "n_universe": len(uni), "excluded": excluded,
            "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "z2y is SAMPLE z (regime-naive). rz10/rz210 = 63d levels-OLS residual z vs 10Y / 2s10s "
                    "(level-/slope-hedged richness, poor-man's PCA); c10/c210 = 63d corr of daily changes; "
                    "crz = latest 3M ROLL vs own 2y history (sigmas); d1s = yesterday's move / daily vol. "
                    "carry3m==0 on fwd grid by Citi convention.",
            "rows": rows}


def serve(http_port=8201):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    lock = threading.Lock()
    cache = {}   # ccy -> {payload, day}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            self.wfile.write(json.dumps(obj).encode())
        def do_OPTIONS(self): self._send(204, {})
        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            if u.path.rstrip("/") == "/health":
                return self._send(200, {"ok": True})
            if u.path.rstrip("/") != "/rvscreen":
                return self._send(404, {"error": "not found"})
            ccy = (q.get("ccy", ["USD"])[0]).strip().upper() or "USD"
            force = "refresh" in u.query or "force" in u.query
            today = time.strftime("%Y-%m-%d")
            with lock:
                ent = cache.get(ccy)
                if force or ent is None or ent["day"] != today:
                    try:
                        cache[ccy] = {"payload": compute(ccy), "day": today}
                    except Exception as e:
                        if ent is None:
                            return self._send(500, {"error": str(e), "rows": []})
                return self._send(200, cache[ccy]["payload"])
        def log_message(self, *a): pass

    httpd = None
    for cand in [http_port, 8802, 8997, 9333, 0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), H); break
        except OSError as e:
            print(f"[rv] port {cand} unavailable ({e}); trying next...")
    if httpd is None:
        raise SystemExit("[rv] could not bind any port.")
    print(f"[rv] http://localhost:{httpd.server_address[1]}/rvscreen?ccy=USD")
    httpd.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="RV structure screen server")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--http-port", type=int, default=8201)
    p.add_argument("--once", action="store_true", help="compute USD once, print summary, exit")
    a = p.parse_args()
    if a.once:
        out = compute("USD")
        print(f"{out['asof']} rows={len(out['rows'])}")
        for r in out["rows"][:10]:
            print(r["structure"], r["z2y"], r["hl2y"], r["vr20"], r["roll3m"], r["carry_to_vol"])
    elif a.serve:
        serve(a.http_port)
    else:
        print("use --serve or --once")
