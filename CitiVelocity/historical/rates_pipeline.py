"""
rates_pipeline.py -- backfill + daily incremental ingest for Citi Velocity rates.

Pipeline:
  config.yaml + discovery inventory  ->  select tags (rule-based)
  -> pull via CitiVelocityClient (DAILY) -> normalize to long rows
  -> partitioned Parquet (product/currency/year) with lookback UPSERT
  -> DuckDB views over the Parquet glob

Commands:
  python rates_pipeline.py select                 # preview the curated tag universe
  python rates_pipeline.py backfill               # one-time full history
  python rates_pipeline.py daily                  # incremental lookback-upsert (cron)
  python rates_pipeline.py duckdb                  # (re)create DuckDB views
  python rates_pipeline.py select --mock           # offline smoke test

Storage is the source of truth (Parquet); DuckDB holds only views.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import logging
import os
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger("pipeline")

LONG_COLS = ["date", "tag", "value", "field", "currency", "product", "basis",
             "index_name", "expiry", "tenor", "moneyness_bp", "vol_type",
             "measure", "source_freq", "ingested_at"]

KNOWN_CCYS = {"USD", "EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "SEK", "NOK"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


def _split_ccy_index(token: str):
    """'USD_SOFR' -> ('USD','SOFR'); 'EUR' -> ('EUR', None); 'JPY_TONAR_LCH' -> ('JPY','TONAR_LCH')."""
    KNOWN = {"USD", "EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "SEK", "NOK"}
    u = token.upper()
    if u in KNOWN:
        return u, None
    if "_" in u and u.split("_", 1)[0] in KNOWN:
        head, rest = u.split("_", 1)
        return head, rest
    return None, None


# ---------------------------------------------------------------------------
# Tag selection (rule-based, from the discovery inventory)
# ---------------------------------------------------------------------------

def select_tags(inventory: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Return a DataFrame of selected tags + normalized dimensions, per config rules."""
    inv = inventory.copy()
    parts: List[pd.DataFrame] = []
    prods = config["products"]

    def _flt(series_vals, allowed):
        return series_vals.isin(allowed) if allowed else pd.Series(True, index=series_vals.index)

    # --- swap PAR: RATES.OIS.<CCY_INDEX>.PAR.<tenor> ---
    if "swap_par" in prods:
        c = prods["swap_par"]
        d = inv[(inv.dataset == c["dataset"]) & (inv.f3 == c["product_field"])
                & (inv.f2.isin(c["indices"]))].copy()
        d = d[_flt(d.f4, c.get("tenors"))]
        d["product"], d["expiry"], d["tenor"] = "swap_par", None, d["f4"]
        d["vol_type"], d["moneyness_bp"] = None, None
        parts.append(d)

    # --- forward swap: RATES.OIS.<CCY_INDEX>.FWD.<expiry>.<tenor> ---
    if "forward_swap" in prods:
        c = prods["forward_swap"]
        d = inv[(inv.dataset == c["dataset"]) & (inv.f3 == c["product_field"])
                & (inv.f2.isin(c["indices"]))].copy()
        d = d[_flt(d.f4, c.get("expiries")) & _flt(d.f5, c.get("tenors"))]
        d["product"], d["expiry"], d["tenor"] = "forward_swap", d["f4"], d["f5"]
        d["vol_type"], d["moneyness_bp"] = None, None
        parts.append(d)

    # --- vol ATM NORMAL: RATES.VOL.<CCY>.<ATM|ATM_RFR>.NORMAL.ANNUAL.<expiry>.<tenor> ---
    if "vol" in prods:
        c = prods["vol"]
        d = inv[(inv.dataset == c["dataset"]) & (inv.f3.isin(c["families"]))
                & (inv.ccy.isin(c["currencies"]))
                & (inv.tag.str.contains(f".{c['quote']}."))].copy()
        # expiry/tenor are the last two dotted fields
        last2 = d["tag"].str.rsplit(".", n=2, expand=True)
        d["expiry"], d["tenor"] = last2[1], last2[2]
        d = d[_flt(d.expiry, c.get("expiries")) & _flt(d.tenor, c.get("tenors"))]
        d["product"], d["vol_type"], d["moneyness_bp"] = "vol", c["quote"], 0
        parts.append(d)

    if not parts:
        return pd.DataFrame()
    sel = pd.concat(parts, ignore_index=True)

    # normalize currency / index / basis from f2 (swaps) or ccy (vol)
    ci = sel["f2"].fillna("").map(_split_ccy_index)
    sel["currency"] = [c if c else cc for (c, _), cc in zip(ci, sel["ccy"].fillna(""))]
    sel["index_name"] = [idx for _, idx in ci]
    sel.loc[sel["product"] == "vol", "index_name"] = None
    sel["basis"] = None
    sel.loc[sel["index_name"].notna(), "basis"] = "RFR"
    sel.loc[(sel["product"] == "vol") & (sel["f3"] == "ATM_RFR"), "basis"] = "RFR"
    sel.loc[(sel["product"] == "vol") & (sel["f3"] == "ATM"), "basis"] = "LEGACY"

    keep = ["tag", "product", "currency", "index_name", "basis", "expiry", "tenor",
            "vol_type", "moneyness_bp"]
    return sel[keep].drop_duplicates("tag").reset_index(drop=True)


