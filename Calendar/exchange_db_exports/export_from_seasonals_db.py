#!/usr/bin/env python3
r"""
export_from_seasonals_db.py — re-export the exchange/market holiday calendars
from Seasonals\calendar_events.duckdb (the pre-existing seasonality event store,
populated 2025-05-20; original generator unknown — treat the duckdb as source
of truth for these NON-procedural sets).

Scope notes discovered on migration (2026-07-06):
  - region 'US' = NYSE equities calendar (no Columbus/Veterans, MLK from 1998,
    ALL Good Fridays, event closes: 1985 Gloria, 1994 Nixon, 9/11 week, 2004
    Reagan, 2007 Ford, 2012 Sandy, 2018 Bush, 2025 Carter). The PRICER uses the
    SIFMA calendar instead -> ..\us_sifma\ (procedural, different rules).
  - region 'JP' = XTKS (TSE) closes, weekday-falling only, incl. the 2020-10-01
    TSE systems-outage halt. The PRICER uses the Tokyo bank calendar -> ..\japan\.
  - 'JEWISH' region reconciles EXACTLY with the procedural generator in
    ..\jewish\ (kept there as db-independent source).
Run from anywhere: paths resolve relative to this file (Pyth tree layout).
"""
import csv, os
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "..", "..", "Seasonals", "calendar_events.duckdb")
EXPORTS = [  # (region, event_type, out_csv)
    ("US", "holiday", "us_nyse_holidays.csv"),
    ("UK", "holiday", "uk_holidays.csv"),
    ("DE", "holiday", "de_holidays.csv"),
    ("FR", "holiday", "fr_holidays.csv"),
    ("JP", "holiday", "jp_xtks_holidays.csv"),
    ("CN", "holiday", "cn_holidays.csv"),
    ("JEWISH", "holiday", "jewish_holidays_db.csv"),
]

def main():
    con = duckdb.connect(DB, read_only=True)
    for region, etype, fn in EXPORTS:
        rows = con.execute(
            "SELECT event_date, description FROM calendar_events "
            "WHERE region=? AND event_type=? ORDER BY event_date", [region, etype]).fetchall()
        with open(os.path.join(HERE, fn), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["date", "name"])
            for d, n in rows: w.writerow([d.isoformat(), n])
        print(f"{fn}: {len(rows)}")
    rows = con.execute(
        "SELECT event_date, description, coalesce(early_close_utc,'') FROM calendar_events "
        "WHERE event_type='early_close' ORDER BY event_date").fetchall()
    with open(os.path.join(HERE, "us_sifma_early_closes.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name", "early_close_utc"])
        for d, n, t in rows: w.writerow([d.isoformat(), n, t])
    print(f"us_sifma_early_closes.csv: {len(rows)}")

if __name__ == "__main__":
    main()
