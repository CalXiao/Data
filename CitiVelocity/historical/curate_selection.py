"""
curate_selection.py -- apply Calvin's curation spec to the discovery inventory and
write a trimmed inventory (+ summary). Pure filtering of the local inventory; no API.

    python curate_selection.py
    -> discovery/rates_selection_inventory.parquet / .csv
"""
from __future__ import annotations
import argparse, os
import pandas as pd

# ---- index survivors (OIS / SWAP_LIBOR) ----
OIS_KEEP = {"USD_SOFR", "GBP_SONIA", "CAD_CORRA", "JPY_TONAR", "JPY_TONAR_JSCC", "JPY_TONAR_LCH",
            "EUR_EUROSTR", "AUD_AONIA"}   # ESTR/AONIA added: clean RFR curves for EUR/AUD (were IBOR-only)
LIBOR_KEEP = {"EUR", "GBP", "AUD"}

# ---- PAR spot tenors ----
PAR_TENORS = {"6M","1Y","2Y","3Y","4Y","5Y","6Y","7Y","8Y","9Y","10Y","12Y","15Y","20Y","25Y","30Y","40Y"}

# ---- forward grid: tail -> list of starts  (tag is FWD.<start>.<tail>) ----
FWD_GRID = {
    "3M":  ["3M","6M","1Y"],
    "6M":  ["3M","6M","9M","1Y","18M"],
    "1Y":  ["3M","6M","9M","1Y","18M","2Y","3Y","4Y","5Y","6Y","7Y","8Y","9Y","10Y",
            "11Y","12Y","13Y","14Y","15Y","16Y","17Y","18Y","19Y","20Y",
            "25Y","30Y","35Y","40Y","45Y","50Y"],  # full 1y-fwd chain out to 50y1y (entitlement max)
    "2Y":  ["3M","6M","1Y","2Y","3Y","4Y","5Y","6Y","7Y","8Y","9Y","10Y","15Y"],
    "3Y":  ["3M","6M","1Y","2Y","3Y","4Y","5Y","6Y","7Y","10Y","12Y","17Y"],
    "4Y":  ["3M","6M","1Y","2Y","3Y","4Y","5Y","6Y","11Y","16Y"],
    "5Y":  ["3M","6M","1Y","2Y","3Y","4Y","5Y","7Y","10Y","15Y","20Y","25Y","30Y"],
    "7Y":  ["3M","6M","1Y","2Y","3Y","5Y","7Y","8Y","13Y"],
    "10Y": ["3M","6M","1Y","2Y","5Y","10Y","15Y","20Y","25Y","30Y"],
    "15Y": ["3M","1Y","5Y","10Y","15Y","20Y"],
    "20Y": ["3M","1Y","5Y","10Y","15Y","20Y"],
}
FWD_PAIRS = {(s, t) for t, starts in FWD_GRID.items() for s in starts}

# ---- swap spread tenors ----
SS_TENORS = {"1Y","2Y","3Y","5Y","7Y","10Y","20Y","30Y"}

# ---- vol grids ----
VOL_EXP  = {"1M","2M","3M","6M","9M","1Y","18M","2Y","3Y","4Y","5Y","7Y","10Y","15Y","20Y","30Y"}
VOL_TAIL = {"1Y","2Y","3Y","5Y","7Y","10Y","15Y","20Y","30Y"}

# ---- inflation swaps: RATES.INFLATION.SWAP.<CCY_INDEX>.<TENOR> ----
INF_SWAP_INDEX = {"USD_CPURNSA", "EUR_CPTFEMU", "GBP_UKRPI", "AUD_AUCPI", "JPY_JCPNGENF"}
INF_SWAP_TENORS = {"1Y","2Y","3Y","4Y","5Y","7Y","10Y","12Y","15Y","20Y","25Y","30Y"}

# ---- tenor basis swaps: RATES.BASIS_SWAPS.<PRODUCT>.<CCY>.<TENOR> ----
BASIS_PRODUCTS = {"3S6S_BASIS", "3S1S_BASIS"}
BASIS_CCYS = {"USD", "EUR", "GBP", "AUD"}
BASIS_TENORS = {"1Y","2Y","3Y","4Y","5Y","6Y","7Y","8Y","9Y","10Y","12Y","15Y","20Y","25Y","30Y"}

# ---- xccy par swap vs USD: RATES.XCCY_SWAP.<CCY>.USD.PAR.<TENOR> ----
XCCY_CCYS = {"JPY", "AUD", "GBP", "EUR", "CAD"}
XCCY_TENORS = {"1Y","2Y","3Y","5Y","7Y","10Y","12Y","15Y","20Y","25Y","30Y"}