def _ccy_of(token):
    """USD_SOFR->USD, EURIBOR->EUR, EUR->EUR, JPY_TONAR_LCH->JPY."""
    c, _ = _split_ccy_index(str(token))
    if c:
        return c
    u = str(token).upper()
    return u[:3] if u[:3] in KNOWN_CCYS else u


def build_selection(path: str) -> pd.DataFrame:
    """Map the curated discovery inventory -> normalized pull dims (one row/tag).
    This is the *exact* pull list the user reviewed; covers every product in scope."""
    inv = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    out = []
    for rec in inv.to_dict("records"):
        ds, f2, f3 = rec.get("dataset"), rec.get("f2"), rec.get("f3")
        f4, f5, f6, f7 = rec.get("f4"), rec.get("f5"), rec.get("f6"), rec.get("f7")
        d = dict(tag=rec.get("tag"), product=None, currency=None, index_name=None,
                 basis=None, expiry=None, tenor=None, vol_type=None,
                 moneyness_bp=None, measure=None)
        if ds in ("RATES.OIS", "RATES.SWAP_LIBOR"):
            ccy, idx = _split_ccy_index(str(f2))
            d["currency"], d["index_name"] = ccy or _ccy_of(f2), idx
            d["basis"] = "RFR" if ds == "RATES.OIS" else "IBOR"
            if f3 == "PAR":
                d.update(product="swap_par", tenor=f4)
            elif f3 == "FWD":
                d.update(product="forward_swap", expiry=f4, tenor=f5)
            elif f3 == "ROLL_CARRY":
                d.update(product="roll_carry", expiry=f5, tenor=f6, measure=f7)
            elif f3 == "SWAP_SPREAD":
                d.update(product="swap_spread", tenor=f4)
            else:
                continue
        elif ds == "RATES.VOL":
            d["currency"], d["moneyness_bp"] = f2, 0
            d["basis"] = "RFR" if f3 == "ATM_RFR" else "LEGACY"
            if f4 == "NORMAL":
                d.update(product="vol", vol_type="NORMAL", measure=f5, expiry=f6, tenor=f7)
            elif f4 == "FWDPREMIUM":
                d.update(product="vol", vol_type="FWDPREMIUM", expiry=f5, tenor=f6)
            else:
                continue
        elif ds == "RATES.MIDCURVES":
            d.update(product="midcurve", currency=_ccy_of(f2), vol_type=f4,
                     measure=f3, expiry=f5, tenor=f6)
        elif ds == "RATES.TSY":
            d.update(product="bond_tips", currency=f3, tenor=f4, measure=f5)
        elif ds == "RATES.INFLATION":
            if f2 == "SWAP":
                # tag: RATES.INFLATION.SWAP.<CCY_INDEX>.<TENOR>  e.g. USD_CPURNSA.10Y
                ccy = str(f3).split("_")[0]
                d.update(product="inflation_swap", currency=ccy, index_name=f3,
                         tenor=f4, basis="REAL")
            else:
                continue
        elif ds == "RATES.BASIS_SWAPS":
            # tag: RATES.BASIS_SWAPS.<PRODUCT>.<CCY>.<TENOR>  e.g. 3S6S_BASIS.USD.10Y
            d.update(product="basis_swap", currency=f3, index_name=f2, tenor=f4, basis="IBOR")
        elif ds == "RATES.XCCY_SWAP":
            # tag: RATES.XCCY_SWAP.<CCY>.USD.PAR.<TENOR>  e.g. JPY.USD.PAR.10Y
            if f4 == "PAR":
                d.update(product="xccy_swap", currency=f2, index_name=f3,
                         tenor=f5, basis="XCCY")
            else:
                continue
        elif ds == "RATES.OIS_INVOICESPREAD":
            # tag: RATES.OIS_INVOICESPREAD.<PRODUCT>.<TENOR>  e.g. USD_SOFR_FRONTMONTH.10Y
            ccy = str(f2).split("_")[0]
            d.update(product="invoice_spread_ois", currency=ccy, index_name=f2,
                     tenor=f3, basis="RFR")
        else:
            continue
        out.append(d)
    return pd.DataFrame(out).drop_duplicates("tag").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Normalize API response -> long rows
