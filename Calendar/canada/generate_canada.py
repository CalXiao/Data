#!/usr/bin/env python3
r"""
generate_canada.py — Canada (Toronto bank) holiday calendar, exact port of the
pricer engine's holidaysCA() (Vol Pricer\src\engine.js). NO external reference
set exists (the Seasonals duckdb has no CA region) — rules-only; spot-check
notable dates against a bank holiday schedule when convenient.

Rules (see SKILL.md): weekend fixed dates observe next Monday; Family Day (ON)
from 2008; Truth & Reconciliation from 2021; Victoria Day = Monday on/before
May 24; Christmas/Boxing observed as a chained pair.

Usage:  python generate_canada.py [--start 1985] [--end 2060] [--out canada_holidays.csv]
"""
import argparse, csv
from datetime import date, timedelta

def nth(y, m, wd, n):
    d = date(y, m, 1); off = (wd - d.weekday()) % 7
    return date(y, m, 1 + off + 7 * (n - 1))

def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)

def mon(d):
    return d + timedelta(days=2) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d

def holidays(y):
    H = []
    A = lambda d, n: H.append((d, n))
    A(mon(date(y, 1, 1)), "New Year's Day")
    if y >= 2008: A(nth(y, 2, 0, 3), "Family Day")
    A(easter(y) - timedelta(days=2), "Good Friday")
    v = date(y, 5, 24)
    while v.weekday() != 0: v -= timedelta(days=1)
    A(v, "Victoria Day")
    A(mon(date(y, 7, 1)), "Canada Day")
    A(nth(y, 8, 0, 1), "Civic Holiday")
    A(nth(y, 9, 0, 1), "Labour Day")
    if y >= 2021: A(mon(date(y, 9, 30)), "National Day for Truth and Reconciliation")
    A(nth(y, 10, 0, 2), "Thanksgiving (CA)")
    A(mon(date(y, 11, 11)), "Remembrance Day")
    w = date(y, 12, 25).weekday()
    if w == 5:   A(date(y, 12, 27), "Christmas Day (obs)"); A(date(y, 12, 28), "Boxing Day (obs)")
    elif w == 6: A(date(y, 12, 26), "Christmas Day (obs)"); A(date(y, 12, 27), "Boxing Day (obs)")
    elif w == 4: A(date(y, 12, 25), "Christmas Day"); A(date(y, 12, 28), "Boxing Day (obs)")
    else:        A(date(y, 12, 25), "Christmas Day"); A(date(y, 12, 26), "Boxing Day")
    return sorted(H)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1985); p.add_argument("--end", type=int, default=2060)
    p.add_argument("--out", default="canada_holidays.csv")
    a = p.parse_args()
    rows = [(d, n) for y in range(a.start, a.end + 1) for d, n in holidays(y)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name"])
        for d, n in rows: w.writerow([d.isoformat(), n])
    print(f"wrote {a.out}: {len(rows)} rows")
