# Citi Velocity Rates Pipeline — Knowledge Base

Single reference for how the data is pulled, what exists, and how it's stored/analyzed.
Project root: `C:\Users\cxiao\Documents\Pyth\Data Feeds\CitiVelocity\historical` (moved from `...\Velocity Pull` during the 2026 Data Feeds consolidation). Last verified: 2026-06-23.

---

## 1. TL;DR / mental model

```
Citi Velocity Historical Data API  (EOD daily, REST, OAuth2)
        │  citivelocity_rates.py   (client: auth, retries, parsing)
        ▼
discover_rates.py   → discovery/rates_tag_inventory.parquet   (what we're entitled to)
        │  curate_selection.py  (Calvin's curation rules)
        ▼
discovery/rates_selection_inventory.parquet   (the 6,342-tag pull list; was 6,119 before the 1y-fwd chain was extended to 20y1y)
        │  rates_pipeline.py backfill / daily
        ▼
data/  (partitioned Parquet, long format)  +  rates.duckdb  (views + rv_presets)
        │  analytics.py   (curves, flies, spreads, forwards)
        ▼
dashboard.py → rates_dashboard.html   ·   explore_rates.py → *.html
```

Key facts:
- The API is **EOD/daily** historical time series. Levels (swap/forward rates, bond
  yields) are in **percent**; vols/spreads/carry in **bp**.
- Data **cannot be redistributed** (Citi terms). Keep internal.
- Runs only where there's Citi network entitlement (Calvin's machine), not in sandboxes.

---

## 2. The API

### Endpoints (base `https://api.citivelocity.com/markets`)
| Purpose | Path (POST unless noted) |
|---|---|
| OAuth2 token | `/cv/api/oauth2/token` |
| Data (time series) | `/analytics/chartingbe/rest/external/authed/data` |
| Metadata (date ranges) | `/analytics/chartingbe/rest/external/authed/metadata` |
| Tag listing | `/analytics/chartingbe/rest/external/authed/taglisting` |
| Tag browsing | `/analytics/chartingbe/rest/external/authed/tagbrowsing` |

### Auth
OAuth2 **client-credentials**: POST `client_id`, `client_secret`,
`grant_type=client_credentials`, `scope=/api` (form-encoded) → bearer token,
`expires_in` ~3600s. Data calls send `Authorization: Bearer <token>` header **and**
`?client_id=<id>` query param. The client auto-fetches/refreshes; creds live in
`secrets.env` (gitignored), auto-loaded by `rates_pipeline.py`.

### Data request/response
Request JSON: `{"startDate":20160101,"endDate":20260623,"tags":[...],"frequency":"DAILY","pricePoints":"C"}`.
- `frequency`: MONTHLY/WEEKLY/DAILY (EOD, no history cap) · HOURLY/MI10/MI01 (intraday, capped). We use **DAILY**.
- `pricePoints`: `C` (close, default) or `OHLC`.
- Response: `{"frequency":"DAILY","body":{tag:{"x":[dates],"c":[values],"type":"SERIES"}},"status":"OK"}`.
  `x` dates are ints: DAILY=`yyyyMMdd`. Per-tag errors come back as `{"type":"ERROR","message":...}`.
- Empty `x`/`c` for a valid tag = "no data" (normal, not an error).

### Limits (per ~24h, standard charting tags)
| API | Max calls | Max items (tags requested) | Max per request | Notes |
|---|---|---|---|---|
| Data | 10,000 | 100,000 | **100 tags/call** | 1 req/sec, server-queued (no client sleep) |
| Metadata | 10,000 | **100,000** | 1000 tags/call | this is the one we exhausted in discovery |
| Tag listing | **100** | 100,000 | — | so list once per dataset, filter in memory |
| Tag browsing | 1,000 | 1,000 | — | |

Our full backfill = ~62 data calls / 6,119 items → far inside limits. HTTP usually 200
even on errors (check `status`/`type`). Service can be down ≤10 min anytime; weekend
maintenance Fri 6pm–Sun 6pm NY (we schedule weekdays only).

---

## 3. Tag grammar (learned from discovery)

