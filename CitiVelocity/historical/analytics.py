"""
analytics.py -- rates RV engine over the DuckDB store.

Derivations on top of the raw swap_par / forward_swap / swap_spread data:
  * swap_curve / slope / butterfly      (spot PAR curve)
  * forward_curve / forward_ladder / fwd_point   (quoted Citi forwards)
  * swap_spread
  * butterfly weightings: equal (default), pca, regression

Conventions
-----------
* Levels are in PERCENT (as stored). Slopes/flies are returned in BASIS POINTS.
* Butterfly sign: positive = belly cheap (high yield) vs wings:  w.[a,b,c] with
  equal weights (-1, +2, -1).
* Currency basis is auto: RFR (OIS) where it exists (USD/GBP/CAD/JPY), else IBOR
  (SWAP_LIBOR) for EUR/AUD. JPY uses the primary TONAR curve (clearing variants
  JSCC/LCH are excluded from curve math; query them explicitly if needed).
* PCA / regression weights are FULL-SAMPLE by default (in-sample -> mild lookahead;
  fine for current-state RV, flag it for backtests). Pass window=N for trailing.

Usage
-----
    import analytics as A
    con = A.connect(A.load_config())
    A.slope(con, "USD", "2Y", "10Y").tail()
    A.butterfly(con, "USD", "5Y", "10Y", "30Y", weighting="pca").tail()
    A.forward_curve(con, "USD", "5Y").tail()       # 5y-forward across tails
"""
from __future__ import annotations
import re
import numpy as np
import pandas as pd

RFR_INDEX = {"USD": "SOFR", "GBP": "SONIA", "CAD": "CORRA", "JPY": "TONAR",
             "EUR": "EUROSTR", "AUD": "AONIA"}


def load_config(path="config.yaml"):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


def connect(config):
    import duckdb
    return duckdb.connect(config["storage"]["duckdb"])


# --- tenor helpers ---------------------------------------------------------

def ten_years(t: str) -> float:
    m = re.match(r"(\d+(?:\.\d+)?)([DWMY])", str(t).upper())
    if not m:
        return float("inf")
    n, u = float(m.group(1)), m.group(2)
    return n * {"D": 1/365, "W": 7/365, "M": 1/12, "Y": 1.0}[u]


def _order(tenors):
    return sorted(tenors, key=ten_years)


# --- basis resolution ------------------------------------------------------

def _has(con, ccy, basis):
    q = "SELECT count(*) FROM swap_par WHERE currency=? AND basis=?"
    return con.execute(q, [ccy, basis]).fetchone()[0] > 0


def _resolve_basis(con, ccy, basis):
    if basis != "auto":
        return basis
    return "RFR" if _has(con, ccy, "RFR") else "IBOR"


# --- spot curve ------------------------------------------------------------

def swap_curve(con, ccy, basis="auto") -> pd.DataFrame:
    """Wide date x tenor PAR levels (%). RFR uses the primary index (no clearing
    variants); IBOR uses the SWAP_LIBOR curve."""
    b = _resolve_basis(con, ccy, basis)
    # exclude clearing-variant index_names (those contain '_', e.g. TONAR_JSCC)
    df = con.execute("""
        SELECT date, tenor, value FROM swap_par
        WHERE currency=? AND basis=? AND (index_name IS NULL OR index_name NOT LIKE '%\\_%' ESCAPE '\\')
    """, [ccy, b]).df()
    if df.empty:
        return df
    w = df.pivot_table(index="date", columns="tenor", values="value")
    return w[[t for t in _order(w.columns)]].sort_index()


def slope(con, ccy, short, long, basis="auto") -> pd.Series:
    """Curve slope long-short in bp."""
    w = swap_curve(con, ccy, basis)
    s = (w[long] - w[short]) * 100.0
    s.name = f"{ccy} {short}{long}"
    return s.dropna()


# --- butterfly weightings --------------------------------------------------

def _equal(df3):
    a, b, c = df3.iloc[:, 0], df3.iloc[:, 1], df3.iloc[:, 2]
    return (2 * b - a - c) * 100.0


