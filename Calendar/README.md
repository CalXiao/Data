# Calendar — central calendar hub (created 2026-07-06)

Canonical home for every holiday/event calendar used by the pricers and the
Seasonals stack. **Reference hub only**: the pricer engine
(`Vol Pricer\src\engine.js`) keeps its own embedded, parity-tested copies of
US SIFMA + Japan — the generators here are validated EXACTLY against them
(see each folder's SKILL.md). Nothing at runtime reads these files.

## Layout

| folder | contents | procedural? | validated against |
|---|---|---|---|
| `us_sifma\` | SIFMA govvie full-close calendar, 1985–2060 | ✔ generator + SKILL.md | engine.js `holidays()` — exact, 836/836 |
| `japan\` | Tokyo bank calendar, 1985–2060 | ✔ generator + SKILL.md | engine.js `holidaysJP()` ≥2016 — exact, 938/938 |
| `uk\` | England & Wales bank holidays, 1985–2060 | ✔ generator + SKILL.md | engine.js `holidaysUK()` exact 615/615 AND duckdb UK region exact 375/375 |
| `canada\` | Toronto bank calendar, 1985–2060 | ✔ generator + SKILL.md | engine.js `holidaysCA()` exact 853/853 (no external reference — rules-only) |
| `jewish\` | Full Jewish holiday set, 1985–2060 | ✔ generator + SKILL.md (Hebrew-calendar arithmetic) | Seasonals duckdb JEWISH region — exact, 2,058/2,058 |
| `fomc\` | FOMC decision dates 2026–27 | ✖ (Fed-published) | `sofr_curve_feed.py` FOMC_FALLBACK + SofrTab FOMC_RAW |
| `cb_meetings\` | ALL-bank decision dates (FED/BOE/BOJ/BOC/ECB/RBA) 2026–27 | ✖ (bank-published; annual refresh process in README) | official pages fetched live 2026-07-06 (RBA secondary — verify) |
| `exchange_db_exports\` | NYSE, XTKS, UK/DE/FR/CN holidays + SIFMA early closes, 1985–2030 | ✖ (extracted from Seasonals duckdb; original generator unknown) | — (db IS the source) |

## Scope findings from the migration (important)

- The Seasonals duckdb **'US' region is the NYSE equities calendar**, not SIFMA:
  no Columbus/Veterans, MLK only from 1998, closes on ALL Good Fridays, plus
  event closes (Gloria '85, Nixon '94, 9/11 week, Reagan '04, Ford '07,
  Sandy '12, Bush '18, Carter '25). The pricer's SIFMA calendar additionally
  **drops Good Friday when it is the first Friday of the month** (NFP release).
  Use `us_sifma\` for rates pricing, `us_nyse_holidays.csv` for equities/seasonality.
- The duckdb **'JP' region is XTKS** (TSE closes, weekday-falling only, incl.
  the 2020-10-01 systems-outage halt). Use `japan\` for JPY swap date math.
- The duckdb **JEWISH region reconciles 1:1** with the from-scratch generator in
  `jewish\` — the procedural version is now the db-independent source and
  extends to 2060.
- `Seasonals\calendar_events.duckdb` also holds `ust_auctions` (1,477 rows) and
  the `market_data` schema — left in place (market data, not calendars).

## Consumers

- `Vol Pricer\src\engine.js` — embedded US SIFMA + Japan (runtime; parity-gated).
- `Data Feeds\Bloomberg\sofr_curve_feed.py` — FOMC scrape + fallback (`/fomc`).
- `Vol Pricer\src\SofrTab.jsx` — baked FOMC_RAW + engine SIFMA holidays.
- `Seasonals\seasonality_engine.py` — reads the duckdb directly (unchanged).

## Regenerating

```
py us_sifma\generate_us_sifma.py                 # -> us_sifma_holidays.csv
py japan\generate_japan.py                       # -> japan_holidays.csv
py jewish\generate_jewish.py                     # -> jewish_holidays.csv
py exchange_db_exports\export_from_seasonals_db.py   # re-export from the duckdb
```
All generators are stdlib-only; default range 1985–2060 (`--start/--end`).
