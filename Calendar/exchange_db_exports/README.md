# Exchange/market calendars — extracted, NOT procedurally generated

CSV exports from `Seasonals\calendar_events.duckdb` (`calendar_events` table,
populated 2025-05-20; the original population script is not in the repo —
**the duckdb is the source of truth** for these sets). Re-export any time with
`py export_from_seasonals_db.py`.

| file | region | rows | range | notes |
|---|---|---|---|---|
| `us_nyse_holidays.csv` | US | 416 | 1985–2030 | **NYSE equities** calendar: no Columbus/Veterans, MLK from 1998, ALL Good Fridays, event closes (Gloria '85, Nixon '94, 9/11 week, Reagan '04, Ford '07, Sandy '12, Bush '18, Carter '25). NOT the bond calendar → use `..\us_sifma\`. |
| `us_sifma_early_closes.csv` | US | 145 | 1985–2030 | SIFMA recommended 2pm ET early closes (incl. NFP Good Fridays). Complements `..\us_sifma\`. |
| `uk_holidays.csv` | UK | 375 | 1985–2030 | Bank holidays incl. one-off royal events (jubilees, funerals, coronation) — one-offs make this non-procedural. |
| `de_holidays.csv` | DE | 307 | 1985–2030 | German market holidays (fixed + Easter-derived). Could be made procedural if ever needed past 2030. |
| `fr_holidays.csv` | FR | 268 | 1985–2030 | French market holidays (fixed + Easter-derived). Same note as DE. |
| `jp_xtks_holidays.csv` | JP | 596 | 1997–2030 | **XTKS/TSE closes**, weekday-falling only, incl. 2020-10-01 systems outage. NOT the bank calendar → use `..\japan\`. |
| `cn_holidays.csv` | CN | 605 | 1991–2026 | Chinese exchange holidays — lunar + government-announced Golden Week bridging: inherently non-procedural, and **already stale (ends Oct 2026)**; needs an annual refresh from exchange notices. |
| `jewish_holidays_db.csv` | JEWISH | 2,058 | 1985–2030 | Kept only as the validation snapshot — the procedural generator in `..\jewish\` reconciles 1:1 and extends to 2060; use that. |

The duckdb also contains `ust_auctions` (1,477 rows) and the Seasonals
`market_data` table — event data, not calendars; left in Seasonals.
