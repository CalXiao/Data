#!/usr/bin/env python3
r"""
generate_us_sifma.py — procedurally generate the US SIFMA (government-securities
full-close) holiday calendar. EXACT port of the pricer engine's holidays() in
Vol Pricer\src\engine.js (the parity-tested embedded copy); that JS stays the
runtime source for the pricer — this file is the documented reference/generator.

Rules (see SKILL.md):
  fixed-date holidays observe Sun->Mon; Sat -> NOT observed (bond market already
  closed; no Friday observance in the engine's SIFMA convention) — except
  Christmas (Sat->Dec 24) and Juneteenth/July 4 (Sat->prev Fri).
  Good Friday = Easter-2d via Gauss/anonymous algorithm, but is DROPPED when it
  is the FIRST Friday of its month (NFP-release Good Fridays: bond market opens).
  Juneteenth only from 2022.

Usage:  python generate_us_sifma.py [--start 1985] [--end 2060] [--out us_sifma_holidays.csv]
"""
import argparse, csv
from datetime import date, timedelta

def nth(y, m, wd, n):      # wd: Mon=0..Sun=6
    d = date(y, m, 1); off = (wd - d.weekday()) % 7
    return date(y, m, 1 + off + 7 * (n - 1))

def last_wd(y, m, wd):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)

def easter(y):             # anonymous Gregorian computus (same as engine.js)
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31; da = (h + l - 7 * m + 114) % 31 + 1
    return date(y, mo, da)

def holidays(y):
    H = []
    A = lambda d, n: H.append((d, n))
    d = date(y, 1, 1)                                     # New Year: Sun->Jan2, Sat->skip
    if d.weekday() == 6: A(date(y, 1, 2), "New Year's Day (obs)")
    elif d.weekday() != 5: A(d, "New Year's Day")
    A(nth(y, 1, 0, 3), "Martin Luther King Jr. Day")
    A(nth(y, 2, 0, 3), "Presidents Day")
    gf = easter(y) - timedelta(days=2)                    # Good Friday, unless 1st Friday (NFP)
    if gf != nth(gf.year, gf.month, 4, 1): A(gf, "Good Friday")
    A(last_wd(y, 5, 0), "Memorial Day")
    if y >= 2022:                                         # Juneteenth: Sat->Fri, Sun->Mon
        d = date(y, 6, 19)
        A(d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d,
          "Juneteenth" + (" (obs)" if d.weekday() in (5, 6) else ""))
    d = date(y, 7, 4)                                     # Independence: Sat->Jul3, Sun->Jul5
    A(d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d,
      "Independence Day" + (" (obs)" if d.weekday() in (5, 6) else ""))
    A(nth(y, 9, 0, 1), "Labor Day")
    A(nth(y, 10, 0, 2), "Columbus Day")
    d = date(y, 11, 11)                                   # Veterans: Sun->Nov12, Sat->skip
    if d.weekday() == 6: A(date(y, 11, 12), "Veterans Day (obs)")
    elif d.weekday() != 5: A(d, "Veterans Day")
    A(nth(y, 11, 3, 4), "Thanksgiving Day")
    d = date(y, 12, 25)                                   # Christmas: Sat->Dec24, Sun->Dec26
    A(d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d,
      "Christmas Day" + (" (obs)" if d.weekday() in (5, 6) else ""))
    return H

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1985); p.add_argument("--end", type=int, default=2060)
    p.add_argument("--out", default="us_sifma_holidays.csv")
    a = p.parse_args()
    rows = sorted((d, n) for y in range(a.start, a.end + 1) for d, n in holidays(y))
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name"])
        for d, n in rows: w.writerow([d.isoformat(), n])
    print(f"wrote {a.out}: {len(rows)} rows")