# ---------------------------------------------------------------------------

_STR_COLS = ["tag", "field", "currency", "product", "basis", "index_name",
             "expiry", "tenor", "vol_type", "measure", "source_freq"]


def _enforce_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Force consistent dtypes so all partitions share one Arrow schema (else
    all-null dimension columns become Arrow 'null' type and break DuckDB)."""
    for c in _STR_COLS:
        if c in df.columns:
            df[c] = df[c].astype("string")
    if "value" in df.columns:
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if "moneyness_bp" in df.columns:
        df["moneyness_bp"] = pd.to_numeric(df["moneyness_bp"], errors="coerce")
    for c in ("date", "ingested_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    return df


def _to_long(frames: Dict[str, pd.DataFrame], dims: pd.DataFrame,
             source_freq: str) -> pd.DataFrame:
    """frames: {tag: df(datetime index, 'close')}; dims indexed by tag -> long rows."""
    dmap = dims.set_index("tag")
    now = pd.Timestamp.utcnow().tz_localize(None)
    out = []
    for tag, df in frames.items():
        if df is None or df.empty or "close" not in df:
            continue
        d = pd.DataFrame({"date": pd.to_datetime(df.index).normalize(),
                          "value": df["close"].values})
        d["tag"] = tag
        d["field"] = "close"
        row = dmap.loc[tag] if tag in dmap.index else None
        for col in ["currency", "product", "basis", "index_name", "expiry",
                    "tenor", "moneyness_bp", "vol_type", "measure"]:
            d[col] = (row[col] if row is not None else None)
        d["source_freq"] = source_freq
        d["ingested_at"] = now
        out.append(d)
    if not out:
        return pd.DataFrame(columns=LONG_COLS)
    res = pd.concat(out, ignore_index=True)
    return _enforce_schema(res[LONG_COLS])


# ---------------------------------------------------------------------------
# Partitioned Parquet store with upsert
# ---------------------------------------------------------------------------

def _partition_path(root: str, product: str, currency: str, year: int) -> str:
    return os.path.join(root, f"product={product}", f"currency={currency}",
                        f"year={year}", "data.parquet")


def write_upsert(root: str, long_df: pd.DataFrame) -> int:
    """Upsert long rows into partitioned parquet. Key = (tag, date). Last write wins."""
    if long_df.empty:
        return 0
    long_df = long_df.copy()
    long_df["year"] = pd.to_datetime(long_df["date"]).dt.year
    written = 0
    for (product, currency, year), grp in long_df.groupby(["product", "currency", "year"]):
        path = _partition_path(root, product, currency, int(year))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = grp.drop(columns=["year"])
        if os.path.exists(path):
            old = pd.read_parquet(path)
            merged = pd.concat([old, new], ignore_index=True)
        else:
            merged = new
        merged = (merged.sort_values("ingested_at")
                        .drop_duplicates(["tag", "date"], keep="last")
                        .sort_values(["tag", "date"]))
        merged.to_parquet(path, index=False)
        written += len(new)
    return written


# ---------------------------------------------------------------------------
# Ingest (backfill + daily)
# ---------------------------------------------------------------------------

def _yyyymmdd(d) -> int:
    return int(pd.Timestamp(d).strftime("%Y%m%d"))


def _pull_and_store(client, sel: pd.DataFrame, start, end, root: str,
                    freq: str = "DAILY", batch: int = 100) -> int:
    tags = sel["tag"].tolist()
    total = 0
    for i in range(0, len(tags), batch):
        chunk = tags[i:i + batch]
        frames = client.get_data(chunk, _yyyymmdd(start), _yyyymmdd(end), frequency=freq)
        long_df = _to_long(frames, sel[sel.tag.isin(chunk)], freq)
        total += write_upsert(root, long_df)
        log.info("batch %d/%d: %d tags -> %d rows (cum %d)",
                 i // batch + 1, (len(tags) - 1) // batch + 1, len(chunk), len(long_df), total)
    return total


def backfill(client, sel: pd.DataFrame, config: dict) -> int:
    start = config["ingest"].get("backfill_start", "2016-01-01")
    end = dt.date.today()
    freq = config["ingest"].get("frequency", "DAILY")
    log.info("BACKFILL %d tags %s -> %s", len(sel), start, end)
    return _pull_and_store(client, sel, start, end, config["storage"]["root"], freq)


def daily(client, sel: pd.DataFrame, config: dict) -> int:
    lb = int(config["ingest"].get("lookback_business_days", 7))
    end = dt.date.today()
    start = (pd.Timestamp(end) - pd.tseries.offsets.BDay(lb)).date()
    freq = config["ingest"].get("frequency", "DAILY")
    log.info("DAILY upsert %d tags, lookback %d bd (%s -> %s)", len(sel), lb, start, end)
    return _pull_and_store(client, sel, start, end, config["storage"]["root"], freq)


# ---------------------------------------------------------------------------
# DuckDB views
# ---------------------------------------------------------------------------

def build_duckdb(config: dict) -> None:
    import duckdb
    root = config["storage"]["root"]
    db = config["storage"]["duckdb"]
    con = duckdb.connect(db)
    # Use an ABSOLUTE, forward-slashed glob so the 'rates' view resolves no matter
    # what the working directory is when rates.duckdb is later opened (a relative
    # './data/...' glob only works when cwd == this folder). Resolved at build time;
    # if you ever relocate this folder, just re-run `python rates_pipeline.py duckdb`.
    glob_path = os.path.join(os.path.abspath(root), "**", "*.parquet").replace("\\", "/")
    con.execute("CREATE OR REPLACE VIEW rates AS "
                f"SELECT * FROM read_parquet('{glob_path}', hive_partitioning=true)")
    # per-product long views (valid; dynamic PIVOT is not allowed inside a view)
    for prod, vname in [("swap_par", "swap_par"), ("forward_swap", "forward_swap"),
                        ("vol", "vol_atm")]:
        con.execute(f"CREATE OR REPLACE VIEW {vname} AS "
                    f"SELECT * FROM rates WHERE product='{prod}'")
    n = con.execute("SELECT count(*) FROM rates").fetchone()[0]
    log.info("DuckDB %s: view 'rates' (%d rows) + swap_par / forward_swap / vol_atm.", db, n)
    con.close()
    try:
        import analytics
        analytics.build_analytics_views(config)
    except Exception as e:  # noqa: BLE001
        log.warning("analytics presets skipped: %s", e)


def load_wide(config: dict, product: str, currency: str, field: str = "close",
              expiry: Optional[str] = None) -> "pd.DataFrame":
    """On-demand wide panel: date index x tenor columns, for one product+currency.
    Use this for curve/fly math instead of a giant global pivot view. For
    forward_swap pass an ``expiry`` to slice one forward-start."""
    import duckdb
    con = duckdb.connect(config["storage"]["duckdb"])
    q = "SELECT date, tenor, value FROM rates WHERE product=? AND currency=? AND field=?"
    params = [product, currency, field]
    if expiry is not None:
        q += " AND expiry=?"; params.append(expiry)
    df = con.execute(q, params).df()
    con.close()
    if df.empty:
        return df
    return df.pivot_table(index="date", columns="tenor", values="value").sort_index()


# ---------------------------------------------------------------------------
# Mock client (offline test)
# ---------------------------------------------------------------------------

class _MockClient:
    def get_data(self, tags, start, end, frequency="DAILY"):
        dates = pd.bdate_range(pd.Timestamp(str(start)), pd.Timestamp(str(end)))
        out = {}
        for j, t in enumerate(tags):
            out[t] = pd.DataFrame({"close": [1.0 + j + 0.001 * i for i in range(len(dates))]},
                                  index=dates)
        return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Citi Velocity rates ingest pipeline.")
    ap.add_argument("cmd", choices=["select", "backfill", "daily", "duckdb"])
    ap.add_argument("--config", default="./config.yaml")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    config = load_config(args.config)

    if args.cmd == "duckdb":
        build_duckdb(config)
        return 0

    sel_path = config["storage"].get("selection")
    if sel_path:
        sel = build_selection(sel_path)
    else:
        inv_path = config["storage"]["inventory"]
        inventory = (pd.read_parquet(inv_path) if inv_path.endswith(".parquet")
                     else pd.read_csv(inv_path, low_memory=False))
        sel = select_tags(inventory, config)
    print(f"Selected {len(sel)} tags:")
    print(sel.groupby(["product", "currency"]).size().to_string())

    if args.cmd == "select":
        return 0

    client = _MockClient() if args.mock else _make_client()
    if args.cmd == "backfill":
        n = backfill(client, sel, config)
    else:
        n = daily(client, sel, config)
    log.info("Wrote/updated %d rows. Rebuilding DuckDB views...", n)
    try:
        build_duckdb(config)
        coverage_report(config, requested=len(sel))
    except Exception as e:  # noqa: BLE001
        log.warning("DuckDB / coverage step skipped: %s", e)
    return 0


def coverage_report(config: dict, requested: int = None) -> None:
    """Print per-product row/date coverage and flag tags that returned no data."""
    import duckdb
    con = duckdb.connect(config["storage"]["duckdb"])
    print("\n=== coverage by product ===")
    print(con.execute(
        "SELECT product, count(distinct tag) tags, min(date) first_date, max(date) last_date, "
        "count(*) n_rows FROM rates GROUP BY product ORDER BY product").df().to_string(index=False))
    got = con.execute("SELECT count(distinct tag) FROM rates").fetchone()[0]
    con.close()
    if requested is not None:
        print(f"\ntags requested: {requested:,} | tags with data: {got:,} | "
              f"empty/no-data: {requested - got:,}")


def _load_secrets(*candidates):
    """Load KEY=VALUE creds from a local secrets.env (does not override real env vars)."""
    import os
    _here = os.path.dirname(os.path.abspath(__file__))
    paths = list(candidates) or [
        # centralized creds in the consolidated Data Feeds tree (historical/ -> ../../common)
        os.path.join(_here, "..", "..", "common", "secrets.env"),
        os.path.join(os.getcwd(), "secrets.env"),
        os.path.join(_here, "secrets.env"),
    ]
    for p in paths:
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # AUTHORITATIVE override (was setdefault): the secrets file is the
                # source of truth, so a stale CITI_* var already in the environment
                # can't shadow the real creds and cause a spurious 401. Mirrors the
                # live feed's load_secrets fix.
                os.environ[k.strip()] = v.strip()
            log.info("Loaded credentials from %s", p)
            return True
    return False


def _make_client():
    _load_secrets()
    from citivelocity_rates import CitiVelocityClient
    return CitiVelocityClient()


if __name__ == "__main__":
    raise SystemExit(main())
