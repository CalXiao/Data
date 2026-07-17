"""
discover_rates.py -- Step 0 of the rates data pipeline.

Goal: before we hardcode any tenor/grid config, enumerate what actually EXISTS in
Citi Velocity for your currencies, so we curate the universe from reality rather
than from guesses about the tag grammar.

Quota-aware design
------------------
* Tag listing is capped at ~100 calls/day, so we make ONE list call per dataset
  and partition by currency in memory (not one call per ccy x dataset).
* The metadata API is capped at 100,000 items / 24h. Metadata annotation is
  therefore OPT-IN (--metadata none|sample|all) and budgeted; it stops cleanly
  rather than spamming failures when a quota is hit.
* --from-inventory rebuilds the summary + grammar map from an inventory file you
  already have, making ZERO API calls.

Outputs (to --out-dir, default ./discovery):
  rates_tag_inventory.parquet / .csv   one row per tag, with parsed dimensions
  rates_dataset_summary.csv            counts + date ranges per (dataset, ccy)
  rates_grammar.txt                    distinct token values per tag position
  vol_tree.txt                         browse() dump of RATES.VOL per ccy

Run
---
    export CITI_CLIENT_ID=...; export CITI_CLIENT_SECRET=...
    python discover_rates.py                         # inventory only (no metadata)
    python discover_rates.py --metadata sample       # + date ranges for a sample
    python discover_rates.py --from-inventory discovery/rates_tag_inventory.csv  # offline
    python discover_rates.py --mock                  # offline self-test
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger("discover")

DEFAULT_CCYS = ["EUR", "USD", "GBP", "AUD", "JPY", "CAD"]

DEFAULT_DATASETS = [
    "RATES.OIS",            # RFR/OIS swaps (live curve)
    "RATES.SWAP_LIBOR",     # legacy IBOR swaps (parallel history)
    "RATES.FORWARD",        # forward-starting swaps
    "RATES.MIDCURVES",      # mid-curve
    "RATES.FRA",            # FRAs
    "RATES.FRA_OIS",
    "RATES.OIS_MEETING",    # dated meeting OIS
    "RATES.XCCY_OIS_SWAP",  # cross-ccy
    "RATES.TSY",            # treasury OTR (USD)
    "RATES.SOV",            # sovereign bonds (non-USD)
    "RATES.SOV_CMT",        # sovereign CMT
    "RATES.TIPS",           # linkers
    "RATES.SSA",            # SSA EUR
    "RATES.VOL",            # swaption vol (ATM + RR)
]

KNOWN_CCYS = {"USD", "EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "SEK",
              "NOK", "DKK", "CNY", "CNH", "HKD", "SGD", "KRW", "MXN", "ZAR",
              "PLN", "CZK", "HUF", "ILS", "INR", "BRL"}

TENOR_RE = re.compile(r"^\d+(?:\.\d+)?[DWMY]$", re.IGNORECASE)
MONEYNESS_RE = re.compile(r"^[+-]?\d+\s*(?:BP|BPS)?$|^(?:ATM|STRIKE_[CP]?\d+|RR\d+)$",
                          re.IGNORECASE)


def parse_tag(tag):
    """Best-effort split of a tag into dimensions. Raw dotted fields kept as f0..fN."""
    fields = tag.split(".")
    rec = {"tag": tag, "n_fields": len(fields)}
    for i, f in enumerate(fields):
        rec[f"f{i}"] = f
    rec["category"] = fields[0] if fields else None
    rec["subcategory"] = fields[1] if len(fields) > 1 else None
    rec["dataset"] = ".".join(fields[:2]) if len(fields) > 1 else fields[0]
    ccy = next((f.upper() for f in fields if f.upper() in KNOWN_CCYS), None)
    rec["ccy"] = ccy
    tenor_tokens = [f.upper() for f in fields if TENOR_RE.match(f)]
    rec["tenor_tokens"] = ",".join(tenor_tokens) if tenor_tokens else None
    if tenor_tokens:
        rec["tenor"] = tenor_tokens[-1]
        rec["expiry"] = tenor_tokens[-2] if len(tenor_tokens) >= 2 else None
    else:
        rec["tenor"] = rec["expiry"] = None
    upper = tag.upper()
    rec["is_atm"] = "ATM" in upper
    rec["vol_type"] = ("NORMAL" if "NORMAL" in upper
                       else "BLACK" if "BLACK" in upper else None)
    rec["measure"] = ("ANNUAL" if "ANNUAL" in upper
                      else "DAILY" if ".DAILY" in upper else None)
    rec["basis_hint"] = ("RFR" if "RFR" in upper or "OIS" in upper
                         else "LIBOR" if "LIBOR" in upper else None)
    money = [f for f in fields if MONEYNESS_RE.match(f) and not TENOR_RE.match(f)
             and f.upper() != "ATM"]
    rec["moneyness_tokens"] = ",".join(money) if money else None
    return rec


def discover(client, currencies, datasets, metadata="none", metadata_budget=90000):
    """Enumerate tags per dataset (ONE list call each), partition by ccy in memory."""
    rows = []
    for ds in datasets:
        try:
            all_tags = client.list_tags(ds)           # ONE call per dataset
        except Exception as e:
            log.warning("listing %s failed: %s", ds, e)
            continue
        for ccy in currencies:
            tags = [t for t in all_tags
                    if ccy.upper() in {p.upper() for p in t.split(".")}]
            log.info("%-22s %s -> %6d tags", ds, ccy, len(tags))
            for t in tags:
                rec = parse_tag(t)
                rec["query_dataset"] = ds
                rec["query_ccy"] = ccy
                rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["tag"]).reset_index(drop=True)
    log.info("Total unique tags discovered: %d", len(df))
    if metadata != "none":
        if metadata == "sample":
            sample = (df.groupby(["dataset", "ccy"], dropna=False, group_keys=False)
                        .apply(lambda g: g.head(200)))
            target = sample["tag"].tolist()
            log.info("Metadata SAMPLE mode: annotating %d of %d tags.", len(target), len(df))
        else:
            target = df["tag"].tolist()
        _attach_metadata(client, df, target_tags=target, budget=metadata_budget)
    return df


def _attach_metadata(client, df, target_tags, batch=1000, budget=90000):
    """Annotate startDate/endDate/description, stopping cleanly at the budget."""
    for col in ("startDate", "endDate", "description"):
        if col not in df.columns:
            df[col] = pd.NA
    spent = 0
    for i in range(0, len(target_tags), batch):
        if spent + batch > budget:
            log.warning("Metadata budget (%d) reached after %d tags; stopping. "
                        "Remaining tags left without date ranges (quota resets ~24h).",
                        budget, spent)
            break
        chunk = target_tags[i:i + batch]
        try:
            meta = client.get_metadata(chunk)
        except Exception as e:
            log.warning("metadata batch %d failed (stopping): %s", i // batch, e)
            break
        spent += len(chunk)
        if meta.empty:
            continue
        for col in ("startDate", "endDate", "description"):
            if col in meta.columns:
                mapping = meta[col].to_dict()
                df.loc[df["tag"].isin(mapping), col] = df["tag"].map(mapping)
        log.info("metadata: %d/%d target tags annotated",
                 min(i + batch, len(target_tags)), len(target_tags))


def dataset_summary(df):
    if df.empty:
        return df
    g = df.groupby(["dataset", "ccy"], dropna=False)
    out = g.agg(n_tags=("tag", "count"),
                n_tenors=("tenor", lambda s: s.dropna().nunique()),
                example_tag=("tag", "first"))
    if "startDate" in df.columns:
        out["min_start"] = g["startDate"].min()
        out["max_end"] = g["endDate"].max()
    return out.reset_index()


def grammar_summary(df, max_vals=40, max_field=9):
    """Distinct token values per tag position, per (dataset, ccy). The curation aid."""
    lines = []
    for (ds, ccy), g in df.groupby(["dataset", "ccy"], dropna=False):
        lines.append(f"== {ds}  {ccy}   n_tags={len(g)} ==")
        lines.append(f"   example: {g['tag'].iloc[0]}")
        maxf = int(g["n_fields"].max())
        for i in range(2, min(maxf, max_field)):
            col = f"f{i}"
            if col not in g:
                continue
            vals = sorted(str(v) for v in g[col].dropna().unique())
            if not vals:
                continue
            shown = vals[:max_vals]
            more = f"  (+{len(vals) - max_vals} more)" if len(vals) > max_vals else ""
            lines.append(f"   f{i}: {len(vals):>4d} distinct -> {shown}{more}")
        lines.append("")
    return "\n".join(lines)


def dump_vol_tree(client, currencies, path):
    """Walk RATES.VOL one level at a time per ccy to expose the ATM/RR grammar."""
    lines = []

    def walk(prefix, depth, max_depth=4):
        try:
            node = client.browse(prefix)
        except Exception as e:
            lines.append(f"{'  ' * depth}{prefix}  [browse error: {e}]")
            return
        fields = node.get("fields") or {}
        desc = node.get("description")
        if desc:
            lines.append(f"{'  ' * depth}{prefix}  -- {desc}")
        for k, v in fields.items():
            lines.append(f"{'  ' * depth}{prefix + ('.' if prefix else '')}{k}  ({v})")
            if depth < max_depth and not node.get("leaves"):
                walk(prefix + ("." if prefix else "") + k, depth + 1, max_depth)

    for ccy in currencies:
        lines.append(f"\n===== RATES.VOL.{ccy} =====")
        walk(f"RATES.VOL.{ccy}", 0)
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    log.info("Wrote vol tree -> %s", path)


MOCK_TAGS = [
    "RATES.OIS.USD.SOFR.PAR.2Y", "RATES.OIS.USD.SOFR.PAR.10Y",
    "RATES.OIS.EUR.ESTR.PAR.5Y", "RATES.OIS.GBP.SONIA.PAR.10Y",
    "RATES.SWAP_LIBOR.USD.PAR.2Y", "RATES.SWAP_LIBOR.USD.PAR.10Y",
    "RATES.SWAP_LIBOR.JPY.PAR.10Y",
    "RATES.FORWARD.USD.SOFR.1Y.1Y", "RATES.FORWARD.USD.SOFR.5Y.5Y",
    "RATES.TSY.USD.OTR.10Y", "RATES.SOV.GBP.OTR.10Y",
    "RATES.VOL.USD.ATM_RFR.NORMAL.ANNUAL.1M.3M",
    "RATES.VOL.USD.ATM_RFR.NORMAL.ANNUAL.1Y.10Y",
    "RATES.VOL.USD.RFR.NORMAL.ANNUAL.1Y.10Y.+50",
    "RATES.VOL.USD.RFR.NORMAL.ANNUAL.1Y.10Y.-50",
    "RATES.VOL.EUR.ATM.NORMAL.ANNUAL.1M.3M",
]


class _MockClient:
    def list_tags(self, prefix, regex=None, tag_type=None):
        return [t for t in MOCK_TAGS if t.startswith(prefix)]

    def get_metadata(self, tags, frequency="EOD"):
        return pd.DataFrame(
            {"description": [f"desc {t.split('.')[-1]}" for t in tags],
             "startDate": [20160601] * len(tags),
             "endDate": [20240620] * len(tags)},
            index=pd.Index(tags, name="tag"))

    def browse(self, prefix=""):
        return {"header": "mock", "fields": {"ATM": "At the money", "RR": "Risk reversal"},
                "leaves": [], "status": "OK"}


def _write_summaries(df, out_dir):
    """Write inventory, dataset summary and grammar map. No API calls."""
    inv_csv = os.path.join(out_dir, "rates_tag_inventory.csv")
    inv_pq = os.path.join(out_dir, "rates_tag_inventory.parquet")
    df.to_csv(inv_csv, index=False)
    try:
        df.to_parquet(inv_pq, index=False)
    except Exception as e:
        log.warning("parquet write skipped (%s); CSV written.", e)
    summ = dataset_summary(df)
    summ_csv = os.path.join(out_dir, "rates_dataset_summary.csv")
    summ.to_csv(summ_csv, index=False)
    gram_path = os.path.join(out_dir, "rates_grammar.txt")
    with open(gram_path, "w") as fh:
        fh.write(grammar_summary(df))
    print(f"\nDiscovered {len(df)} unique tags across "
          f"{df['dataset'].nunique()} datasets, {df['ccy'].nunique()} currencies.")
    print(f"  inventory : {inv_csv}")
    print(f"  summary   : {summ_csv}")
    print(f"  grammar   : {gram_path}\n")
    print(summ.to_string(index=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Discover available Citi Velocity rates tags.")
    ap.add_argument("--currencies", nargs="+", default=DEFAULT_CCYS)
    ap.add_argument("--datasets", nargs="+", default=DEFAULT_DATASETS)
    ap.add_argument("--out-dir", default="./discovery")
    ap.add_argument("--metadata", choices=["none", "sample", "all"], default="none",
                    help="Date-range annotation. none (default, 0 calls), "
                         "sample (<=200/group), all (up to --metadata-budget).")
    ap.add_argument("--metadata-budget", type=int, default=90000,
                    help="Max metadata items to spend (cap is 100k/24h).")
    ap.add_argument("--no-vol-tree", action="store_true", help="Skip the VOL browse dump.")
    ap.add_argument("--from-inventory", default=None,
                    help="OFFLINE: rebuild summary+grammar from an existing inventory "
                         "CSV/parquet. Makes ZERO API calls.")
    ap.add_argument("--mock", action="store_true", help="Run offline against mock data.")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-metadata", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    os.makedirs(args.out_dir, exist_ok=True)

    if args.from_inventory:
        path = args.from_inventory
        df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
        log.info("Loaded %d tags from %s (offline, no API calls).", len(df), path)
        _write_summaries(df, args.out_dir)
        return 0

    if args.mock:
        client = _MockClient()
    else:
        from citivelocity_rates import CitiVelocityClient
        client = CitiVelocityClient()

    metadata = "none" if args.no_metadata else args.metadata
    df = discover(client, args.currencies, args.datasets,
                  metadata=metadata, metadata_budget=args.metadata_budget)
    if df.empty:
        log.warning("No tags discovered. Check entitlement / dataset names.")
        return 1

    _write_summaries(df, args.out_dir)

    if not args.no_vol_tree and not args.mock:
        dump_vol_tree(client, args.currencies, os.path.join(args.out_dir, "vol_tree.txt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
