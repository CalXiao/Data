# Citi Velocity Rates — Historical Data client

A small Python client for pulling **Rates** (and any other) time-series data from
the Citi Velocity Charting **Historical Data API**
(`.../chartingbe/rest/external/authed/data`). It handles OAuth2 token fetch +
refresh, retries, and parses responses into tidy `pandas` DataFrames.

## Files

- `citivelocity_rates.py` — the client (`CitiVelocityClient`) + a CLI.
- `examples.py` — worked examples (smoke test, swaption vol, swap curve, treasuries, discovery).
- `requirements.txt` — `pandas`, `requests` (+ optional `pyarrow`).

## Setup

```bash
pip install -r requirements.txt
export CITI_CLIENT_ID=your_client_id
export CITI_CLIENT_SECRET=your_client_secret
```

The client fetches a bearer token from the OAuth2 endpoint
(`client_credentials` grant) and refreshes it automatically before it expires
(~1 hour). You never paste tokens by hand. If you only have a short-lived token,
you can also pass `access_token=...` (no auto-refresh in that mode), or set
`CITI_ACCESS_TOKEN`.

> Note on auth: the marketplace documents a 2-legged `client_credentials` flow at
> `markets/cv/api/oauth2/token`. The client posts `client_id`, `client_secret`,
> `grant_type=client_credentials`, `scope=/api` as form data. If your tenant
> expects HTTP Basic auth instead, that is a one-line change in `_fetch_token`.

## Quick start (Python)

```python
from citivelocity_rates import CitiVelocityClient

c = CitiVelocityClient()  # reads env vars

# Single series -> DataFrame (datetime index, 'close' column)
df = c.get_series("RATES.TSY.USD.10Y", "2024-01-01", "2024-06-30")

# Many tags -> one wide DataFrame of closes (columns = tags)
panel = c.get_closes(
    ["RATES.TSY.USD.2Y", "RATES.TSY.USD.10Y"],
    "2024-01-01", "2024-06-30",
)

# OHLC
ohlc = c.get_series("FX.SPOT.EUR.USD.CITI", "2024-03-20", "2024-03-24",
                    price_points="OHLC")

# Intraday (history-capped: HOURLY 1y / MI10 2mo / MI01 1mo)
hr = c.get_series("RATES.TSY.USD.10Y", "2024-06-01", "2024-06-07",
                 frequency="HOURLY")
```

### Discovery

You can't list *all* tags, but you can list under a prefix and inspect metadata:

```python
c.list_tags("RATES.VOL.USD.ATM.", regex=".*NORMAL.ANNUAL.1M.*")  # -> [tags]
c.browse("RATES")                 # one level of the tag tree
c.get_metadata(["RATES.TSY.USD.10Y"])  # description + available date range
```

Rates convenience helpers (just build common prefixes — discovery is the source
of truth for exact tags): `find_swaption_vol(ccy)`, `find_swap_tags(ccy, dataset)`,
`find_govvie_tags(ccy, dataset)`.

## Quick start (CLI)

```bash
python citivelocity_rates.py datasets                       # rates dataset reference
python citivelocity_rates.py list RATES.VOL.USD.ATM. --regex ".*NORMAL.ANNUAL.1M.*"
python citivelocity_rates.py meta RATES.TSY.USD.10Y
python citivelocity_rates.py browse RATES
python citivelocity_rates.py data RATES.TSY.USD.2Y RATES.TSY.USD.10Y \
    --start 2024-01-01 --end 2024-06-30 --out tsy.csv
```

## Rates datasets (reference)

From the Flat-Files guide; OHLC support is mostly EOD-only for rates. Exact tag
structure depends on your entitlement — use `list_tags` / `browse` to confirm.

| Dataset | Since | Description |
|---|---|---|
| RATES.VOL | 2016-06 | Rates volatility (swaption vol surfaces) |
| RATES.TSY | 2016-06 | Treasury on-the-run |
| RATES.TIPS | 2020-11 | TIPS |
| RATES.SPREAD_OPTIONS | 2022-06 | Single-look spread options |
| RATES.SOV | 2016-06 | Sovereign |
| RATES.SOV_CMT | 2020-05 | Sovereign CMT |
| RATES.XCCY_OIS_SWAP | 2019-03 | Cross-currency OIS swaps |
| RATES.SWAP_LIBOR | 2016-06 | Swap (Libor) |
| RATES.OIS | 2018-02 | OIS swaps (RFR) |
| RATES.FRA_OIS | 2020-11 | FRA/OIS |
| RATES.FRA | 2022-03 | FRA |
| RATES.FORWARD | 2021-06 | Forward |
| RATES.MIDCURVES | 2022-06 | MidCurves |
| RATES.OIS_MEETING | 2020-11 | OIS meeting (dated) |
| RATES.SSA | 2022-04 | SSA EUR |

A worked example tag from the guide: `RATES.VOL.USD.ATM_RFR.NORMAL.ANNUAL.1M.3M`
("1M x 3M USD Normal Annual RFR Vol, BPS/ANNUM").

## Behavior & limits worth knowing

