# Citi Velocity Rates Pipeline — Design Spec (v0.1)

Status: draft for review. Phase 1 is the ingest/storage pipeline; Phase 2 is the
analytics layer (spreads, curves, flies). Decisions already locked are marked ✓.

## Decisions locked

- **Currencies:** EUR, USD, GBP, AUD, JPY, CAD — expandable via config. ✓
- **Swap basis:** RFR/OIS as the live curve; legacy IBOR/Libor kept as a *separate,
  parallel, un-stitched* series per tenor. No forced splice. ✓
- **Vol:** ATM BPVol (normal/bp vol) + risk reversals defined by ±bp moneyness
  offsets (not % delta). ✓
- **Ingest:** daily lookback-window upsert (re-pull last N business days, upsert on
  key) to self-heal restatements/late prints. ✓
- **Universe breadth:** exact tenors/grids chosen *after* discovery. ← pending
- **Storage:** Parquet (source of truth) + DuckDB (engine). ✓ (layout below)

## Architecture (layers)

```
            ┌─────────────────────────────────────────────────────────┐
  Step 0    │ discover_rates.py  ──►  tag inventory (what exists)        │
            └─────────────────────────────────────────────────────────┘
                              │  (you pick tenors/grids -> config.yaml)
                              ▼
   Raw      ┌─────────────────────────────────────────────────────────┐
  ingest    │ Data API  ──►  normalize to long rows  ──►  Parquet        │
            │   backfill (one-time, from each tag's true start)          │
            │   daily   (lookback-upsert, cron / Task Scheduler)         │
            └─────────────────────────────────────────────────────────┘
                              ▼
  Engine    ┌─────────────────────────────────────────────────────────┐
            │ DuckDB views over the Parquet glob (source = files)        │
            └─────────────────────────────────────────────────────────┘
                              ▼
 Analytics  ┌─────────────────────────────────────────────────────────┐
 (phase 2)  │ swap spreads · swap curves & flies · forward curves & flies│
            │   as DuckDB SQL views / materialized tables                │
            └─────────────────────────────────────────────────────────┘
```

## Storage schema (long / tidy)

One row per (tag, date, field). Long format keeps ingest append-friendly and lets
new dimensions appear without schema migrations.

| column | type | notes |
|---|---|---|
| `date` | DATE | observation date (GMT) |
| `tag` | VARCHAR | raw Citi tag (the natural key) |
| `value` | DOUBLE | the number |
| `field` | VARCHAR | `close` (or `open/high/low` if we ever pull OHLC) |
| `currency` | VARCHAR | parsed |
| `product` | VARCHAR | `swap` / `forward_swap` / `bond` / `vol` |
| `basis` | VARCHAR | `RFR` / `IBOR` (swaps); null elsewhere |
| `index_name` | VARCHAR | SOFR/ESTR/SONIA/TONA/CORRA/BBSW/Libor |
| `expiry` | VARCHAR | vol option expiry / forward start (e.g. `1Y`) |
| `tenor` | VARCHAR | underlying/swap tenor (e.g. `10Y`) |
| `moneyness_bp` | INTEGER | vol RR offset in bp; `0` = ATM |
| `vol_type` | VARCHAR | `NORMAL` (BPVol) etc. |
| `ingested_at` | TIMESTAMP | run timestamp (audit) |
| `source_freq` | VARCHAR | frequency the server actually returned |

Dimension columns are derived from the tag via a parser we finalize once discovery
shows the real grammar. The raw `tag` is always retained, so parsing can be
re-derived later without re-pulling.

### Partition layout

```
data/
  product=swap/        currency=USD/  year=2024/  part-*.parquet
  product=forward_swap/currency=EUR/  year=2024/  ...
  product=bond/        currency=GBP/  year=2024/  ...
  product=vol/         currency=USD/  year=2024/  ...
```

Hive-partitioned by `product / currency / year`. DuckDB reads the whole store with
`read_parquet('data/**/*.parquet', hive_partitioning=true)`. Pruning on product/
currency/year is automatic. Files are written per ingest batch; a light monthly
compaction step merges small daily files (optional).

## DuckDB layer

DuckDB holds **views**, not a second copy of the data — Parquet is the single
source of truth. A persisted `rates.duckdb` carries the view definitions and any
materialized analytics tables.

