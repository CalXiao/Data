#!/usr/bin/env python3
r"""
generate_japan.py — procedurally generate the Japan (Tokyo bank) holiday
calendar. For years >= 2016 this is an EXACT port of the pricer engine's
holidaysJP() in Vol Pricer\src\engine.js (asserted by validate step); for
1985-2015 it adds the historical rules the engine doesn't need (pre-2000
fixed-date Coming-of-Age/Sports days, Showa-era Emperor's Birthday, Marine Day
introduction 1996, one-off imperial events).

Rules (see SKILL.md): equinox day approximations, Happy-Monday shifts, the
citizens'-holiday sandwich rule (>=1986), the substitute-holiday rule
(Sun -> next open day, >=1973), bank closures Jan 2-3 + Dec 31, and the
2019/2020/2021 one-offs & Olympic moves.

Usage:  python generate_japan.py [--start 1985] [--end 2060] [--out japan_holidays.csv]
"""
import argparse, csv
from datetime import date, timedelta

def nth(y, m, wd, n):
    d = date(y, m, 1); off = (wd - d.weekday()) % 7
    return date(y, m, 1 + off + 7 * (n - 1))

def equinox(y, vernal):
    base = 20.8431 if vernal else 23.2488
    return int(base + 0.242194 * (y - 1980)) - (y - 1980) // 4

def national(y):
    """National holidays BEFORE citizens'/substitute rules. dict date->name."""
    N = {}
    A = lambda d, n: N.setdefault(d, n)
    A(date(y, 1, 1), "New Year's Day")
    A(nth(y, 1, 0, 2) if y >= 2000 else date(y, 1, 15), "Coming of Age Day")
    A(date(y, 2, 11), "National Foundation Day")
    if y >= 2020: A(date(y, 2, 23), "Emperor's Birthday")
    elif 1989 <= y <= 2018: A(date(y, 12, 23), "Emperor's Birthday")
    # (pre-1989 Showa Emperor's birthday IS Apr 29, added below)
    A(date(y, 3, equinox(y, True)), "Vernal Equinox Day")
    A(date(y, 4, 29), "Showa Day" if y >= 2007 else "Greenery Day" if y >= 1989 else "Emperor's Birthday")
    A(date(y, 5, 3), "Constitution Memorial Day")
    if y >= 2007: A(date(y, 5, 4), "Greenery Day")     # pre-2007 May 4 comes via citizens' rule
    A(date(y, 5, 5), "Children's Day")
    if y == 2020: A(date(y, 7, 23), "Marine Day")      # Olympic moves
    elif y == 2021: A(date(y, 7, 22), "Marine Day")
    elif y >= 2003: A(nth(y, 7, 0, 3), "Marine Day")
    elif y >= 1996: A(date(y, 7, 20), "Marine Day")
    if y == 2020: A(date(y, 8, 10), "Mountain Day")
    elif y == 2021: A(date(y, 8, 8), "Mountain Day")
    elif y >= 2016: A(date(y, 8, 11), "Mountain Day")
    A(nth(y, 9, 0, 3) if y >= 2003 else date(y, 9, 15), "Respect for the Aged Day")
    A(date(y, 9, equinox(y, False)), "Autumnal Equinox Day")
    if y == 2020: A(date(y, 7, 24), "Sports Day")
    elif y == 2021: A(date(y, 7, 23), "Sports Day")
    elif y >= 2000: A(nth(y, 10, 0, 2), "Sports Day" if y >= 2020 else "Health-Sports Day")
    else: A(date(y, 10, 10), "Health-Sports Day")
    A(date(y, 11, 3), "Culture Day")
    A(date(y, 11, 23), "Labor Thanksgiving Day")
    if y == 1989: A(date(y, 2, 24), "Funeral of Emperor Showa")
    if y == 1990: A(date(y, 11, 12), "Enthronement Ceremony")
    if y == 1993: A(date(y, 6, 9), "Crown Prince Wedding")
    if y == 2019: A(date(y, 5, 1), "Accession Day"); A(date(y, 10, 22), "Enthronement Ceremony")
    return N

def holidays(y):
    N = national(y)
    if y >= 1986:                                       # citizens' holiday (sandwich rule)
        for d in list(N):
            mid = d + timedelta(days=1)
            if d + timedelta(days=2) in N and mid not in N and mid.weekday() != 6:
                N[mid] = "Citizens' Holiday"
    if y >= 1973:                                       # substitute: Sunday -> next open day
        for d in list(N):
            if d.weekday() == 6:
                x = d + timedelta(days=1)
                while x in N: x += timedelta(days=1)
                N[x] = N[d] + " (substitute)"
    for d, n in [(date(y, 1, 2), "Bank Holiday"), (date(y, 1, 3), "Bank Holiday"),
                 (date(y, 12, 31), "Bank Holiday")]:
        N.setdefault(d, n)
    return sorted(N.items())

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1985); p.add_argument("--end", type=int, default=2060)
    p.add_argument("--out", default="japan_holidays.csv")
    a = p.parse_args()
    rows = [(d, n) for y in range(a.start, a.end + 1) for d, n in holidays(y)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name"])
        for d, n in rows: w.writerow([d.isoformat(), n])
    print(f"wrote {a.out}: {len(rows)} rows")
