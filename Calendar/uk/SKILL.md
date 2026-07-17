# SKILL — generating the UK (England & Wales bank holiday) calendar

`generate_uk.py` is an exact port of the pricer engine's `holidaysUK()`
(`Vol Pricer\src\engine.js`), used for GBP SONIA swap date math (T+0 spot,
London Modified Following). Triple-validated on 2026-07-06: python = engine
(615/615, 1985–2060) and python = Seasonals duckdb UK region (375/375, 1985–2030,
first attempt — every one-off confirmed).

## Generation rules

- **Substitute rule**: a holiday falling Saturday/Sunday moves to the next
  weekday not already a holiday. Christmas + Boxing Day are placed as a
  *chained pair* (Dec 25 Sat → Mon 27 + Tue 28, etc.).
- **Easter-derived**: Good Friday (Easter − 2d), Easter Monday (Easter + 1d),
  anonymous Gregorian computus.
- **Bank holidays**: Early May (1st Mon May), Spring (last Mon May), Summer
  (last Mon Aug).
- **Moved bank holidays** (jubilees pull Spring into June; VE anniversaries
  move Early May): 1995 & 2020 Early May → May 8; 2002 & 2012 Spring → Jun 4;
  2022 Spring → Jun 2.
- **One-offs**: 1999-12-31 (millennium), 2002-06-03 (golden jubilee),
  2011-04-29 (royal wedding), 2012-06-05 (diamond jubilee), 2022-06-03
  (platinum jubilee), 2022-09-19 (state funeral), 2023-05-08 (coronation).
- **Forward-looking caveat**: future royal events (accessions, funerals,
  jubilees) create new one-offs that must be added by hand in BOTH this file
  and `engine.js` (then re-run the triple diff).

## Regenerate

```
py generate_uk.py --start 1985 --end 2060   # -> uk_holidays.csv (date,name)
```
