# SKILL — generating the US SIFMA (bond market) holiday calendar

`generate_us_sifma.py` is an **exact port** of the pricer engine's `holidays()`
(`Vol Pricer\src\engine.js` — the parity-tested runtime copy). Validated
836/836 dates identical for 1985–2060 on 2026-07-06. The JS stays the runtime
source; regenerate this CSV after any engine calendar change and re-diff.

## Generation rules

- **Nth-weekday holidays**: MLK (3rd Mon Jan), Presidents (3rd Mon Feb),
  Memorial (LAST Mon May), Labor (1st Mon Sep), Columbus (2nd Mon Oct),
  Thanksgiving (4th Thu Nov). SIFMA closes on Columbus/Veterans (NYSE does not
  — see `..\exchange_db_exports\us_nyse_holidays.csv`).
- **Fixed-date holidays with observance shifts**:
  - New Year (Jan 1) and Veterans (Nov 11): Sunday → next Monday;
    **Saturday → not observed at all** (no Friday shift in this convention).
  - July 4 and Juneteenth (Jun 19): Saturday → preceding Friday, Sunday → Monday.
  - Christmas (Dec 25): Saturday → Dec 24, Sunday → Dec 26.
  - **Juneteenth only from 2022** (first federal/SIFMA observance year in the engine).
- **Good Friday** = Easter − 2 days, Easter via the anonymous Gregorian
  computus (Gauss-family algorithm; ported verbatim from the JS, same integer
  ops). **Quirk kept deliberately**: Good Friday is *dropped entirely when it
  falls on the first Friday of its month* — the NFP-release Good Fridays on
  which the bond market opens (1985, 1988, 1994, 1996, 1999, 2007, 2010, 2012,
  2015, 2021, 2023, 2026...). This models those days as full trading days; in
  reality SIFMA recommends an early close — see the early-close file below.

## Companion data

- `..\exchange_db_exports\us_sifma_early_closes.csv` — 145 SIFMA recommended
  early closes 1985–2030 (from the Seasonals duckdb; 2pm ET closes around
  July 4/Thanksgiving/Christmas/New Year + NFP Good Fridays).

## Regenerate

```
py generate_us_sifma.py --start 1985 --end 2060   # -> us_sifma_holidays.csv (date,name)
```

Validation recipe (used at migration): dump `ENG.holidays(y)` for the range
from `engine.js` under node, diff date sets — must be empty before committing
either side.
