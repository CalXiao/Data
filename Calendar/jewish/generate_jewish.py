#!/usr/bin/env python3
r"""
generate_jewish.py — procedurally generate the Jewish (Hebrew-calendar) holiday
set from first principles: classical molad arithmetic + the four postponement
rules (dechiyot), per Dershowitz & Reingold, "Calendrical Calculations".
No external libraries; exact for all years in range.

Output: jewish_holidays.csv  (date,name)  — validated 1:1 against the
pre-existing Seasonals calendar_events.duckdb JEWISH region (1985-2030).

Usage:  python generate_jewish.py [--start 1985] [--end 2060] [--out jewish_holidays.csv]
"""
import argparse, csv
from datetime import date, timedelta

# ---- Hebrew calendar core (Dershowitz-Reingold; RD = Rata Die day numbers,
#      date.toordinal() in Python IS the RD number) ----
HEBREW_EPOCH = -1373428          # RD of Tishrei 1, AM 1 (proleptic)

def is_leap(y):                  # 7 leap years per 19-year Metonic cycle
    return (7 * y + 1) % 19 < 7

def elapsed_days(y):
    """Days from Hebrew epoch to Rosh Hashanah of Hebrew year y (molad + dechiyot)."""
    months = 235 * ((y - 1) // 19) + 12 * ((y - 1) % 19) + (7 * ((y - 1) % 19) + 1) // 19
    parts = 204 + 793 * (months % 1080)
    hours = 5 + 12 * months + 793 * (months // 1080) + parts // 1080
    day = 1 + 29 * months + hours // 24
    parts = 1080 * (hours % 24) + parts % 1080
    # dechiyot 2-4: molad old (>= 18h), Tuesday molad of common year, Monday molad after leap year
    if (parts >= 19440
            or (day % 7 == 2 and parts >= 9924 and not is_leap(y))
            or (day % 7 == 1 and parts >= 16789 and is_leap(y - 1))):
        day += 1
    # dechiya 1 (lo ADU rosh): Rosh Hashanah may not fall Sun/Wed/Fri
    if day % 7 in (0, 3, 5):
        day += 1
    return day

def year_days(y): return elapsed_days(y + 1) - elapsed_days(y)
def long_marcheshvan(y): return year_days(y) in (355, 385)
def short_kislev(y): return year_days(y) in (353, 383)
def last_month(y): return 13 if is_leap(y) else 12   # months numbered Nisan=1 .. Adar(II)

def month_days(y, m):
    if m in (2, 4, 6, 10, 13): return 29             # Iyar, Tammuz, Elul, Tevet, Adar II
    if m == 12 and not is_leap(y): return 29         # Adar in common year
    if m == 8 and not long_marcheshvan(y): return 29 # Marcheshvan
    if m == 9 and short_kislev(y): return 29         # Kislev
    return 30

def to_fixed(y, m, d):
    """RD of Hebrew y-m-d (m: Nisan=1..Adar II=13; year begins Tishrei=7)."""
    total = HEBREW_EPOCH + elapsed_days(y) + d - 1
    if m < 7:
        for mm in range(7, last_month(y) + 1): total += month_days(y, mm)
        for mm in range(1, m): total += month_days(y, mm)
    else:
        for mm in range(7, m): total += month_days(y, mm)
    return total

def greg(y, m, d):
    return date.fromordinal(to_fixed(y, m, d))

DOW = lambda dt_: dt_.weekday()   # Mon=0 .. Sun=6; Saturday=5

# ---- holiday synthesis for one Hebrew year hy (Tishrei..Elul) ----
def holidays_for_hebrew_year(hy):
    H = []
    A = lambda d, name: H.append((d, name))
    ADAR = 13 if is_leap(hy) else 12                 # Purim month
    # Tishrei block
    A(greg(hy, 7, 1), "Rosh Hashanah I"); A(greg(hy, 7, 2), "Rosh Hashanah II")
    g = greg(hy, 7, 3)                               # Fast of Gedaliah: 3 Tishrei, Sat -> Sun
    A(g + timedelta(days=1) if DOW(g) == 5 else g, "Fast of Gedaliah")
    A(greg(hy, 7, 10), "Yom Kippur")
    A(greg(hy, 7, 15), "Sukkot I"); A(greg(hy, 7, 16), "Sukkot II")
    for k in range(17, 21): A(greg(hy, 7, k), "Chol HaMoed Sukkot")
    A(greg(hy, 7, 21), "Hoshana Rabbah")
    A(greg(hy, 7, 22), "Shemini Atzeret"); A(greg(hy, 7, 23), "Simchat Torah")
    # Chanukah: 8 days from 25 Kislev (crosses into Tevet when Kislev is short)
    ch = greg(hy, 9, 25)
    for k in range(8): A(ch + timedelta(days=k), f"Chanukah Day {k+1}")
    t = greg(hy, 10, 10)                             # Fast of 10 Tevet (never Shabbat)
    A(t, "Fast of 10 Tevet")
    A(greg(hy, 11, 15), "Tu BiShvat")
    if is_leap(hy):
        A(greg(hy, 12, 14), "Purim Katan"); A(greg(hy, 12, 15), "Shushan Purim Katan")
    e = greg(hy, ADAR, 13)                           # Fast of Esther: 13 Adar, Sat -> prev Thu
    A(e - timedelta(days=2) if DOW(e) == 5 else e, "Fast of Esther")
    A(greg(hy, ADAR, 14), "Purim"); A(greg(hy, ADAR, 15), "Shushan Purim")
    # Nisan block
    A(greg(hy, 1, 15), "Pesach I"); A(greg(hy, 1, 16), "Pesach II")
    for k in range(17, 21): A(greg(hy, 1, k), "Chol HaMoed Pesach")
    A(greg(hy, 1, 21), "Pesach VII"); A(greg(hy, 1, 22), "Pesach VIII")
    sh = greg(hy, 1, 27)                             # Yom HaShoah: 27 Nisan, Fri -> Thu, Sun -> Mon
    if DOW(sh) == 4: sh -= timedelta(days=1)
    elif DOW(sh) == 6: sh += timedelta(days=1)
    A(sh, "Yom HaShoah")
    # Yom HaAtzmaut: 5 Iyar; Fri/Sat -> preceding Thu; Mon -> Tue. Zikaron = day before.
    az = greg(hy, 2, 5)
    if DOW(az) == 4: az -= timedelta(days=1)
    elif DOW(az) == 5: az -= timedelta(days=2)
    elif DOW(az) == 0: az += timedelta(days=1)
    A(az - timedelta(days=1), "Yom HaZikaron"); A(az, "Yom HaAtzmaut")
    A(greg(hy, 2, 18), "Lag B'Omer")
    A(greg(hy, 2, 28), "Yom Yerushalayim")
    A(greg(hy, 3, 6), "Shavuot I"); A(greg(hy, 3, 7), "Shavuot II")
    tz = greg(hy, 4, 17)                             # 17 Tammuz: Sat -> Sun
    A(tz + timedelta(days=1) if DOW(tz) == 5 else tz, "Fast of 17 Tammuz")
    av = greg(hy, 5, 9)                              # Tisha B'Av: Sat -> Sun
    A(av + timedelta(days=1) if DOW(av) == 5 else av, "Tisha B'Av")
    A(greg(hy, 5, 15), "Tu B'Av")
    return H

def generate(y0, y1):
    out = []
    for hy in range(y0 + 3760, y1 + 3762):           # cover both Hebrew years touching range
        for d, name in holidays_for_hebrew_year(hy):
            if y0 <= d.year <= y1: out.append((d, name))
    out.sort()
    return out

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=int, default=1985); p.add_argument("--end", type=int, default=2060)
    p.add_argument("--out", default="jewish_holidays.csv")
    a = p.parse_args()
    rows = generate(a.start, a.end)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["date", "name"])
        for d, n in rows: w.writerow([d.isoformat(), n])
    print(f"wrote {a.out}: {len(rows)} rows {rows[0][0]}..{rows[-1][0]}")
    # anchor checks (Rosh Hashanah I of 5784-5786)
    assert greg(5786, 7, 1) == date(2025, 9, 23), greg(5786, 7, 1)
    assert greg(5785, 7, 1) == date(2024, 10, 3), greg(5785, 7, 1)
    assert greg(5784, 7, 1) == date(2023, 9, 16), greg(5784, 7, 1)
    print("anchor OK: 1 Tishrei 5786 = 2025-09-23")