def _pca_weights(df3, window=None):
    """Wing weights that neutralize the level (PC1) and slope (PC2) loadings of the
    triplet, belly fixed at +2 (so it matches the equal-weight -1/+2/-1 scale).
    On a parallel/linear curve this reduces exactly to (-1, +2, -1)."""
    chg = df3.diff().dropna()
    if window:
        chg = chg.iloc[-window:]
    C = np.cov(chg.values.T)
    vals, vecs = np.linalg.eigh(C)     # ascending
    L2 = vecs[:, -2]                   # slope (PC2) loadings
    # belly fixed at +2; wings solve: sum-to-zero (parallel-neutral) AND PC2-neutral
    M = np.array([[1.0, 1.0], [L2[0], L2[2]]])
    rhs = np.array([-2.0, -2.0 * L2[1]])
    wa, wc = np.linalg.solve(M, rhs)
    return np.array([wa, 2.0, wc])


def _pca(df3, window=None):
    w = _pca_weights(df3, window)
    return (df3.values @ w) * 100.0


def _regression(df3, window=None):
    """Belly residual vs wings (OLS b ~ a + c + const). Residual in bp."""
    a, b, c = df3.iloc[:, 0].values, df3.iloc[:, 1].values, df3.iloc[:, 2].values
    X = np.column_stack([np.ones_like(a), a, c])
    if window:  # trailing-window betas (no lookahead)
        out = np.full(len(b), np.nan)
        for i in range(window, len(b) + 1):
            sl = slice(i - window, i)
            beta, *_ = np.linalg.lstsq(X[sl], b[sl], rcond=None)
            out[i - 1] = b[i - 1] - X[i - 1] @ beta
        return out * 100.0
    beta, *_ = np.linalg.lstsq(X, b, rcond=None)   # full-sample
    return (b - X @ beta) * 100.0


def butterfly(con, ccy, a, b, c, weighting="equal", basis="auto", window=None) -> pd.Series:
    """Butterfly a-b-c in bp. weighting in {equal, pca, regression}."""
    w = swap_curve(con, ccy, basis)[[a, b, c]].dropna()
    if weighting == "equal":
        vals = _equal(w)
    elif weighting == "pca":
        vals = pd.Series(_pca(w, window), index=w.index)
    elif weighting == "regression":
        vals = pd.Series(_regression(w, window), index=w.index)
    else:
        raise ValueError("weighting must be equal|pca|regression")
    out = pd.Series(vals, index=w.index, name=f"{ccy} {a}{b}{c} {weighting}")
    return out.dropna()


# --- forwards (quoted) -----------------------------------------------------

def forward_grid(con, ccy) -> pd.DataFrame:
    """Long: date, start(expiry), tail(tenor), value(%) for RFR forwards."""
    b = _resolve_basis(con, ccy, "auto")
    return con.execute("""
        SELECT date, expiry AS start, tenor AS tail, value FROM forward_swap
        WHERE currency=? AND basis=? AND (index_name IS NULL OR index_name NOT LIKE '%\\_%' ESCAPE '\\')
    """, [ccy, b]).df()


def forward_curve(con, ccy, start) -> pd.DataFrame:
    """For a fixed forward-start, wide date x tail (%). e.g. start='5Y' -> 5y-fwd curve."""
    g = forward_grid(con, ccy)
    g = g[g["start"] == start]
    w = g.pivot_table(index="date", columns="tail", values="value")
    return w[[t for t in _order(w.columns)]].sort_index()


def forward_ladder(con, ccy, tail) -> pd.DataFrame:
    """For a fixed tail, wide date x start (%). e.g. tail='1Y' -> ladder of n-fwd-1y."""
    g = forward_grid(con, ccy)
    g = g[g["tail"] == tail]
    w = g.pivot_table(index="date", columns="start", values="value")
    return w[[t for t in _order(w.columns)]].sort_index()


def fwd_point(con, ccy, start, tail) -> pd.Series:
    g = forward_grid(con, ccy)
    s = g[(g["start"] == start) & (g["tail"] == tail)].set_index("date")["value"].sort_index()
    s.name = f"{ccy} {start}{tail} fwd"
    return s


# --- swap spreads ----------------------------------------------------------

