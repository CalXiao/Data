#!/usr/bin/env python3
r"""
generate_uk.py — UK (England & Wales bank holiday) calendar, exact port of the
pricer engine's holidaysUK() (Vol Pricer\src\engine.js). Validated 375/375
against the Seasonals duckdb UK region 1985-2030 on 2026-07-06.

Rules (see SKILL.md): substitute rule (weekend -> next open weekday, chained
for Christmas/Boxing), Easter-derived Good Friday + Easter Monday, Early May /
Spring / Summer bank holidays with jubilee-year Spring moves and VE-day Early
May moves, plus royal one-offs.

Usage:  python generate_uk.py [--start 1985] [--end 2060] [--out uk_holidays.csv]
"""
import argparse, csv
from datetime import date, timedelta

def nth(y, m, wd, n):
    d = date(y, m, 1); off = (wd - d.weekday()) % 7
    return date(y, m, 1 + off + 7 * (n - 1))

def last_wd(y, m, wd):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)

def easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)

def holidays(y):
    H = {}
    def sub(d):
        while d.weekday() >= 5 or d in H: d += timedelta(days=1)
        return d
    def A(d, n): H[d] = n
    A(sub(date(y, 1, 1)), "New Year's Day")
    es = easter(y)
    A(es - timedelta(days=2), "Good Friday"); A(es + timedelta(days=1), "Easter Monday")
    A(date(y, 5, 8) if y in (1995, 2020) else nth(y, 5, 0, 1), "Early May Bank Holiday")
    A(date(y, 6, 4) if y in (2002, 2012) else date(y, 6, 2) if y == 2022 else last_wd(y, 5, 0), "Spring Bank Holiday")
    A(last_wd(y, 8, 0), "Summer Bank Holiday")
    A(sub(date(y, 12, 25)), "Christmas Day"); A(sub(date(y, 12, 26)), "Boxing Day")
    for yy, mm, dd, n in [(1999, 12, 31, "Millennium Eve"), (2002, 6, 3, "Golden Jubilee"),
                          (2011, 4, 29, "Royal Wedding"), (2012, 6, 5, "Diamond Jubilee"),
                          (2022, 6, 3, "Platinum Jubilee"), (2022, 9, 19, "State Funeral"),
                          (2023, 5, 8, "Coronation")]:
        if y == yy: A(date(yy, mm, dd), n)
    return sorted(H.items())

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1985); p.add_argument("--end", type=int, default=2060)
    p.add_argument("--out", default="uk_holidays.csv")
    a = p.parse_args()
    rows = [(d, n) for y in range(a.start, a.end + 1) for d, n in holidays(y)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name"])
        for d, n in rows: w.writerow([d.isoformat(), n])
    print(f"wrote {a.out}: {len(rows)} rows")