```sql
CREATE VIEW rates AS
  SELECT * FROM read_parquet('data/**/*.parquet', hive_partitioning=true);

-- wide close panel, one column per tag (handy for curve math)
CREATE VIEW close_wide AS
  PIVOT (SELECT date, tag, value FROM rates WHERE field='close')
  ON tag USING first(value);
```

## Ingest

### Backfill (one-time)
For each configured tag, read its true `startDate` from the **metadata API**, then
pull the full daily history in date-range requests (≤100 tags/call). Write to the
partitioned store. Sizing is comfortably inside the limits (100 tags/call, 10k
calls/day, ~1/sec server-queued — no client-side sleeping needed).

### Daily (incremental, lookback-upsert) ✓
1. Re-pull the last `lookback_business_days` (default 7) for every configured tag.
2. Upsert into the store on key `(tag, date, field, moneyness_bp)` — last write
   wins, so restatements and late publishes overwrite stale values.
3. Optionally (`full_reconcile`) run a periodic deeper re-pull (weekly/monthly) to
   catch restatements older than the lookback window.

Why not pure append: the metadata API explicitly exposes `modifiedTimes`, i.e.
series get revised/appended after first publish. Pure append would silently retain
stale points. The lookback upsert is cheap (a handful of calls) and self-healing.

### Restatement audit (optional)
Keep `ingested_at`; if you want a full revision trail rather than overwrite, we can
switch to an SCD-2 style append (store every observed value with a valid-from
timestamp). Flag if you want this — it roughly doubles vol-surface row counts.

## Currency / basis handling ✓

Per currency we pull the RFR/OIS curve as the live series **and** the legacy IBOR
swaps as a parallel series, tagged by `basis` (`RFR` vs `IBOR`) and `index_name`.
Nothing is stitched, so curve/spread/fly math can choose a basis explicitly and you
never get an artificial jump at the transition date.

Expected live indices (to confirm in discovery): USD→SOFR, EUR→ESTR (+ Euribor
swaps still active), GBP→SONIA, JPY→TONA, CAD→CORRA, AUD→BBSW (still IBOR-style, no
RFR swap market — likely only the BBSW series exists).

## Vol convention ✓

ATM BPVol across the expiry × tenor grid, plus risk reversals as ±bp moneyness
offsets stored in `moneyness_bp` (`0`=ATM, e.g. `±25/±50/±100`). Discovery will
confirm exactly how Citi encodes the non-ATM nodes (the ATM grammar is
`RATES.VOL.<CCY>.ATM[_RFR].NORMAL.ANNUAL.<EXPIRY>.<TENOR>`; we need to see the
non-ATM/RR field to lock the parser and the config offsets).

## Analytics layer (Phase 2) — conventions to settle

These don't block Phase 1, but flagging so we align before building:

- **Swap spreads:** swap rate − govie yield at matched tenor. Confirm: vs OTR
  benchmark or interpolated-to-constant-maturity? Govie yield or asset-swap?
- **Swap curves & flies:** standard slopes (2s10s, 5s30s…) and butterflies
  (2s5s10s…). Fly weighting: equal-weight (−1/+2/−1) by default, or PCA/regression-
  weighted? (You'll likely want both — I'd expose a `weights` arg.)
- **Forward curves & flies:** built from the pulled forward-starting swaps
  (RATES.FORWARD) directly, *or* bootstrapped/implied from the spot curve? You have
  both data sources; I'd default to using the directly-quoted forwards and offer the
  implied-forward calc as a cross-check.
- **Bond/asset-swap spreads:** if you want ASW specifically, we need the matched
  funding/discount convention.

## History caveats

"10 years" is not uniform. Rates EOD generally starts ~2016 (OIS ~2018, FORWARD
~2021, FRA ~2022). We pull each tag from its real inception via metadata rather than
assuming a fixed window — `min_start` per dataset is reported in discovery output.

## Runtime & ops

- Runs on **your** machine/server (needs Citi entitlement; this sandbox can't reach
  the API). I build + verify offline (mock modes); you run against the live API.
- Auth: auto-fetched OAuth2 token via the existing `citivelocity_rates` client.
- Retries/backoff built in (service can be down ≤10 min for maintenance; avoid heavy
  weekend jobs). Times are GMT throughout.
