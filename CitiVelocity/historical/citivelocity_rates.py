"""
citivelocity_rates.py
======================

A small, dependency-light client for pulling **Rates** (and any other) time-series
data from the Citi Velocity Charting *Historical Data* API.

Endpoint (POST):
    https://api.citivelocity.com/markets/analytics/chartingbe/rest/external/authed/data

Design notes
------------
* Auth is handled automatically: you supply a Client ID + Client Secret and the
  client fetches an OAuth2 ``client_credentials`` bearer token and refreshes it
  transparently before it expires (tokens last ~1 hour).
* All network calls have built-in retry logic (the service can be down for up to
  10 minutes for maintenance) and request gzip encoding.
* Responses are parsed into tidy ``pandas`` DataFrames, with the index parsed to
  real datetimes according to the *response* frequency (which can be coarser than
  what you requested if you over-ask for history -- see the guide).

This file is the work product of reading the two Citi Velocity API guides; it does
not contain or require any embedded credentials. Set credentials via environment
variables or pass them in explicitly.

    export CITI_CLIENT_ID=...
    export CITI_CLIENT_SECRET=...

Usage (quick):
    from citivelocity_rates import CitiVelocityClient
    c = CitiVelocityClient()                      # reads env vars
    df = c.get_series("RATES.SWAP_LIBOR.USD.PAR.2Y", "2024-01-01", "2024-06-30")
    panel = c.get_closes(["RATES.TSY.USD.2Y", "RATES.TSY.USD.10Y"],
                         "2024-01-01", "2024-06-30")   # wide DataFrame
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd
import requests

__all__ = [
    "CitiVelocityClient",
    "CitiVelocityError",
    "RATES_DATASETS",
]

log = logging.getLogger("citivelocity")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE = "https://api.citivelocity.com/markets"
DATA_URL = f"{BASE}/analytics/chartingbe/rest/external/authed/data"
METADATA_URL = f"{BASE}/analytics/chartingbe/rest/external/authed/metadata"
TAGLIST_URL = f"{BASE}/analytics/chartingbe/rest/external/authed/taglisting"
TAGBROWSE_URL = f"{BASE}/analytics/chartingbe/rest/external/authed/tagbrowsing"
# OAuth2 token endpoint (client-credentials / 2-legged flow).
TOKEN_URL = f"{BASE}/cv/api/oauth2/token"

# Valid frequency values accepted by the Data API.
FREQUENCIES = {"MONTHLY", "WEEKLY", "DAILY", "HOURLY", "MI10", "MI01"}

# Max history per single request for intraday frequencies (from the guide).
# Used only for friendly client-side warnings -- the server enforces the real cap
# and will silently down-sample if you over-ask.
INTRADAY_MAX_HISTORY = {
    "HOURLY": _dt.timedelta(days=365),
    "MI10": _dt.timedelta(days=60),
    "MI01": _dt.timedelta(days=31),
}

# Rates datasets documented in the Flat Files guide (dataset -> (description, start)).
# Handy as a discovery reference; not exhaustive of every tag.
RATES_DATASETS: Dict[str, Dict[str, str]] = {
    "RATES.VOL": {"desc": "Rates Volatility (swaption vol surfaces)", "start": "2016-06"},
    "RATES.TSY": {"desc": "Treasury on-the-run", "start": "2016-06"},
    "RATES.TIPS": {"desc": "TIPS", "start": "2020-11"},
    "RATES.SPREAD_OPTIONS": {"desc": "Single-look spread options", "start": "2022-06"},
    "RATES.SOV": {"desc": "Sovereign", "start": "2016-06"},
    "RATES.SOV_CMT": {"desc": "Sovereign CMT", "start": "2020-05"},
    "RATES.XCCY_OIS_SWAP": {"desc": "Cross-currency OIS swaps", "start": "2019-03"},
    "RATES.SWAP_LIBOR": {"desc": "Swap (Libor)", "start": "2016-06"},
    "RATES.OIS": {"desc": "OIS swaps (RFR)", "start": "2018-02"},
    "RATES.FRA_OIS": {"desc": "FRA/OIS", "start": "2020-11"},
    "RATES.FRA": {"desc": "FRA", "start": "2022-03"},
    "RATES.FORWARD": {"desc": "Forward", "start": "2021-06"},
    "RATES.MIDCURVES": {"desc": "MidCurves", "start": "2022-06"},
    "RATES.OIS_MEETING": {"desc": "OIS meeting (dated)", "start": "2020-11"},
    "RATES.SSA": {"desc": "SSA EUR", "start": "2022-04"},
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CitiVelocityError(RuntimeError):
    """Raised for API-level errors (status == 'ERROR') and auth failures."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _to_yyyymmdd(d: Union[str, int, _dt.date, _dt.datetime]) -> int:
    """Coerce a date-like value into the integer yyyyMMdd the API expects."""
    if isinstance(d, int):
        return d
    if isinstance(d, (_dt.date, _dt.datetime)):
        return int(d.strftime("%Y%m%d"))
    s = str(d).strip()
    if s.isdigit() and len(s) == 8:
        return int(s)
    # Fall back to pandas' flexible parser for things like '2024-01-31'.
    return int(pd.Timestamp(s).strftime("%Y%m%d"))