def swap_spread(con, ccy) -> pd.DataFrame:
    """Wide date x tenor swap spreads (bp, as published)."""
    df = con.execute("SELECT date, tenor, value FROM rates WHERE product='swap_spread' AND currency=?",
                     [ccy]).df()
    if df.empty:
        return df
    w = df.pivot_table(index="date", columns="tenor", values="value")
    return w[[t for t in _order(w.columns)]].sort_index()


# --- preset materialization (refreshed by the pipeline's build_duckdb) ------

SLOPE_PRESETS = [("2Y", "5Y"), ("2Y", "10Y"), ("5Y", "10Y"),
                 ("5Y", "30Y"), ("10Y", "30Y"), ("2Y", "30Y")]
FLY_PRESETS = [("2Y", "5Y", "10Y"), ("5Y", "10Y", "30Y"),
               ("2Y", "5Y", "30Y"), ("2Y", "10Y", "30Y")]
FWD_PRESETS = [("1Y", "1Y"), ("2Y", "2Y"), ("5Y", "5Y"), ("1Y", "10Y"),
               ("5Y", "10Y"), ("10Y", "10Y"), ("2Y", "10Y"), ("3M", "2Y")]
CCYS = ["USD", "EUR", "GBP", "AUD", "JPY", "CAD"]


def build_analytics_views(config) -> None:
    """Materialize tidy `rv_presets` (standard slopes / equal flies / fwd outrights /
    swap spreads). Reads each product ONCE (slow mount -> minimize globs), computes
    in pandas, writes the table. Called by the pipeline after each ingest."""
    con = connect(config)
    sp = con.execute("SELECT date,currency,basis,index_name,tenor,value FROM swap_par").df()
    fw = con.execute("SELECT date,currency,basis,index_name,expiry AS start,tenor AS tail,value "
                     "FROM forward_swap").df()
    ssp = con.execute("SELECT date,currency,tenor,value FROM rates WHERE product='swap_spread'").df()
    rows = []

    def add(ccy, signal, kind, unit, series):
        if series is None or len(series) == 0:
            return
        d = series.dropna().reset_index()
        d.columns = ["date", "value"]
        d["currency"], d["signal"], d["kind"], d["unit"] = ccy, signal, kind, unit
        rows.append(d)

    def curve_wide(ccy):
        b = "RFR" if ((sp.currency == ccy) & (sp.basis == "RFR")).any() else "IBOR"
        d = sp[(sp.currency == ccy) & (sp.basis == b)]
        d = d[d.index_name.isna() | ~d.index_name.astype("string").str.contains("_", na=False)]
        if d.empty:
            return pd.DataFrame()
        return d.pivot_table(index="date", columns="tenor", values="value").sort_index()

    for ccy in CCYS:
        curve = curve_wide(ccy)
        have = set(curve.columns) if len(curve) else set()
        for s, l in SLOPE_PRESETS:
            if {s, l} <= have:
                add(ccy, f"{s}{l}", "slope", "bp", ((curve[l] - curve[s]) * 100).dropna())
        for a, b, c in FLY_PRESETS:
            if {a, b, c} <= have:
                add(ccy, f"{a}{b}{c}", "fly_equal", "bp", _equal(curve[[a, b, c]].dropna()))
        fwc = fw[fw.currency == ccy]
        for st, tl in FWD_PRESETS:
            sub = fwc[(fwc["start"] == st) & (fwc["tail"] == tl)]
            if len(sub):
                add(ccy, f"{st}{tl}", "fwd_outright", "pct",
                    sub.set_index("date")["value"].sort_index())
        ssc = ssp[ssp.currency == ccy]
        for t, g in ssc.groupby("tenor"):
            add(ccy, t, "swap_spread", "bp", g.set_index("date")["value"].sort_index())

    if rows:
        allrows = pd.concat(rows, ignore_index=True)
        con.execute("DROP TABLE IF EXISTS rv_presets")
        con.execute("CREATE TABLE rv_presets AS SELECT * FROM allrows")
        n = con.execute("SELECT count(*) FROM rv_presets").fetchone()[0]
        print(f"rv_presets: {n:,} rows / {allrows['signal'].nunique()} signals / "
              f"{allrows['currency'].nunique()} ccys")
    con.close()


if __name__ == "__main__":
    build_analytics_views(load_config())