- Scheduling: a `daily` entrypoint suitable for cron / Windows Task Scheduler. I can
  also wire it as a scheduled task in this app if you'd rather it run here — but it
  would still need network access to Citi, so local scheduling is the realistic path.
- **Redistribution:** data pulled from this service cannot be redistributed (Citi
  terms). Keep the store internal.

## Next steps

1. **You run** `discover_rates.py` (creds set) → returns the tag inventory + the VOL
   tree. Send me `rates_dataset_summary.csv` and a slice of `rates_tag_inventory`.
2. We curate the exact tenors / forward grid / vol nodes into `config.yaml`.
3. I finalize the tag parser against the real grammar, then build the storage module
   + `backfill` and `daily` entrypoints.
4. Backfill, validate, schedule daily.
5. Phase 2: analytics views once the data lands.

---

## v0.2 — Post-discovery update (2026-06-22)

Discovery against your entitlement returned **526,892 rates tags**. Key realities:

- **The universe is huge only because Citi pre-computes everything.** Under
  `RATES.OIS` / `RATES.SWAP_LIBOR`, the `f3` product types are `ROLL_CARRY`,
  `BFLY`, `CURVES`, `FWD`, `PAR`, `SWAP_SPREAD`. We ingest only the raw building
  blocks (`PAR` + `FWD`) and compute curves/flies/spreads ourselves.
- **Currency is fused as `<CCY>_<INDEX>`** in OIS (e.g. `USD_SOFR`, `EUR_EUROSTR`,
  `JPY_TONAR_LCH`). Parser handles this.
- **Forward swaps live inside OIS** (`...FWD.<expiry>.<tenor>`), not a separate dataset.
- **AUD has an RFR curve** (`AUD_AONIA`) — correction to the earlier assumption.
- **Bonds gap:** `RATES.SOV/SOV_CMT/TIPS` empty; `RATES.TSY` only TIPS series.
  Nominal govvie yields not entitled. Deferred; swap-vs-govie spreads wait on this.
- **Vol is ATM-only:** `RATES.VOL` has `ATM`/`ATM_RFR`, `REALIZED`, `VOL_RATIO`
  (each +/-RFR) in NORMAL/BLACK/PREMIUM. **No skew / risk-reversals exist.** We
  store ATM NORMAL (BPVol). Note CAD has no swaption vol.

### Curated universe (initial build): ~9,668 tags
| product | tags | source |
|---|---|---|
| swap_par | 352 | `RATES.OIS.<idx>.PAR.<tenor>` (8 indices incl. JPY JSCC/LCH) |
| forward_swap | 6,936 | `RATES.OIS.<idx>.FWD.<exp>.<tenor>` (6 indices; no JSCC/LCH fwds) |
| vol (ATM NORMAL) | 2,380 | `RATES.VOL.<ccy>.ATM[_RFR].NORMAL.ANNUAL.<exp>.<tenor>` (5 ccys) |

Full backfill ≈ 97 API calls; daily refresh similar. Far inside all limits.
Initial build is **RFR-only** (no parallel IBOR `SWAP_LIBOR`) per your latest call —
re-enable in `config.yaml` when you want the long history.

### Pipeline (`rates_pipeline.py`)
Rule-based selection from the discovery inventory (no hardcoded tags). Commands:

```
python rates_pipeline.py select          # preview curated universe (offline)
python rates_pipeline.py backfill         # one-time full history
python rates_pipeline.py daily            # incremental lookback-upsert (cron / Task Scheduler)
python rates_pipeline.py duckdb           # (re)build DuckDB views
```

Storage: long-format Parquet partitioned `product/currency/year`, upsert on
`(tag, date)`. DuckDB exposes `rates` (long) + `swap_par` / `forward_swap` /
`vol_atm` views. For curve math, `rates_pipeline.load_wide(config, "swap_par",
"USD")` returns a date × tenor panel on demand.

### Open follow-ups
1. Bond/govvie entitlement (for swap spreads) — raise with Citi sales.
2. Re-run discovery's metadata in `--metadata sample` mode (quota permitting) to
   capture true per-tag start dates for backfill (otherwise we floor at 2016-01-01).
3. Phase 2 analytics: curves, flies (equal vs PCA weighting), forward grids — built
   in DuckDB on top of `swap_par` + `forward_swap`.
