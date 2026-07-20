# bbg_econ — Bloomberg economic-release actuals + consensus

`econ_surveys_pull.py` pulls, for each US macro release, the **actual** print and the
**Bloomberg survey (consensus)** — median / mean / std / high / low / #forecasts — as
history. It feeds the `econ_surprises` study in the Hobbes repo (surprise = actual −
consensus, and the seasonality of those surprises).

## Run (needs a logged-in Bloomberg Terminal)

```
python econ_surveys_pull.py                 # 2000-01-01 -> today  -> econ_surveys.csv
python econ_surveys_pull.py --start 2015-01-01
```

Then push so the desk can pull it — or just let `..\refresh_and_push.ps1` do the pull +
commit + push for the whole repo.

## First-run checklist (5 min on the terminal)

The tickers in `INDICATORS` are **best-effort**. Confirm each with `ECO <GO>` (open the
release → its ticker) or `<TICKER> DES <GO>`, and flip `status` to `"ok"`. Ones marked
`VERIFY` (PPI/PCE especially) are the least certain. A wrong ticker is skipped with a
warning — it won't break the run. Two field names are also worth a check:
`BN_SURVEY_STANDARD_DEVIATION` and `BN_SURVEY_NUMBER_OF_FORECASTS`.

Also verify, for one series (e.g. `NFP TCH Index` GP), whether the history is dated by
**release date** or **reference month** — the Hobbes consumer needs to know which to map
surprises to the right calendar month (it assumes release-dated, reference = prior month).

## "Whisper"

Bloomberg has consensus, not a distinct macro *whisper* number. The output has an empty
`whisper` column for a hand-entered / other-source value if you have one; otherwise the
study uses the consensus median as the expectation.

## Outputs (data contracts)

`econ_surveys.csv` — long, one row per (indicator, release date):

    indicator, ticker, date, actual, median, average, std, high, low, n_forecasts, whisper

`econ_surveys_daily.csv` — DAILY median history (trailing `--drift-years`, default 8), so
the consumer can see how consensus **drifted into** each release:

    indicator, ticker, date, actual, median

## Also here: `fedfunds_futures_pull.py`

Pulls the 30-day **Fed Funds futures strip** (generic `FF1..FFn Comdty`, `implied_rate =
100 − price`) → `fedfunds_futures.csv` — the market-implied policy path that the Hobbes
`rates_events` toolkit uses to back out priced-in hikes (the precise version of its
2Y−target proxy). Tickers are best-effort — verify `FF1 Comdty` on the terminal.
`refresh_and_push.ps1` runs it alongside the survey pull.