- **Frequencies**: `MONTHLY`, `WEEKLY`, `DAILY` (default), `HOURLY`, `MI10`
  (ten-minutely), `MI01` (minutely). HOURLY and finer are intraday.
- **Intraday history is capped per request** (HOURLY 1y / MI10 2mo / MI01 1mo).
  Over-asking does **not** error — the server silently returns a coarser
  frequency. The client parses whatever frequency comes back and logs a warning
  if it differs from your request. For large intraday pulls use the **Bulk Data**
  or **Flat Files** services (separate endpoints; can be added on request).
- **Up to 100 tags per data call** (1000 for metadata). The client enforces this.
- **Rate limit** is 1 req/sec with server-side queuing — you do **not** need to
  sleep; the client just issues requests.
- **Times are GMT.** The DataFrame index is tz-naive GMT.
- **Empty `x`/`c`** for a valid tag is normal (no data in window) — returns an
  empty frame, not an error. Per-tag bad tags come back as ERROR and are skipped
  (logged at WARNING).
- **Retries**: transient 5xx/timeouts retry with exponential backoff (the service
  can be down up to 10 min for maintenance); avoid heavy weekend jobs.
- **Redistribution**: data pulled from this service **cannot be redistributed**
  (per Citi's terms).

## Not yet wired (say the word)

- Historical **Bulk** export (submit job → poll status → download zip) for large
  intraday histories of a selected tag set.
- **Flat Files** export (download links for *all* tags in a dataset).

Both are documented in the guides and straightforward to add to this client.

---

## Running the backfill (live)

The pull list is the curated selection (`discovery/rates_selection_inventory.parquet`,
6,119 tags) wired in via `config.yaml` -> `storage.selection`. Edit
`curate_selection.py` + re-run it to change the universe.

```bash
pip install -r requirements.txt
export CITI_CLIENT_ID=...    ;  export CITI_CLIENT_SECRET=...   # (set / setx on Windows)

python rates_pipeline.py select      # preview the 6,119-tag pull list (offline, no API)
python rates_pipeline.py backfill     # one-time full history -> ./data + DuckDB views + coverage
python rates_pipeline.py daily        # incremental lookback-upsert (schedule this)
```

Backfill is ~62 calls / ~6,119 items (well inside the 10k-call / 100k-item daily caps),
takes ~1-2 min of API time, and lands ~15-17M rows (~150-300 MB Parquet). It's
idempotent: safe to re-run (upsert on tag+date). After it finishes you'll get a
per-product coverage table and a count of any tags that returned no data (expected
for some long JPY/AUD tails).

---

## Analytics layer (`analytics.py`)

Derivations over the raw store — recomputed automatically whenever the pipeline
rebuilds DuckDB (so `daily` keeps them current; nothing extra to run).

Engine (returns tidy DataFrames, levels in %, slopes/flies in bp):
```python
import analytics as A
con = A.connect(A.load_config())
A.swap_curve(con, "USD")                       # date x tenor PAR levels (%)
A.slope(con, "USD", "2Y", "10Y")               # 2s10s in bp
A.butterfly(con, "USD", "5Y","10Y","30Y", weighting="equal")   # equal|pca|regression
A.forward_curve(con, "USD", "5Y")              # 5y-fwd across tails
A.forward_ladder(con, "USD", "1Y")             # n-fwd-1y ladder
A.fwd_point(con, "USD", "5Y", "5Y")            # 5y5y outright (%)
A.swap_spread(con, "USD")                       # swap spreads (bp)
```
Conventions: basis auto-resolves (RFR where it exists; IBOR for EUR/AUD); JPY uses
primary TONAR (clearing variants excluded from curve math). Butterfly default is
equal-weight (-1/+2/-1); `pca` = level- & slope-neutral (sum-zero) curvature
weights; `regression` = belly residual vs wings. PCA/regression are full-sample by
default (pass `window=N` for trailing, no lookahead).

Materialized presets live in the DuckDB table **`rv_presets`** (tidy: date,
currency, signal, kind, unit, value) — standard slopes, equal flies, forward
outrights, and swap spreads across all six currencies. Query it directly, e.g.:
```sql
SELECT * FROM rv_presets WHERE currency='USD' AND kind='slope';
```

---

## Daily scheduled refresh

`rates_pipeline.py daily` pulls the last `lookback_business_days` (config, default 7),
upserts on (tag, date) — self-healing restatements — and rebuilds DuckDB views +
`rv_presets`. Three helpers:

- `run_daily.bat` — runs the refresh, logging to `logs\daily_YYYYMMDD.log`.
- `register_task.bat` — **run once** to schedule it (Windows Task Scheduler,
  weekdays 07:00 local; weekends skipped to avoid Citi's Fri-Sun maintenance window).
- `unregister_task.bat` — removes the scheduled task.

Setup: double-click `register_task.bat` once (no admin needed for a per-user task).
Adjust time/days via `/ST`/`/D` in the script or the Task Scheduler GUI. Each run is
~1-2 min; credentials load from `secrets.env` automatically. Check `logs\` for the
coverage table and any errors. Legacy (pre-transition) tags returning no new data is
expected, not an error.
