#!/usr/bin/env python3
r"""
carry_recon.py — reconcile Citi's ROLL_CARRY.3M series (USD_SOFR) against
first-principles candidates computed from Citi's OWN par/forward grid, and
export a compact dataset for the engine-side batch-bootstrap comparison.

Run ON THE BOX (the sandbox mount serves roll_carry/forward_swap parquet
truncated):    py carry_recon.py            # reconciliation report
               py carry_recon.py --export   # + writes carry_recon_export.csv

WHAT IT ESTABLISHES (in order):
 1. Internal identity: TOTAL_CARRY ?= CARRY + ROLL (pure Citi data).
 2. Units/scale: are the series in bp or % (inferred from magnitudes).
 3. ROLL definition: compare Citi ROLL(e,t) against the STATIC-CURVE slide
    computed from Citi's own forward grid, F(e-3m,t) - F(e,t), at the (e,t)
    points where e-3m is ALSO on the grid (e in {6M,9M,1Y}: 3m-shifted expiry
    lands on {3M,6M,9M}). Sign convention falls out of the comparison.
 4. CARRY definition: candidates tested at the same points —
      (a) zero            (forwards-realize measure: fwd points don't carry)
      (b) F(e,t)-S(t)     (fwd-vs-spot pickup)
      (c) S(t)-F(3M,t-3m?) etc. — residual printed so an unlisted convention
                          is visible rather than force-fitted.
 5. Off-grid expiries (2Y+ where e-3m isn't quoted) are left to the ENGINE
    batch (needs curve interpolation): --export writes date x tag values of
    swap_par + forward_swap + roll_carry for N sample dates so the sandbox
    engine run can price the aged points exactly (Hagan-West + fwd chain,
    same interpolation as the live pricer).

Output: console table + carry_recon_export.csv (if --export).
"""
import argparse, csv, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
G = os.path.join(HERE, "data", "product={p}", "currency=USD", "year=*", "*.parquet").replace("\\", "/")

# fwd-grid expiries with e-3m also on the grid (exact, no interpolation needed)
ONGRID = [("6M", "3M"), ("9M", "6M"), ("1Y", "9M")]
TENORS = ["1Y", "2Y", "5Y", "10Y", "20Y"]