# ---- OIS invoice spreads: RATES.OIS_INVOICESPREAD.<PRODUCT>.<TENOR> ----
IVSP_PRODUCTS = {"USD_SOFR_FRONTMONTH"}


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return column if it exists, else an empty string series (same index)."""
    return df[name] if name in df.columns else pd.Series("", index=df.index, dtype=str)


def curate(df: pd.DataFrame) -> pd.DataFrame:
    keep = pd.Series(False, index=df.index)

    is_ois = df.dataset == "RATES.OIS"
    is_lib = df.dataset == "RATES.SWAP_LIBOR"
    idx_ok = (is_ois & df.f2.isin(OIS_KEEP)) | (is_lib & df.f2.isin(LIBOR_KEEP))

    # swaps: PAR / FWD / ROLL_CARRY(3m carry) / SWAP_SPREAD  (drop BFLY, CURVES, ...)
    par = idx_ok & (df.f3 == "PAR") & df.f4.isin(PAR_TENORS)
    fwd = idx_ok & (df.f3 == "FWD") & df.apply(lambda r: (r.f4, r.f5) in FWD_PAIRS, axis=1)
    carry = (idx_ok & (df.f3 == "ROLL_CARRY") & (df.f4 == "3M") & _col(df, "f7").isin(["CARRY", "ROLL", "TOTAL_CARRY"])
             & df.apply(lambda r: (r.f5, r.f6) in FWD_PAIRS, axis=1))
    ss = idx_ok & (df.f3 == "SWAP_SPREAD") & df.f4.isin(SS_TENORS)
    keep |= par | fwd | carry | ss

    # vol: ATM/ATM_RFR, Annual BPVol (NORMAL.ANNUAL) + Forward Premium
    v = df.dataset == "RATES.VOL"
    vfam = v & df.f3.isin(["ATM", "ATM_RFR"])
    vbp = vfam & (df.f4 == "NORMAL") & (df.f5 == "ANNUAL") & df.f6.isin(VOL_EXP) & _col(df, "f7").isin(VOL_TAIL)
    vfp = vfam & (df.f4 == "FWDPREMIUM") & df.f5.isin(VOL_EXP) & df.f6.isin(VOL_TAIL)
    keep |= vbp | vfp

    # midcurves: OPT_STR + VOL (all tenors/tails)
    mc = (df.dataset == "RATES.MIDCURVES") & (df.f3 == "OPT_STR") & (df.f4 == "VOL")
    keep |= mc

    # treasuries: all
    keep |= (df.dataset == "RATES.TSY")

    # inflation swaps: RATES.INFLATION.SWAP.<CCY_INDEX>.<TENOR>
    inf = ((df.dataset == "RATES.INFLATION") & (df.f2 == "SWAP")
           & df.f3.isin(INF_SWAP_INDEX) & df.f4.isin(INF_SWAP_TENORS))
    keep |= inf

    # tenor basis swaps: RATES.BASIS_SWAPS.<3S6S|3S1S>_BASIS.<CCY>.<TENOR>
    basis = ((df.dataset == "RATES.BASIS_SWAPS") & df.f2.isin(BASIS_PRODUCTS)
             & df.f3.isin(BASIS_CCYS) & df.f4.isin(BASIS_TENORS))
    keep |= basis

    # xccy par swap vs USD: RATES.XCCY_SWAP.<CCY>.USD.PAR.<TENOR>
    xccy = ((df.dataset == "RATES.XCCY_SWAP") & df.f2.isin(XCCY_CCYS)
            & (df.f3 == "USD") & (df.f4 == "PAR") & _col(df, "f5").isin(XCCY_TENORS))
    keep |= xccy

    # OIS invoice spreads (SOFR front-month): RATES.OIS_INVOICESPREAD.<PRODUCT>.<TENOR>
    ivsp = ((df.dataset == "RATES.OIS_INVOICESPREAD") & df.f2.isin(IVSP_PRODUCTS))
    keep |= ivsp

    return df[keep].copy()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", default="discovery/rates_tag_inventory.csv")
    ap.add_argument("--out", default="discovery/rates_selection_inventory.csv")
    args = ap.parse_args(argv)
    if args.inventory.endswith(".parquet"):
        try:
            df = pd.read_parquet(args.inventory)
        except ImportError:
            df = pd.read_csv(args.inventory.replace(".parquet", ".csv"), low_memory=False)
    else:
        df = pd.read_csv(args.inventory, low_memory=False)
    sel = curate(df)
    out_csv = args.out if args.out.endswith(".csv") else args.out.replace(".parquet", ".csv")
    sel.to_csv(out_csv, index=False)
    if args.out.endswith(".parquet"):
        try:
            sel.to_parquet(args.out, index=False)
        except ImportError:
            pass  # pyarrow not available; CSV written above

    print(f"Trimmed {len(df):,} -> {len(sel):,} tags\n")
    # breakdown by dataset + product
    g = (sel.assign(product=sel.f3.fillna("(na)"))
            .groupby(["dataset", "product"]).size().reset_index(name="n")
            .sort_values(["dataset", "n"], ascending=[True, False]))
    print(g.to_string(index=False))
    print("\nby dataset/index:")
    gi = sel.groupby(["dataset", "f2"]).size().reset_index(name="n")
    print(gi.to_string(index=False))


if __name__ == "__main__":
    raise SystemExit(main())