def _parse_x_index(x_values: Sequence, frequency: str) -> pd.DatetimeIndex:
    """Convert the API's frequency-specific 'x' integers into a DatetimeIndex.

    Formats (all GMT), per the guide:
        MONTHLY      yyyyMM
        WEEKLY       yyyyww     (ISO week-based year + ISO week)
        DAILY        yyyyMMdd
        HOURLY       yyyyMMddHH
        MI10         yyyyMMddHHm   (m = ten-minute value 0-5 -> minute = m*10)
        MI01         yyyyMMddHHmm
    """
    freq = (frequency or "DAILY").upper()
    s = [str(v) for v in x_values]

    if freq == "MONTHLY":
        return pd.to_datetime(s, format="%Y%m")
    if freq == "WEEKLY":
        # yyyyww ISO week -> Monday of that ISO week.
        out = []
        for v in s:
            v = v.zfill(6)
            year, week = int(v[:4]), int(v[4:6])
            out.append(_dt.date.fromisocalendar(year, week, 1))
        return pd.DatetimeIndex(pd.to_datetime(out))
    if freq == "DAILY":
        return pd.to_datetime(s, format="%Y%m%d")
    if freq == "HOURLY":
        return pd.to_datetime(s, format="%Y%m%d%H")
    if freq == "MI10":
        # 11-digit yyyyMMddHHm; expand the single ten-minute digit to mm.
        out = []
        for v in s:
            v = v.zfill(11)
            out.append(v[:10] + str(int(v[10]) * 10).zfill(2))
        return pd.to_datetime(out, format="%Y%m%d%H%M")
    if freq == "MI01":
        return pd.to_datetime([v.zfill(12) for v in s], format="%Y%m%d%H%M")

    # Unknown frequency: best-effort, leave as raw ints.
    return pd.Index(x_values)


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    value: Optional[str] = None
    expires_at: float = 0.0  # epoch seconds

    def valid(self, skew: int = 60) -> bool:
        return bool(self.value) and (time.time() + skew) < self.expires_at


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class CitiVelocityClient:
    """Client for the Citi Velocity Historical Data API.

    Parameters
    ----------
    client_id, client_secret:
        OAuth2 credentials. If omitted, read from ``CITI_CLIENT_ID`` /
        ``CITI_CLIENT_SECRET`` environment variables.
    access_token:
        Optionally supply a pre-fetched bearer token. If given (and no secret),
        the client uses it directly and cannot auto-refresh.
    proxies:
        Optional requests-style proxy dict, e.g. {"https": "http://host:8080"}.
    timeout:
        Per-request timeout in seconds.
    max_retries:
        Retries on transient failures (HTTP >=500, timeouts, connection errors).
        The service may be down up to 10 min, so retry with backoff.
    """

    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    access_token: Optional[str] = None
    proxies: Optional[dict] = None
    timeout: float = 60.0
    max_retries: int = 6
    backoff: float = 5.0  # base seconds for exponential backoff

    _token: _Token = field(default_factory=_Token, init=False, repr=False)
    _session: requests.Session = field(default_factory=requests.Session, init=False, repr=False)

    def __post_init__(self) -> None:
        self.client_id = self.client_id or os.environ.get("CITI_CLIENT_ID")
        self.client_secret = self.client_secret or os.environ.get("CITI_CLIENT_SECRET")
        if self.access_token is None:
            self.access_token = os.environ.get("CITI_ACCESS_TOKEN")
        if self.access_token:
            # Seed the token cache; expiry unknown so assume short-lived.
            self._token = _Token(self.access_token, time.time() + 1800)
        if not self.client_id:
            raise CitiVelocityError(
                "client_id is required (pass client_id= or set CITI_CLIENT_ID)."
            )
        if self.proxies:
            self._session.proxies.update(self.proxies)
        self._session.headers.update({"Accept-Encoding": "gzip"})

    # -- auth ---------------------------------------------------------------

    def _fetch_token(self) -> None:
        """Fetch a fresh OAuth2 client-credentials token."""
        if not self.client_secret:
            raise CitiVelocityError(
                "Cannot fetch a token without client_secret. "
                "Provide client_secret/CITI_CLIENT_SECRET, or pass a live access_token."
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "/api",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                r = self._session.post(
                    TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Accept": "application/json"},
                    timeout=self.timeout,
                )
                if r.status_code == 200:
                    payload = r.json()
                    tok = payload.get("access_token")
                    if not tok:
                        raise CitiVelocityError(f"No access_token in token response: {payload}")
                    expires_in = int(payload.get("expires_in", 3600))
                    self._token = _Token(tok, time.time() + expires_in)
                    log.info("Fetched new access token (expires_in=%ss).", expires_in)
                    return
                if r.status_code in (401, 403):
                    raise CitiVelocityError(
                        f"Auth failed ({r.status_code}). Check client_id/secret. Body: {r.text[:300]}"
                    )
                # 5xx / 429 -> retry
                last_exc = CitiVelocityError(f"Token endpoint HTTP {r.status_code}: {r.text[:200]}")
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
            self._sleep(attempt)
        raise CitiVelocityError(f"Failed to fetch token after retries: {last_exc}")

    def _auth_header(self) -> dict:
        if not self._token.valid():
            self._fetch_token()
        return {"authorization": f"Bearer {self._token.value}"}

    def _sleep(self, attempt: int) -> None:
        delay = min(self.backoff * (2 ** attempt), 120)
        log.warning("Retrying in %.0fs (attempt %d).", delay, attempt + 1)
        time.sleep(delay)

    # -- low-level POST -----------------------------------------------------

    def _post(self, url: str, payload: dict) -> dict:
        """POST JSON with auth, retry and error handling. Returns parsed JSON."""
        params = {"client_id": self.client_id}
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                headers = {"Content-Type": "application/json",
                           "Accept": "application/json"}
                headers.update(self._auth_header())
                r = self._session.post(
                    url, params=params, json=payload, headers=headers, timeout=self.timeout
                )
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 401:
                    # Token may have expired mid-flight; force refresh once.
                    log.info("401 received; refreshing token.")
                    self._token = _Token()
                    last_exc = CitiVelocityError("401 Unauthorized")
                elif r.status_code >= 400:
                    last_exc = CitiVelocityError(f"HTTP {r.status_code}: {r.text[:300]}")
                    if r.status_code < 500 and r.status_code != 429:
                        # Non-retryable client error.
                        raise last_exc
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
            self._sleep(attempt)
        raise CitiVelocityError(f"Request to {url} failed after retries: {last_exc}")

    # -- Data API -----------------------------------------------------------

    def get_data(
        self,
        tags: Union[str, Sequence[str]],
        start: Union[str, int, _dt.date],
        end: Union[str, int, _dt.date],
        frequency: Optional[str] = None,
        price_points: str = "C",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        latest_only: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Pull time series for one or many tags.

        Returns a dict ``{tag: DataFrame}``. Each DataFrame is indexed by a
        DatetimeIndex (GMT) and has a 'close' column (price_points='C') or
        'open'/'high'/'low'/'close' columns (price_points='OHLC').

        Tags that come back as per-tag ERRORs are logged and omitted from the
        result (their messages are available via the logger at WARNING level).

        Notes
        -----
        * Max 100 tags per call -- this method raises if you exceed that.
        * Intraday history is capped (HOURLY 1y / MI10 2mo / MI01 1mo). If you
          ask for more, the server returns a coarser frequency; the returned
          DataFrames are parsed using whatever frequency the server actually
          sent back, and a warning is logged if it differs from the request.
        """
        tag_list = [tags] if isinstance(tags, str) else list(tags)
        if not 1 <= len(tag_list) <= 100:
            raise CitiVelocityError(f"Data API allows 1-100 tags per call (got {len(tag_list)}).")

        pp = price_points.upper()
        if pp not in ("C", "OHLC"):
            raise CitiVelocityError("price_points must be 'C' or 'OHLC'.")

        payload: dict = {
            "startDate": _to_yyyymmdd(start),
            "endDate": _to_yyyymmdd(end),
            "tags": tag_list,
        }
        if frequency:
            freq = frequency.upper()
            if freq not in FREQUENCIES:
                raise CitiVelocityError(f"frequency must be one of {sorted(FREQUENCIES)}.")
            payload["frequency"] = freq
            self._warn_history(freq, payload["startDate"], payload["endDate"])
        if pp == "OHLC":
            payload["pricePoints"] = "OHLC"
        if start_time is not None:
            payload["startTime"] = start_time
        if end_time is not None:
            payload["endTime"] = end_time
        if latest_only:
            payload["latestOnly"] = True

        resp = self._post(DATA_URL, payload)
        if resp.get("status") == "ERROR":
            raise CitiVelocityError(f"Data API error: {resp.get('message')}")

        resp_freq = resp.get("frequency") or frequency or "DAILY"
        if frequency and resp_freq and resp_freq.upper() != frequency.upper():
            log.warning(
                "Server returned frequency %s instead of requested %s "
                "(history cap or EOD-only series).", resp_freq, frequency,
            )

        out: Dict[str, pd.DataFrame] = {}
        for tag, obj in (resp.get("body") or {}).items():
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "ERROR":
                log.warning("Tag %s -> ERROR: %s", tag, obj.get("message"))
                continue
            x = obj.get("x", []) or []
            idx = _parse_x_index(x, resp_freq)
            cols = {}
            if "o" in obj or "h" in obj or "l" in obj:
                cols = {
                    "open": obj.get("o"),
                    "high": obj.get("h"),
                    "low": obj.get("l"),
                    "close": obj.get("c"),
                }
                cols = {k: v for k, v in cols.items() if v is not None}
            else:
                cols = {"close": obj.get("c", [])}
            df = pd.DataFrame(cols, index=idx)
            df.index.name = "datetime"
            out[tag] = df
        return out

    def get_series(self, tag: str, start, end, **kwargs) -> pd.DataFrame:
        """Convenience: pull a single tag, return its DataFrame (empty if missing)."""
        res = self.get_data(tag, start, end, **kwargs)
        return res.get(tag, pd.DataFrame())

    def get_closes(
        self,
        tags: Sequence[str],
        start,
        end,
        frequency: Optional[str] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """Pull many tags and return a single *wide* DataFrame of closes.

        Columns are tags; rows are the (outer-joined) datetime index. This is the
        most convenient shape for cross-sectional / cross-asset rates work.
        """
        data = self.get_data(tags, start, end, frequency=frequency, price_points="C", **kwargs)
        if not data:
            return pd.DataFrame()
        series = {tag: df["close"] for tag, df in data.items() if "close" in df}
        wide = pd.DataFrame(series).sort_index()
        return wide

    def _warn_history(self, freq: str, start_yyyymmdd: int, end_yyyymmdd: int) -> None:
        cap = INTRADAY_MAX_HISTORY.get(freq)
        if not cap:
            return
        s = pd.to_datetime(str(start_yyyymmdd), format="%Y%m%d")
        e = pd.to_datetime(str(end_yyyymmdd), format="%Y%m%d")
        if (e - s) > cap:
            log.warning(
                "Requested %s of %s data exceeds the single-request cap (%s); "
                "the server will return a coarser frequency. Use the Bulk/Flat-Files "
                "API for large intraday pulls.",
                (e - s), freq, cap,
            )

    # -- Metadata API -------------------------------------------------------

    def get_metadata(
        self,
        tags: Union[str, Sequence[str]],
        frequency: str = "EOD",
    ) -> pd.DataFrame:
        """Return a DataFrame of per-tag metadata (description, start/end dates,
        last modification times). Up to 1000 tags per call. frequency in
        {'EOD','INTRADAY'} (EOD default; modifiedTimes/endDate are EOD-only)."""
        tag_list = [tags] if isinstance(tags, str) else list(tags)
        if not 1 <= len(tag_list) <= 1000:
            raise CitiVelocityError("Metadata API allows 1-1000 tags per call.")
        payload = {"tags": tag_list, "frequency": frequency.upper()}
        resp = self._post(METADATA_URL, payload)
        if resp.get("status") == "ERROR":
            raise CitiVelocityError(f"Metadata error: {resp.get('message')}")
        rows = []
        for tag, obj in (resp.get("body") or {}).items():
            mts = obj.get("modifiedTimes") or []
            rows.append({
                "tag": tag,
                "description": obj.get("description"),
                "startDate": obj.get("startDate"),
                "endDate": obj.get("endDate"),
                "lastModified": mts[0] if mts else None,
                "modifiedTimes": mts,
            })
        return pd.DataFrame(rows).set_index("tag") if rows else pd.DataFrame()

    # -- Tag discovery ------------------------------------------------------

    def list_tags(
        self,
        prefix: str,
        regex: Optional[str] = None,
        tag_type: Optional[str] = None,
    ) -> List[str]:
        """List tags under a prefix (must include Category + Sub-Category).

        Optional ``regex`` (Java syntax; begin with '.*') further filters the
        full tag. ``tag_type='BT'`` returns backtest-only tags.
        """
        payload: dict = {"prefix": prefix}
        if regex:
            payload["regex"] = regex
        if tag_type:
            payload["tagType"] = tag_type
        resp = self._post(TAGLIST_URL, payload)
        if resp.get("status") == "ERROR":
            raise CitiVelocityError(f"Tag listing error: {resp.get('message')}")
        return resp.get("tags", []) or []

    def browse(self, prefix: str = "") -> dict:
        """Explore the tag tree one level at a time. Pass '' for the root.
        Returns the raw node dict (header/fields/leaves/description)."""
        resp = self._post(TAGBROWSE_URL, {"prefix": prefix})
        if resp.get("status") == "ERROR":
            raise CitiVelocityError(f"Tag browsing error: {resp.get('message')}")
        return resp

    # -- Rates convenience wrappers ----------------------------------------
    # These are thin helpers that build common Rates prefixes/regexes for you.
    # They return tag lists you then pass to get_closes/get_data. Exact tag
    # structure varies by entitlement, so discovery (list_tags) is the source
    # of truth -- these just save typing the common cases.

    def find_swaption_vol(self, ccy: str, basis: str = "NORMAL", regex: Optional[str] = None) -> List[str]:
        """List swaption-vol tags, e.g. ccy='USD'. basis like NORMAL/BLACK/etc.
        Prefix used: RATES.VOL.<CCY>.ATM."""
        return self.list_tags(f"RATES.VOL.{ccy.upper()}.ATM.", regex=regex)

    def find_swap_tags(self, ccy: str, dataset: str = "RATES.SWAP_LIBOR", regex: Optional[str] = None) -> List[str]:
        """List swap/OIS tags for a currency under the given rates dataset."""
        return self.list_tags(f"{dataset}.{ccy.upper()}", regex=regex)

    def find_govvie_tags(self, ccy: str, dataset: str = "RATES.TSY", regex: Optional[str] = None) -> List[str]:
        """List government-bond/yield tags (TSY/SOV/TIPS) for a currency."""
        return self.list_tags(f"{dataset}.{ccy.upper()}", regex=regex)


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def save(data: Union[pd.DataFrame, Dict[str, pd.DataFrame]], path: str) -> None:
    """Save a wide DataFrame or a {tag: DataFrame} dict to .csv or .parquet.

    For a dict, frames are concatenated with a 'tag' column.
    """
    if isinstance(data, dict):
        frames = []
        for tag, df in data.items():
            d = df.copy()
            d.insert(0, "tag", tag)
            frames.append(d)
        out = pd.concat(frames) if frames else pd.DataFrame()
    else:
        out = data
    if path.lower().endswith(".parquet"):
        out.to_parquet(path)
    else:
        out.to_csv(path)
    log.info("Saved %d rows to %s", len(out), path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_cli():
    import argparse

    p = argparse.ArgumentParser(
        prog="citivelocity_rates",
        description="Pull Rates (and other) time series from the Citi Velocity Historical Data API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pd_ = sub.add_parser("data", help="Pull time series for one or more tags.")
    pd_.add_argument("tags", nargs="+", help="One or more tags.")
    pd_.add_argument("--start", required=True, help="Start date (YYYY-MM-DD or YYYYMMDD).")
    pd_.add_argument("--end", required=True, help="End date.")
    pd_.add_argument("--frequency", default=None, choices=sorted(FREQUENCIES))
    pd_.add_argument("--ohlc", action="store_true", help="Request OHLC instead of close-only.")
    pd_.add_argument("--latest-only", action="store_true")
    pd_.add_argument("--out", default=None, help="Save to .csv or .parquet.")

    pl = sub.add_parser("list", help="List tags under a prefix.")
    pl.add_argument("prefix")
    pl.add_argument("--regex", default=None)
    pl.add_argument("--tag-type", default=None)

    pm = sub.add_parser("meta", help="Show metadata for tags (description, date range).")
    pm.add_argument("tags", nargs="+")
    pm.add_argument("--frequency", default="EOD", choices=["EOD", "INTRADAY"])

    pb = sub.add_parser("browse", help="Browse the tag tree one level (empty prefix = root).")
    pb.add_argument("prefix", nargs="?", default="")

    sub.add_parser("datasets", help="Print the known Rates dataset reference table.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_cli().parse_args(argv)

    if args.cmd == "datasets":
        for name, info in RATES_DATASETS.items():
            print(f"{name:24s} {info['start']:>8s}  {info['desc']}")
        return 0

    client = CitiVelocityClient()

    if args.cmd == "data":
        res = client.get_data(
            args.tags, args.start, args.end,
            frequency=args.frequency,
            price_points="OHLC" if args.ohlc else "C",
            latest_only=args.latest_only,
        )
        for tag, df in res.items():
            print(f"\n=== {tag}  ({len(df)} rows) ===")
            print(df.head())
        if args.out:
            save(res, args.out)
    elif args.cmd == "list":
        for t in client.list_tags(args.prefix, regex=args.regex, tag_type=args.tag_type):
            print(t)
    elif args.cmd == "meta":
        print(client.get_metadata(args.tags, frequency=args.frequency).to_string())
    elif args.cmd == "browse":
        node = client.browse(args.prefix)
        print("header:", node.get("header"))
        for k, v in (node.get("fields") or {}).items():
            print(f"  {k}: {v}")
        if node.get("description"):
            print("description:", node["description"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