def q(con, sql):
    return con.execute(sql).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="write carry_recon_export.csv for the engine batch")
    ap.add_argument("--dates", type=int, default=24, help="number of sample dates (spread across history)")
    a = ap.parse_args()
    import duckdb
    con = duckdb.connect(":memory:")

    # sample dates: spread evenly + the 10 most recent
    ds = [r[0] for r in q(con, f"select distinct date from read_parquet('{G.format(p='roll_carry')}') order by date")]
    if not ds:
        sys.exit("no roll_carry rows — check the store")
    step = max(1, len(ds) // max(1, a.dates - 10))
    sample = sorted(set(ds[::step] + ds[-10:]))
    dlist = ",".join(f"'{d}'" for d in sample)
    print(f"[recon] {len(ds)} dates in store; sampling {len(sample)} ({sample[0]} .. {sample[-1]})\n")

    # load the three products for the sample dates into dicts keyed (date, tag-ish)
    # (leg = CARRY|ROLL|TOTAL_CARRY, expected as the tag suffix; if the parquet
    # schema differs, dump it so the query can be adapted rather than guessed)
    try:
        rc = {(str(r[0]), r[1], r[2], r[3]): r[4] for r in q(con,
            f"select date, expiry, tenor, regexp_extract(tag, '\\.([A-Z_]+)$', 1) leg, value "
            f"from read_parquet('{G.format(p='roll_carry')}') where date in ({dlist}) and value is not null")}
    except Exception as ex:
        print(f"[recon] roll_carry query failed ({str(ex)[:150]})\n[recon] schema is:")
        for row in q(con, f"describe select * from read_parquet('{G.format(p='roll_carry')}') limit 1"):
            print("   ", row)
        sys.exit("adapt the leg extraction to the schema above and re-run")
    fw = {(str(r[0]), r[1], r[2]): r[3] for r in q(con,
        f"select date, expiry, tenor, value from read_parquet('{G.format(p='forward_swap')}') "
        f"where date in ({dlist}) and value is not null")}
    sp = {(str(r[0]), r[1]): r[2] for r in q(con,
        f"select date, tenor, value from read_parquet('{G.format(p='swap_par')}') "
        f"where date in ({dlist}) and value is not null")}
    print(f"[recon] loaded: {len(rc)} roll_carry, {len(fw)} fwd, {len(sp)} par values")

    # ---- 1. TOTAL = CARRY + ROLL identity ----
    resid, n = 0.0, 0
    for (d, e, t, leg), v in rc.items():
        if leg != "TOTAL_CARRY":
            continue
        c, r = rc.get((d, e, t, "CARRY")), rc.get((d, e, t, "ROLL"))
        if c is None or r is None:
            continue
        resid = max(resid, abs(v - (c + r))); n += 1
    print(f"\n1) TOTAL_CARRY == CARRY + ROLL : {n} triplets, max |residual| = {resid:.6g}"
          f"  -> {'HOLDS' if resid < 1e-6 else 'check units/definition'}")

    # ---- 2. magnitude/units ----
    vals = [abs(v) for (d, e, t, leg), v in rc.items() if leg == "ROLL"]
    vals.sort()
    med = vals[len(vals)//2] if vals else float("nan")
    print(f"2) median |ROLL| = {med:.4g}  -> looks like {'bp' if med > 0.2 else '% (decimal-ish)'} "
          f"(static 3m slide is typically 1-15bp)")

    # ---- 3/4. on-grid candidates ----
    print(f"\n3) ROLL vs static slide F(e-3m,t)-F(e,t), and CARRY candidates (last sampled date + averages):")
    hdr = f"{'e':>4} {'t':>4} | {'citiROLL':>9} {'static':>9} {'d(bp)':>7} | {'citiCARRY':>9} {'F-S':>9} {'resid':>7}"
    print(hdr); print("-" * len(hdr))
    agg = {}
    for d in sample:
        for e, em in ONGRID:
            for t in TENORS:
                cr, cc = rc.get((d, e, t, "ROLL")), rc.get((d, e, t, "CARRY"))
                f0, fm, s0 = fw.get((d, e, t)), fw.get((d, em, t)), sp.get((d, t))
                if None in (cr, cc, f0, fm, s0):
                    continue
                static = (fm - f0) * 100.0          # % -> bp
                fvs = (f0 - s0) * 100.0             # fwd-vs-spot pickup, bp
                k = (e, t)
                A = agg.setdefault(k, {"n": 0, "dr": 0.0, "dr2": 0.0, "dcA": 0.0, "dc0": 0.0, "last": None})
                A["n"] += 1
                A["dr"] += cr - static              # if ~0: ROLL is static slide in bp, same sign
                A["dr2"] += cr + static             # if ~0: static slide with OPPOSITE sign
                A["dcA"] += cc - fvs                # if ~0: CARRY = fwd-vs-spot
                A["dc0"] += cc                      # if ~0: CARRY = 0 for fwd points (fwds-realize)
                A["last"] = (cr, static, cc, fvs)
    for (e, t), A in sorted(agg.items()):
        cr, static, cc, fvs = A["last"]
        print(f"{e:>4} {t:>4} | {cr:9.3f} {static:9.3f} {cr-static:7.3f} | {cc:9.3f} {fvs:9.3f} {cc-fvs:7.3f}"
              f"   avg[R-static]={A['dr']/A['n']:+.3f} avg[R+static]={A['dr2']/A['n']:+.3f}"
              f" avg[C-(F-S)]={A['dcA']/A['n']:+.3f} avg[C]={A['dc0']/A['n']:+.3f}")
    print("\nREAD-OFF: whichever avg column pins near zero across rows is the convention.")
    print("Off-grid expiries (2Y+) need the engine batch -> run with --export and hand the csv back.")

    # ---- 5. export for the engine-side batch ----
    if a.export:
        out = os.path.join(HERE, "carry_recon_export.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["date", "product", "expiry", "tenor", "leg", "value"])
            for (d, t), v in sorted(sp.items()):
                w.writerow([d, "swap_par", "", t, "", v])
            for (d, e, t), v in sorted(fw.items()):
                w.writerow([d, "forward_swap", e, t, "", v])
            for (d, e, t, leg), v in sorted(rc.items()):
                w.writerow([d, "roll_carry", e, t, leg, v])
        print(f"\n[recon] wrote {out} ({len(sp)+len(fw)+len(rc)} rows, {len(sample)} dates)")


if __name__ == "__main__":
    main()
