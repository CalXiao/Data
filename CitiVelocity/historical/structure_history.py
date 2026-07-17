#!/usr/bin/env python3
r"""
structure_history.py — build daily level histories + first-pass stats for the
structures.py universe from the historical store. RUN ON THE BOX (the sandbox
mount serves forward_swap parquet truncated; spot-only runs work anywhere).

    py structure_history.py --ccy USD
    py structure_history.py --ccy USD --spot-only     # sandbox-safe subset

Outputs (beside this script):
    structures_history_<CCY>.csv   date x structure level matrix (bp)
    console: per-structure stats — last, mean, sd, z (full-sample AND 2y),
             AR(1) half-life (full + 2y window), daily vol.

Stats caveats printed on purpose: z vs rolling/full mean is regime-naive
(see SESSION_HANDOFF 2026-07-08 discussion) — the PCA-residual version is the
real object, v2. Weights are conventional; PCA/regression weights v2.
"""
import argparse, csv, math, os, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from structures import build_universe, level  # noqa

G = os.path.join(HERE, "data", "product={p}", "currency={c}", "year=*", "*.parquet").replace("\\", "/")


def load(ccy, spot_only):
    """Per-year reads with skip-on-error: unreadable partitions (the sandbox
    mount truncates recently-rewritten parquet) are reported and skipped —
    on the box everything reads."""
    import duckdb, glob as _glob
    con = duckdb.connect(":memory:")
    def read(prod, cols):
        rows, bad = [], []
        pat = G.format(p=prod, c=ccy).replace("year=*", "year=YYYY")
        years = sorted({p.split("year=")[1].split("/")[0].split(os.sep)[0]
                        for p in _glob.glob(G.format(p=prod, c=ccy))})
        for y in years:
            try:
                rows += con.execute(f"select {cols} from read_parquet('{pat.replace('YYYY', y)}') "
                                    f"where value is not null").fetchall()
            except Exception:
                bad.append(y)
        if bad:
            print(f"[hist] WARN {prod}: skipped unreadable year(s) {','.join(bad)} (mount truncation; fine on the box)")
        return rows
    par_rows = read("swap_par", "date, tenor, value")
    fwd_rows = [] if spot_only else read("forward_swap", "date, expiry, tenor, value")
    par = defaultdict(dict)   # date -> {tenor: %}
    for d, t, v in par_rows:
        par[str(d)[:10]][t] = v
    fwd = defaultdict(dict)   # date -> {(expiry,tenor): %}
    for d, e, t, v in fwd_rows:
        fwd[str(d)[:10]][(e, t)] = v
    return par, fwd


def ar1_halflife(series):
    if len(series) < 60:
        return float("inf")
    x = series[:-1]; dx = [series[i + 1] - series[i] for i in range(len(series) - 1)]
    mx = sum(x) / len(x); mdx = sum(dx) / len(dx)
    cov = sum((a - mx) * (b - mdx) for a, b in zip(x, dx))
    var = sum((a - mx) ** 2 for a in x)
    if var <= 0:
        return float("inf")
    b = cov / var
    return math.log(2) / -b if b < 0 else float("inf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccy", default="USD")
    ap.add_argument("--spot-only", action="store_true", help="skip forward_swap (sandbox-safe)")
    a = ap.parse_args()
    par, fwd = load(a.ccy, a.spot_only)

    av_par = set().union(*[set(m) for m in par.values()]) if par else set()
    av_fwd = set().union(*[set(m) for m in fwd.values()]) if fwd else set()
    uni, dropped = build_universe(av_fwd, av_par)
    if a.spot_only:
        uni = {k: v for k, v in uni.items() if all(e is None for e, t, w in v)}
    print(f"[hist] {a.ccy}: {len(uni)} structures ({len(dropped)} dropped for missing grid points)")
    if dropped:
        print("[hist] dropped:", ", ".join(dropped[:15]) + (" ..." if len(dropped) > 15 else ""))

    dates = sorted(set(par) | set(fwd))
    series = {name: [] for name in uni}
    kept_dates = []
    for d in dates:
        row = {name: level(legs, par.get(d, {}), fwd.get(d, {})) for name, legs in uni.items()}
        if sum(v is not None for v in row.values()) < max(1, len(uni) // 2):
            continue   # skip sparse days (holidays / partial pulls)
        kept_dates.append(d)
        for name in uni:
            series[name].append(row[name])
    print(f"[hist] {len(kept_dates)} dates ({kept_dates[0]} .. {kept_dates[-1]})")

    out = os.path.join(HERE, f"structures_history_{a.ccy}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date"] + list(uni))
        for i, d in enumerate(kept_dates):
            w.writerow([d] + [series[n][i] if series[n][i] is not None else "" for n in uni])
    print(f"[hist] wrote {out}")

    hdr = f"{'structure':>22} {'last':>8} {'mean':>8} {'sd':>7} {'z-full':>7} {'z-2y':>6} {'HL':>6} {'HL-2y':>6} {'vol/d':>6}"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for name in uni:
        lv = [v for v in series[name] if v is not None]
        if len(lv) < 120:
            continue
        n = len(lv); mean = sum(lv) / n
        sd = (sum((x - mean) ** 2 for x in lv) / n) ** 0.5
        lv2 = lv[-504:]; m2 = sum(lv2) / len(lv2)
        s2 = (sum((x - m2) ** 2 for x in lv2) / len(lv2)) ** 0.5
        dv = [lv[i + 1] - lv[i] for i in range(n - 1)]
        vol = (sum(x * x for x in dv) / len(dv)) ** 0.5
        f_ = lambda h: f"{h:6.0f}" if h < 9999 else "   inf"
        print(f"{name:>22} {lv[-1]:8.1f} {mean:8.1f} {sd:7.1f} {(lv[-1]-mean)/sd if sd else 0:7.2f} "
              f"{(lv[-1]-m2)/s2 if s2 else 0:6.2f} {f_(ar1_halflife(lv))} {f_(ar1_halflife(lv2))} {vol:6.2f}")
    print("\nCAVEAT: z vs sample means (regime-naive); conventional weights. PCA-residual overlay = v2.")


if __name__ == "__main__":
    main()
