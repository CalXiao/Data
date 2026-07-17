# SKILL — generating the Jewish holiday calendar procedurally

`generate_jewish.py` derives every date from first principles — classical
Hebrew-calendar arithmetic (Dershowitz & Reingold, *Calendrical Calculations*),
no libraries, exact for the whole 1985–2060 range (and beyond). Validated
**1:1 (2,058/2,058 rows)** against the pre-existing Seasonals duckdb JEWISH
region on 2026-07-06.

## 1. The Hebrew calendar engine

The calendar is lunisolar and fully deterministic:

- **Leap years**: 7 per 19-year Metonic cycle — year `y` is leap iff
  `(7y + 1) mod 19 < 7` (leap years add a 13th month, Adar I).
- **Molad (mean new moon) of Tishrei** fixes Rosh Hashanah. Elapsed months to
  year `y`: `235·⌊(y−1)/19⌋ + 12·((y−1) mod 19) + ⌊(7·((y−1) mod 19)+1)/19⌋`.
  Each month = 29d 12h 793 parts (1 hour = 1080 parts); accumulate from the
  epoch molad (5h 204p into the epoch day).
- **Dechiyot (postponements)** applied to the molad day:
  1. *Molad zaken*: molad at/after 18h (noon) → postpone 1 day (`parts ≥ 19440`);
  2. Tuesday molad ≥ 9h 204p in a **common** year → postpone (`day%7==2, parts ≥ 9924`);
  3. Monday molad ≥ 15h 589p after a **leap** year → postpone (`day%7==1, parts ≥ 16789`);
  4. *Lo ADU rosh*: Rosh Hashanah may not fall Sunday/Wednesday/Friday → +1 day.
- **Year length** (`elapsed(y+1) − elapsed(y)`) ∈ {353,354,355} or {383,384,385};
  355/385 ⇒ long Marcheshvan (30d), 353/383 ⇒ short Kislev (29d). All other
  month lengths are fixed (30/29 alternating; Adar II 29d; Adar 29d in common
  years, Adar I 30d in leap years).
- **Epoch**: RD −1373428 with `date.fromordinal` (Python's ordinal *is* the
  Rata Die number). Anchors asserted in-code: 1 Tishrei 5784/85/86 =
  2023-09-16 / 2024-10-03 / 2025-09-23.

## 2. Holiday placement (Hebrew dates + observance shifts)

Fixed Hebrew dates: Rosh Hashanah 1–2 Tishrei; Yom Kippur 10 Tishrei; Sukkot
15–16, Chol HaMoed 17–20, Hoshana Rabbah 21, Shemini Atzeret 22, Simchat Torah
23 Tishrei (diaspora); Chanukah 8 days from 25 Kislev (crosses into Tevet when
Kislev is short); 10 Tevet; Tu BiShvat 15 Shevat; Purim 14 / Shushan Purim 15
Adar (Adar **II** in leap years; Purim Katan 14–15 Adar I leap years only);
Pesach 15–16 / 17–20 / 21–22 Nisan; Lag B'Omer 18 Iyar; Yom Yerushalayim
28 Iyar; Shavuot 6–7 Sivan; Tu B'Av 15 Av.

Weekday-driven shifts (all confirmed by the db reconciliation):

| holiday | rule |
|---|---|
| Fast of Gedaliah (3 Tishrei) | Shabbat → Sunday |
| Fast of Esther (13 Adar) | Shabbat → preceding **Thursday** |
| Fast of 17 Tammuz, Tisha B'Av | Shabbat → Sunday |
| Yom HaShoah (27 Nisan) | Friday → Thursday; Sunday → Monday |
| Yom HaAtzmaut (5 Iyar) | Fri → Thu; Sat → Thu; Mon → Tue (Yom HaZikaron = day before, always) |

## 3. Regenerate / extend

```
py generate_jewish.py --start 1985 --end 2060   # -> jewish_holidays.csv (date,name)
```
Gregorian year G is covered by Hebrew years G+3760 (Jan–autumn) and G+3761
(autumn–Dec); the generator emits both and filters. The arithmetic is exact
indefinitely — extending the range is just a flag.
