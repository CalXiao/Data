# SKILL — generating the Japan (Tokyo bank) holiday calendar

`generate_japan.py` produces the Tokyo banking calendar used for JPY swap date
math. For **years ≥ 2016 it is an exact port** of the pricer engine's
`holidaysJP()` (`Vol Pricer\src\engine.js`) — validated 938/938 dates identical
on 2026-07-06. For 1985–2015 it adds the historical rules the engine never
needed (the engine's history only has to cover the Citi/BOJ data window).

## Generation rules

**Equinox days** (exact for 1980–2099):
`vernal = ⌊20.8431 + 0.242194·(y−1980)⌋ − ⌊(y−1980)/4⌋` (March);
`autumnal = ⌊23.2488 + 0.242194·(y−1980)⌋ − ⌊(y−1980)/4⌋` (September).

**Fixed / nth-Monday holidays with era transitions**:

| holiday | rule |
|---|---|
| Coming of Age | Jan 15 ≤1999; 2nd Mon Jan ≥2000 (Happy Monday) |
| Emperor's Birthday | Apr 29 ≤1988 (Showa); Dec 23 1989–2018; **none 2019**; Feb 23 ≥2020 |
| Apr 29 | holiday every year — relabeled Greenery Day 1989, Showa Day 2007 |
| Greenery Day (May 4) | explicit ≥2007; 1986–2006 arises via the citizens' rule |
| Marine Day | none <1996; Jul 20 1996–2002; 3rd Mon Jul ≥2003; 2020→Jul 23, 2021→Jul 22 |
| Mountain Day | Aug 11 ≥2016; 2020→Aug 10, 2021→Aug 8 |
| Respect for the Aged | Sep 15 ≤2002; 3rd Mon Sep ≥2003 |
| Health-Sports / Sports | Oct 10 ≤1999; 2nd Mon Oct ≥2000 (renamed 2020); 2020→Jul 24, 2021→Jul 23 |
| one-offs | 1989-02-24 Showa funeral; 1990-11-12 & 2019-10-22 enthronements; 1993-06-09 royal wedding; 2019-05-01 accession |

Plus fixed: Jan 1, Feb 11, Mar/Sep equinoxes, May 3, May 5, Nov 3, Nov 23.

**Derived-holiday rules, applied in this order**:
1. **Citizens' holiday** (≥1986): a non-holiday weekday sandwiched between two
   national holidays becomes one (creates May 4 pre-2007 and the occasional
   Silver-Week Sep 22, e.g. 2026-09-22).
2. **Substitute holiday** (≥1973): a national holiday falling **Sunday** moves
   to the next non-holiday day (can chain through Golden Week, e.g. 2026-05-06).
3. **Bank closures** (not national holidays, no substitutes): Jan 2, Jan 3, Dec 31.

## Scope notes

- This is the **bank** calendar. The Seasonals duckdb 'JP' region is **XTKS**
  (TSE closes, weekday-falling only, incl. the 2020-10-01 exchange outage) —
  exported at `..\exchange_db_exports\jp_xtks_holidays.csv`; don't mix them.
- Engine ↔ generator contract: ≥2016 the two must diff empty (same node-dump
  recipe as `..\us_sifma\SKILL.md`). Pre-2016 only the generator is authoritative.

## Regenerate

```
py generate_japan.py --start 1985 --end 2060   # -> japan_holidays.csv (date,name)
```
