# SKILL — generating the Canada (Toronto bank) holiday calendar

`generate_canada.py` is an exact port of the pricer engine's `holidaysCA()`
(`Vol Pricer\src\engine.js`), used for CAD CORRA swap date math (T+1 spot,
semi-annual, Toronto Modified Following). Python = engine verified 853/853
(1985–2060) on 2026-07-06. **No independent reference set exists** — the
Seasonals duckdb has no CA region — so unlike UK/Jewish this calendar is
rules-only; spot-check against a Schedule I bank holiday schedule when
convenient and record any corrections here AND in engine.js.

## Generation rules

- **Weekend observance**: fixed-date holidays (New Year, Canada Day, T&R Day,
  Remembrance Day) falling Sat/Sun observe the following Monday.
- **Nth-Monday holidays**: Family Day (3rd Mon Feb, **from 2008** — Ontario
  introduction), Civic Holiday (1st Mon Aug), Labour Day (1st Mon Sep),
  Thanksgiving CA (2nd Mon Oct).
- **Victoria Day**: the Monday on or before May 24.
- **Good Friday**: Easter − 2d (computus as UK/US). No Easter Monday for banks.
- **National Day for Truth & Reconciliation**: Sep 30, **from 2021** (federal;
  banks close — note the TSX does not, if this set is ever reused for equities).
- **Christmas + Boxing Day chained pair**: Dec 25 Sat → obs Mon 27 + Tue 28;
  Sun → Mon 26 + Tue 27; Fri → Fri 25 + Mon 28; else both actual days.

## Known scope judgments

- Toronto **bank** calendar (matters for CORRA swap rolls), not Quebec
  (no St-Jean-Baptiste) and not provincial variants.
- Pre-2008 Family Day / pre-2021 T&R correctly absent in history.

## Regenerate

```
py generate_canada.py --start 1985 --end 2060   # -> canada_holidays.csv (date,name)
```