Tags are dotted: `CATEGORY.SUBCATEGORY.<...>`. For rates, `CATEGORY=RATES`.
**The full universe for our 6 ccys was ~526,892 tags** — huge only because Citi
pre-computes roll/carry, curves, flies. Raw building blocks are tiny.

### Swaps: `RATES.OIS` (RFR) and `RATES.SWAP_LIBOR` (legacy IBOR)
Field layout: `RATES.OIS.<CCY_INDEX>.<PRODUCT>.<...>`
- **Currency is fused with the index** in OIS: `USD_SOFR`, `USD_FEDFUND`, `EUR_EUROSTR`,
  `EUR_EONIA`, `GBP_SONIA`, `AUD_AONIA`, `CAD_CORRA`, `JPY_TONAR`, `JPY_TONAR_JSCC`,
  `JPY_TONAR_LCH`. In SWAP_LIBOR the field is the bare ccy: `USD`,`EUR`,`GBP`,`JPY`,`AUD`,`CAD`,`JPY_LCH`.
- `<PRODUCT>` (f3) values & layouts:
  - `PAR.<tenor>` — spot par swap rate (the curve). e.g. `RATES.OIS.USD_SOFR.PAR.10Y`
  - `FWD.<start>.<tail>` — forward-starting swap. e.g. `RATES.OIS.USD_SOFR.FWD.5Y.5Y` = 5y5y
  - `ROLL_CARRY.<horizon>.<start>.<tail>.<MEASURE>` — MEASURE ∈ {CARRY, ROLL, TOTAL_CARRY}; horizon e.g. `3M`
  - `CURVES.<...>` — pre-computed slopes (we don't pull; build ourselves)
  - `BFLY.<t1>.<t2>.<t3>` — pre-computed butterflies (we don't pull; build ourselves)
  - `SWAP_SPREAD.<tenor>` — swap spread vs govie (bp)

### Swaption vol: `RATES.VOL`
`RATES.VOL.<CCY>.<FAMILY>.<QUOTE>[.<MEASURE>].<expiry>.<tail>`
- FAMILY: `ATM` (legacy/Libor-underlying), `ATM_RFR` (RFR-underlying), plus `REALIZED`,
  `REALIZED_RFR`, `VOL_RATIO`, `VOL_RATIO_RFR` (not pulled).
- QUOTE: `NORMAL` (bp vol — what we use), `BLACK`, `PREMIUM`, `FWDPREMIUM`.
- NORMAL has a MEASURE field `ANNUAL`/`DAILY`: `...ATM_RFR.NORMAL.ANNUAL.<exp>.<tail>` (8 fields).
- FWDPREMIUM has no measure: `...ATM_RFR.FWDPREMIUM.<exp>.<tail>` (7 fields).
- **There is NO skew / risk-reversal / strike data** in RATES.VOL — ATM only. (Calvin's
  "ATM + risk reversals" request can't be met from this dataset; RR would need the Vol
  & Swap Pricer or another entitlement.)
- Legacy `ATM` series go **stale post-transition** (USD ATM froze ~2025-02-21); use `ATM_RFR`.

### Mid-curve: `RATES.MIDCURVES`
`RATES.MIDCURVES.<CCY>.<TYPE>.<MEASURE>.<expiry>.<combo>` — TYPE ∈ {OPT_PAY,OPT_REC,OPT_STR},
MEASURE ∈ {VOL,PRICE}, combo like `5Y5Y`,`10Y10Y`. We pull **OPT_STR + VOL** only.
CCY field includes `EUR`,`EURIBOR`,`GBP`,`USD`,`USD_SOFR`.

### Bonds / inflation: `RATES.TSY`
Nearly empty in our entitlement — only **TIPS** series: `RATES.TSY.TIPS.USD.<tenor>.<MEASURE>`
where MEASURE ∈ {YIELD, BREAKEVENS, PRICE, ASS_SOFR, CARRY, CARRY_BE}. **Nominal OTR
govvie yields are NOT available** (RATES.SOV / SOV_CMT returned 0). Bonds deferred.

### Entitled datasets NOT in original discovery (added 2026-06-27)
The first discovery run omitted these four datasets from `DEFAULT_DATASETS`. They are
entitled and accessible via `browse()` but were never listed. Run this once to add their
tags to the inventory — requires **Citi network** (office or Citi VPN; `webservice.citivelocity.com`
is only resolvable on the internal network) and ideally a **weekday** (maintenance window
Fri 6pm–Sun 6pm NY may also block the endpoint):

```bash
python -c "
import os, pandas as pd
for line in open('secrets.env'): k,v=line.strip().split('=',1) if '=' in line.strip() and not line.startswith('#') else (None,None); os.environ.setdefault(k.strip(),v.strip()) if k else None
from citivelocity_rates import CitiVelocityClient
from discover_rates import parse_tag
client = CitiVelocityClient()
existing = pd.read_csv('discovery/rates_tag_inventory.csv', low_memory=False)
existing_tags = set(existing['tag'])
new_rows = []
for ds in ['RATES.INFLATION','RATES.BASIS_SWAPS','RATES.XCCY_SWAP','RATES.OIS_INVOICESPREAD']:
    tags = client.list_tags(ds); print(f'{ds}: {len(tags)} tags')
    new_rows += [dict(parse_tag(t), query_dataset=ds) for t in tags if t not in existing_tags]
if new_rows:
    merged = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True).drop_duplicates('tag').reset_index(drop=True)
    merged.to_csv('discovery/rates_tag_inventory.csv', index=False)
    print(f'Saved {len(merged):,} tags total')
"
python curate_selection.py           # rebuilds rates_selection_inventory.csv
python rates_pipeline.py backfill    # pulls only the new tags (existing tags already stored)
```

After backfill, update KNOWLEDGE.md §4 with new product counts.

Known tag grammar for these datasets (from browse() calls):
- `RATES.INFLATION.SWAP.{CCY_INDEX}.{TENOR}Y` — CCY_INDEX: USD_CPURNSA, EUR_CPTFEMU, GBP_UKRPI, AUD_AUCPI, JPY_JCPNGENF
- `RATES.BASIS_SWAPS.{3S6S|3S1S}_BASIS.{CCY}.{TENOR}Y` — CCY: USD, EUR, GBP, AUD
- `RATES.XCCY_SWAP.{CCY}.USD.PAR.{TENOR}Y` — CCY: JPY, AUD, GBP, EUR, CAD
- `RATES.OIS_INVOICESPREAD.USD_SOFR_FRONTMONTH.{TENOR}` — TENOR: 2Y,3Y,5Y,10Y,ULTRA10Y,20Y_BOND,30Y_BOND

Curation rules for these new datasets are already in `curate_selection.py` (added 2026-06-27).
Pipeline dimension mapping is already in `rates_pipeline.py` `build_selection()`.
CV tags for all four products are in `BBG_Dict.py` `CV` dict (added 2026-06-27).

### Discovery quirks worth remembering
- Tag listing is **1 call/dataset** (100/day cap) — never per ccy.
- Metadata is **100k items/day** — easy to exhaust; annotate samples only.
- `discover_rates.py --from-inventory <csv>` rebuilds summaries offline (0 API calls).
- The taglisting/data endpoints redirect to `webservice.citivelocity.com` which is **only resolvable on the Citi internal network** (office or Citi VPN). The OAuth token endpoint `api.citivelocity.com` is public (token succeeds), but data calls fail with NameResolutionError if not on Citi network.
- Maintenance window (Fri 6pm–Sun 6pm NY) may additionally block endpoints; run on weekdays when on Citi network.

---

## 4. What has been pulled (current store)

**14,574,674 rows · 6,119 tags · 2016-01-01 → 2026-06-23 · ~89 MB.**
Currencies: USD, EUR, GBP, AUD, JPY, CAD.

| product | tags | rows | notes |
|---|---|---|---|
| forward_swap | 701 | 1.85M | quoted forwards (start×tail), Calvin's grid |
| roll_carry | 2,076 | 5.45M | 3M horizon, CARRY/ROLL/TOTAL_CARRY, on the fwd grid |
| vol | 2,502 | 5.18M | ATM + ATM_RFR, NORMAL + FWDPREMIUM (CAD has none) |
| swap_par | 153 | 0.40M | spot par curve |
| swap_spread | 59 | 0.14M | published swap spreads |
| midcurve | 585 | 1.51M | OPT_STR · VOL |
| bond_tips | 24 | 0.05M | USD TIPS only |

Curation decisions (see `config.yaml`, `curate_selection.py`):
- **Swap basis per ccy**: RFR live where it trades — USD=SOFR, GBP=SONIA, CAD=CORRA,
  JPY=TONAR (incl. JSCC/LCH clearing variants for PAR). EUR & AUD = **IBOR** (SWAP_LIBOR)
  because they're mid-transition; their RFR (EUROSTR/AONIA) was excluded. GBP keeps **both**
  SONIA (OIS) and GBP Libor. Dropped: USD_FEDFUND, EUR_EONIA, and USD/JPY/CAD/JPY-LCH Libor.
- **Forwards**: Calvin's explicit start×tail grid (3m..20y tails).
- **PAR tenors**: 6M,1Y,2Y…10Y,12Y,15Y,20Y,25Y,30Y,40Y.
- **Carry**: 3M horizon, CARRY+ROLL+TOTAL_CARRY, restricted to the forward grid.
- **Swap spreads**: 1Y,2Y,3Y,5Y,7Y,10Y,20Y,30Y.
- **Vol**: ATM bp (NORMAL/ANNUAL) + Forward Premium, both ATM & ATM_RFR.
- **Midcurve**: OPT_STR + VOL, all tenors.
- ~19 tags are validly empty (transitioning-ccy 18M-start Libor carry, 1 long USD midcurve).

---

## 5. Storage schema

**Source of truth = partitioned Parquet** under `data/`, hive layout:
`data/product=<p>/currency=<ccy>/year=<yyyy>/data.parquet`. Long/tidy, one row per
(tag, date). Upsert key = **(tag, date)** — daily re-pull self-heals restatements.

Columns:
| col | type | notes |
|---|---|---|
| date | TIMESTAMP | GMT, normalized to day |
| tag | VARCHAR | raw Citi tag (natural key) |
| value | DOUBLE | level in % (rates) or bp (vol/spread/carry) |
| field | VARCHAR | `close` |
| currency, product, basis | VARCHAR | parsed dims; basis ∈ RFR/IBOR/LEGACY |
| index_name | VARCHAR | SOFR/SONIA/… (null for IBOR/vol) |
| expiry, tenor | VARCHAR | tenor tokens ('5Y','10Y'); expiry=fwd start / vol option expiry |
| moneyness_bp | DOUBLE | 0 for ATM vol, null else |
| vol_type | VARCHAR | NORMAL / FWDPREMIUM (vol) |
| measure | VARCHAR | CARRY/ROLL/TOTAL_CARRY (carry); ANNUAL (vol); TIPS measure; OPT_STR (midcurve) |
| source_freq, ingested_at | | audit |

> Schema gotcha (fixed): all-null dimension columns can serialize as Arrow `null` type
> and break DuckDB cross-partition queries. `rates_pipeline._enforce_schema` casts dims
> to string on every write to prevent this.

**DuckDB (`rates.duckdb`)** = views over the Parquet glob (no data copy):
- `rates` — everything (long)
- `swap_par`, `forward_swap`, `vol_atm` — filtered views
- `rv_presets` — **materialized** analytics table (451,594 rows): tidy
  (date, currency, signal, kind, unit, value). kinds: slope, fly_equal, fwd_outright,
  swap_spread. Rebuilt after every ingest.

---

## 6. How to run

```bash
cd "C:\Users\cxiao\Documents\Pyth\Data Feeds\CitiVelocity\historical"
pip install -r requirements.txt           # pandas, requests, pyarrow, pyyaml, duckdb
# creds auto-load from secrets.env (CITI_CLIENT_ID / CITI_CLIENT_SECRET)

python rates_pipeline.py select           # preview the 6,342-tag pull list (offline)
python rates_pipeline.py backfill          # one-time full history → data/ + rates.duckdb
python rates_pipeline.py daily             # incremental lookback-upsert (7 bd) + rebuild
python rates_pipeline.py duckdb            # rebuild views + rv_presets only

python dashboard.py                        # regenerate rates_dashboard.html
python explore_rates.py                    # regenerate universe explorer
```

Daily schedule: `register_task.bat` (Windows Task Scheduler, weekdays 07:00 →
`run_daily.bat` → logs to `logs\`). Change universe by editing `curate_selection.py`,
re-running it, then `backfill`.

---

## 7. Querying the data

The `rates` view uses an **absolute** parquet glob (baked in by `build_duckdb`),
so you can open `rates.duckdb` from any working directory. If you ever relocate
this folder, re-run `python rates_pipeline.py duckdb` once to regenerate the view
with the new path. (Legacy DBs built before this change used a relative `./data`
glob that only resolved when cwd was this folder — the first rebuild fixes it.)

```python
import duckdb
con = duckdb.connect(r"C:\Users\cxiao\Documents\Pyth\Data Feeds\CitiVelocity\historical\rates.duckdb")
con.execute("SELECT * FROM rates WHERE product='swap_par' AND currency='USD' LIMIT 20").df()
con.execute("SELECT * FROM rv_presets WHERE currency='USD' AND kind='slope'").df()
```

Analytics engine (tidy DataFrames; levels %, slopes/flies bp):
```python
import analytics as A
con = A.connect(A.load_config())
A.swap_curve(con, "USD")                              # date × tenor PAR levels
A.slope(con, "USD", "2Y", "10Y")                      # 2s10s, bp
A.butterfly(con, "USD", "5Y","10Y","30Y", weighting="equal")   # equal|pca|regression
A.forward_curve(con, "USD", "5Y")                     # 5y-fwd across tails
A.forward_ladder(con, "USD", "1Y")                    # n-fwd-1y ladder
A.fwd_point(con, "USD", "5Y", "5Y")                   # 5y5y outright
A.swap_spread(con, "USD")                             # swap spreads, bp
```

Conventions: basis auto (RFR where it exists; IBOR for EUR/AUD); JPY uses primary TONAR
(clearing variants excluded from curve math). Butterfly default equal (-1/+2/-1); `pca`
= sum-zero, level- & slope-neutral curvature weights; `regression` = belly residual vs
wings. PCA/regression are full-sample (in-sample → mild lookahead; pass `window=N` for
trailing). DuckDB reserved words to avoid as aliases: `start`, `end`, `rows`, `days`,
`last`, `first` — quote or rename.

---

## 8. Files

| file | role |
|---|---|
| `citivelocity_rates.py` | API client (OAuth, retries, gzip, parsing) |
| `discover_rates.py` | enumerate entitled tags → inventory/grammar |
| `curate_selection.py` | apply curation rules → selection inventory |
| `explore_rates.py` | interactive HTML universe explorer |
| `rates_pipeline.py` | backfill / daily ingest, storage, DuckDB build, selection mapping |
| `analytics.py` | RV engine + rv_presets materialization |
| `dashboard.py` | RV monitor HTML generator |
| `config.yaml` | curated config (storage paths, ingest, products) |
| `secrets.env` | credentials (gitignored) |
| `pipeline_design.md` / `README.md` | architecture spec / usage |
| `data/`, `rates.duckdb`, `discovery/`, `logs/` | data + db + discovery outputs + run logs |

---

## 9. Open items / caveats

- **Bonds**: nominal govvie yields not entitled (only USD TIPS). Raise with Citi sales.
- **Vol skew/RR**: not available in RATES.VOL (ATM only).
- **Parallel IBOR history**: currently RFR-only for USD/GBP/CAD/JPY; SWAP_LIBOR can be
  re-enabled in `curate_selection.py` for long pre-transition history.
- **Z-scores** in the dashboard are raw-level, full-window — over 5Y/10Y windows a high
  z partly reflects the rate-regime shift (ZIRP→hiking), not pure RV richness. Detrended
  / carry-adjusted variants are a future enhancement.
- **EUR/AUD** swap curves are on **IBOR** basis (mid-transition); mind the basis when
  comparing to USD/GBP RFR curves.
