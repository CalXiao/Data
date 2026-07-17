#!/usr/bin/env python3
"""
citivelocity_feed.py
====================
Live USD SOFR OIS par-curve feed for the Swap Pricer, sourced from the
**CitiVelocity Live Time Series WebSocket** (streamapi.citivelocity.com).

It mirrors the role of sofr_curve_feed.py (the Bloomberg bridge): it maintains
the latest par rates in memory and serves them as JSON at /curve on localhost,
so the pricer's LOAD button can pull them. The difference is the source - here a
streaming WebSocket rather than a Bloomberg ReferenceDataRequest.

Auth is the same OAuth2 client-credentials flow used by the historical pull
(Velocity Pull/citivelocity_rates.py): mint a bearer token from CITI_CLIENT_ID /
CITI_CLIENT_SECRET, then open the wss URL with client_id + access_token.

Tags (matching your CVSTREAM formula, CLOSE price point) — per ccy USD + JPY:
    RATES.OIS.{USD_SOFR,JPY_TONAR}.PAR.{6M,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,12Y,15Y,20Y,25Y,30Y,40Y}
    RATES.OIS.{USD_SOFR,JPY_TONAR}.FWD.{1..20}Y.1Y
/curve payload: legacy top-level quotes/forwards = USD; per-ccy blocks under "ccys".
Also serves /tona_fixings (BOJ TONA fixings + synthetic compounded index).

Usage
-----
    # live (needs creds + Citi entitlement + network/proxy):
    python citivelocity_feed.py --serve --http-port 8198

    # off-hours smoke test using Citi's MOCK.n tags (ticks every minute):
    python citivelocity_feed.py --serve --http-port 8198 --mock

    # one-shot snapshot to stdout/JSON after a short listen window:
    python citivelocity_feed.py --listen-seconds 90 --out citi_curve.json

Then open http://localhost:8198/ (serves SwapPricer.html) and click LOAD.
The raw feed is at http://localhost:8198/curve.

Requirements
------------
    pip install websocket-client requests
A logged-in entitlement to the CitiVelocity Live Time Series Web Service, and
(if behind a corporate proxy) HTTPS_PROXY set or --proxy host:port.

COMPLIANCE: data pulled from this service cannot be redistributed. This bridge
binds to loopback (127.0.0.1) only and is for your own local pricer.
"""

import argparse, json, os, struct, sys, threading, time, urllib.parse
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_URL = "https://api.citivelocity.com/markets/cv/api/oauth2/token"
WS_URL = "wss://www.streamapi.citivelocity.com/markets/analytics/ws/chartingbe/ws/authed/v1"

# multi-ccy (2026-07-06): USD + JPY stream side by side. Same 17-point par grid
# and 1y-fwd suite per ccy (verified present in ticker_inventory.csv for both).
# dual-curve (2026-07-07): EUR/AUD stream BOTH the OIS family (discounting) and
# the SWAP_LIBOR family (EURIBOR/BBSW projection). Family value = full tag prefix.
CCYS = {"USD": {"ois": "OIS.USD_SOFR"},   "JPY": {"ois": "OIS.JPY_TONAR"},
        "GBP": {"ois": "OIS.GBP_SONIA"},  "CAD": {"ois": "OIS.CAD_CORRA"},
        "EUR": {"ois": "OIS.EUR_EUROSTR", "irs": "SWAP_LIBOR.EUR"},
        "AUD": {"ois": "OIS.AUD_AONIA",   "irs": "SWAP_LIBOR.AUD"}}
# tenor -> tag  (the exact 17 par points from your CVSTREAM formula, per family)
TENORS = ["6M","1Y","2Y","3Y","4Y","5Y","6Y","7Y","8Y","9Y","10Y","12Y","15Y","20Y","25Y","30Y","40Y"]
def tag_for(tenor, ccy="USD", leg="ois"): return f"RATES.{CCYS[ccy][leg]}.PAR.{tenor}"
# forward suite -> RATES.<fam>.FWD.<E>Y.1Y. Expiries are read from the HISTORICAL
# pull list per family (SWAP_LIBOR chains have catalog holes at 16-19/35/45Y —
# streamed as-is; the engine's sparse-fwd path tolerates the gaps). Falls back to
# the baked set. Restart the feed after editing the csv (resolved once at import).
FWD_EXP_DEFAULT = list(range(1, 21)) + [25, 30, 35, 40, 45, 50]
PULL_LIST = os.path.join(SCRIPT_DIR, "..", "historical", "ticker_pull_list.csv")
def _fwd_expiries():
    out = {}
    try:
        import re
        txt = open(PULL_LIST, encoding="utf-8", errors="ignore").read()
        for c, legs in CCYS.items():
            for leg, fam in legs.items():
                es = sorted({int(m) for m in re.findall(r"RATES\." + fam.replace(".", r"\.") + r"\.FWD\.(\d+)Y\.1Y", txt)})
                if es:
                    out[(c, leg)] = es
    except Exception as e:
        print(f"[citi] WARN: could not parse {PULL_LIST} ({e}); using baked fwd expiries")
    return {(c, leg): out.get((c, leg), list(FWD_EXP_DEFAULT)) for c, legs in CCYS.items() for leg in legs}
FWD_EXP = _fwd_expiries()     # (ccy, leg) -> [int expiries]
def fwd_tag(e, ccy="USD", leg="ois"): return f"RATES.{CCYS[ccy][leg]}.FWD.{e}Y.1Y"
def build_subs():
    """Unified subscription list: par + 1y-fwd suite per (ccy, leg). Each {kind,ccy,leg,key,tag}."""
    subs = []
    for ccy, legs in CCYS.items():
        for leg in legs:
            subs += [{"kind": "par", "ccy": ccy, "leg": leg, "key": t, "tag": tag_for(t, ccy, leg)} for t in TENORS]
            subs += [{"kind": "fwd", "ccy": ccy, "leg": leg, "key": f"{e}Y", "tag": fwd_tag(e, ccy, leg)} for e in FWD_EXP[(ccy, leg)]]
    return subs


# ----------------------------------------------------------------------------
# credentials
# ----------------------------------------------------------------------------
def load_secrets(path):
    """Populate os.environ from a simple KEY=VALUE .env file. The file is AUTHORITATIVE:
    it overrides any pre-existing/placeholder env vars (e.g. a stale CITI_CLIENT_ID=your_id)."""
    if path and os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ[k.strip()] = v   # override, do not setdefault

def fetch_token(client_id, client_secret, timeout=30):
    """OAuth2 client-credentials bearer token. Tries HTTP Basic auth (the common
    CitiVelocity form) first, then creds-in-body (the historical client's form)."""
    import requests, base64
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    attempts = [
        ("basic", {"grant_type": "client_credentials", "scope": "/api"},
         {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}),
        ("body", {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret, "scope": "/api"},
         {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}),
    ]
    codes = []
    for name, data, headers in attempts:
        try:
            r = requests.post(TOKEN_URL, data=data, headers=headers, timeout=timeout)
        except Exception as e:
            codes.append(f"{name}:{type(e).__name__}"); continue
        if r.status_code == 200 and r.json().get("access_token"):
            return r.json()["access_token"]
        codes.append(f"{name}:{r.status_code}")
        last_body = r.text[:160]
    raise RuntimeError(f"token rejected [{', '.join(codes)}] {last_body}")


def test_token_variants(client_id, client_secret, timeout=30):
    """Diagnostic: try several documented OAuth request forms and report which the
    server accepts. Creds are masked in output. Run with --test-token."""
    import requests, base64
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    mid = f"{client_id[:4]}…{client_id[-2:]} (len {len(client_id)})"
    print(f"[citi] token endpoint: {TOKEN_URL}")
    print(f"[citi] client_id: {mid} | secret len {len(client_secret)}")
    variants = [
        ("Basic header + scope=/api",  {"grant_type": "client_credentials", "scope": "/api"},  {"Authorization": f"Basic {basic}"}),
        ("Basic header, no scope",      {"grant_type": "client_credentials"},                   {"Authorization": f"Basic {basic}"}),
        ("Body creds + scope=/api",     {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret, "scope": "/api"}, {}),
        ("Body creds, no scope",        {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}, {}),
    ]
    for name, data, extra in variants:
        h = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}; h.update(extra)
        try:
            r = requests.post(TOKEN_URL, data=data, headers=h, timeout=timeout)
            ok = r.status_code == 200 and bool(r.json().get("access_token")) if r.headers.get("content-type","").startswith("application/json") else False
            print(f"  [{'OK ' if ok else 'xx'}] {name:28s} -> HTTP {r.status_code}  {('' if ok else r.text[:120])}")
            if ok:
                print(f"        ^ THIS WORKS. expires_in={r.json().get('expires_in')}")
        except Exception as e:
            print(f"  [er] {name:28s} -> {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------
# live curve state (thread-safe latest snapshot)
# ----------------------------------------------------------------------------
class CurveState:
    def __init__(self):
        self.lock = threading.Lock()
        # per (ccy, leg): par tenor -> latest, fwd expiry (1y tenor) -> latest
        self.par = {(c, l): {t: {"rate": None, "ts": None} for t in TENORS} for c, legs in CCYS.items() for l in legs}
        self.fwd = {(c, l): {f"{e}Y": {"rate": None, "ts": None} for e in FWD_EXP[(c, l)]} for c, legs in CCYS.items() for l in legs}
        self.connected = False
        self.last_msg = 0.0
        self.diag = {"connected": False, "conn_ready": False, "subscribed": 0,
                     "suback_err": [], "conn_error": None, "binary_msgs": 0, "first_data": None}

    def update(self, kind, ccy, leg, key, value, ts_str):
        with self.lock:
            d = (self.par if kind == "par" else self.fwd).get((ccy, leg), {})
            if key in d:
                d[key] = {"rate": value, "ts": ts_str}
                self.last_msg = time.time()

    def _leg_block(self, c, l):
        P, F = self.par[(c, l)], self.fwd[(c, l)]
        quotes = [{"tenor": t, "kind": "swap", "rate": P[t]["rate"], "ts": P[t]["ts"]} for t in TENORS]
        forwards = [{"expiry": f"{e}Y", "tenor": "1Y", "rate": F[f"{e}Y"]["rate"], "ts": F[f"{e}Y"]["ts"]} for e in FWD_EXP[(c, l)]]
        missing = [t for t in TENORS if P[t]["rate"] is None]
        allts = [v["ts"] for v in list(P.values()) + list(F.values()) if v["ts"]]
        return {"quotes": quotes, "forwards": forwards, "missing": missing, "asof": max(allts, default=None)}

    def payload(self):
        with self.lock:
            # per-ccy block = the OIS leg (back-compat shape); dual ccys add an "irs" sub-block
            ccys = {}
            for c, legs in CCYS.items():
                blk = self._leg_block(c, "ois")
                if "irs" in legs:
                    blk["irs"] = self._leg_block(c, "irs")
                ccys[c] = blk
            usd = ccys["USD"]
            asof = max([b["asof"] for b in ccys.values() if b["asof"]], default=None)
            return {
                "asof": asof,
                "asof_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "CitiVelocity live stream (RATES.OIS.{USD_SOFR,JPY_TONAR} PAR + 1y-fwd suite, MI01)",
                "connected": self.connected,
                "stale_seconds": round(time.time() - self.last_msg, 1) if self.last_msg else None,
                "diag": dict(self.diag),
                # legacy top-level fields = USD (pre-multi-ccy consumers keep working)
                "quotes": usd["quotes"],
                "forwards": usd["forwards"],
                "missing": usd["missing"],
                "ccys": ccys,
            }


def ts_from_long(v):
    """Binary timestamp is an int64 of decimal yyyyMMddHHmm -> ISO 'yyyy-mm-ddThh:mm'."""
    try:
        s = str(int(v))
        if len(s) == 12:
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}"
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# WebSocket streaming client (runs in a background thread, auto-reconnects)
# ----------------------------------------------------------------------------
def run_stream(state, client_id, client_secret, mock=False, proxy=None, verbose=True):
    try:
        import websocket  # from the 'websocket-client' package
    except ImportError:
        state.diag["conn_error"] = "websocket-client not installed (run: pip install websocket-client)"
        print("[citi] " + state.diag["conn_error"]); return

    # unified subscription list (par + 1y-fwd suite). mock mode swaps real tags for
    # MOCK.n so the whole pipeline (sub -> decode -> serve -> UI) works off-hours.
    SUBS = build_subs()
    if mock:
        SUBS = [{**s, "tag": f"MOCK.{i+1}"} for i, s in enumerate(SUBS)]

    def on_open(ws):
        state.connected = True; state.diag["connected"] = True
        print("[citi] socket OPEN; waiting for CONN_READY...")

    def send_subs(ws):
        for i, s in enumerate(SUBS):
            ws.send(json.dumps({"type": "SUB", "id": i + 1, "tag": s["tag"], "pricePoint": "CLOSE"}))
        print(f"[citi] CONN_READY -> sent {len(SUBS)} SUB requests ({'MOCK' if mock else 'USD+JPY par + 1y-fwd'}).")

    def on_message(ws, message):
        if isinstance(message, (bytes, bytearray)):
            if len(message) < 8:
                return
            ts_str = ts_from_long(struct.unpack_from(">q", message, 0)[0])
            n = (len(message) - 8) // 12
            hit = 0
            for k in range(n):
                off = 8 + 12 * k
                subid = struct.unpack_from(">i", message, off)[0]
                val = struct.unpack_from(">d", message, off + 4)[0]
                meta = ws._subid_meta.get(subid)
                if meta is not None:
                    state.update(meta["kind"], meta.get("ccy", "USD"), meta.get("leg", "ois"), meta["key"], val, ts_str); hit += 1
            state.diag["binary_msgs"] += 1
            if state.diag["first_data"] is None:
                state.diag["first_data"] = ts_str or "yes"
                print(f"[citi] FIRST DATA received ({n} pairs, {hit} matched, ts {ts_str}).")
            return
        # text control message (JSON)
        try:
            msg = json.loads(message)
        except Exception:
            return
        typ = msg.get("type")
        if typ == "CONN_READY":
            ws._subid_meta = {}
            state.diag.update(conn_ready=True, subscribed=0, suback_err=[], conn_error=None)
            send_subs(ws)
        elif typ == "SUBACK":
            idx = msg.get("id")
            sub = SUBS[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(SUBS) else None
            if msg.get("status") == "OK":
                if sub is not None:
                    ws._subid_meta[msg.get("subid")] = sub   # subid -> {kind,key,tag}
                    state.diag["subscribed"] += 1
            else:
                err = f"{(sub or {}).get('tag','?')}: {msg.get('message')}"
                state.diag["suback_err"].append(err)
                print(f"[citi] SUBACK ERROR {err}")
        elif typ == "CONN_ERROR":
            state.diag["conn_error"] = msg.get("message")
            print(f"[citi] CONN_ERROR: {msg.get('message')}  (server will close the connection)")
        elif typ == "KEEP_ALIVE":
            pass

    def on_error(ws, err):
        state.diag["conn_error"] = f"{type(err).__name__}: {err}"   # surface wss connect/handshake errors
        print(f"[citi] ws error: {err}")

    def on_close(ws, code, reason):
        state.connected = False
        state.diag.update(connected=False, conn_ready=False)
        print(f"[citi] socket CLOSED ({code} {reason}); reconnecting...")

    # proxy resolution: --proxy, else HTTPS_PROXY, else the OS/WinINET system proxy.
    # (Excel's CVSTREAM add-in uses the Windows system proxy transparently; websocket-client does not,
    #  which is the usual reason a direct connect "never connects" on a machine where Excel ticks fine.)
    import urllib.request
    px_host = px_port = None
    if proxy:
        parts = proxy.replace("http://", "").split(":"); px_host = parts[0]; px_port = parts[1] if len(parts) > 1 else "8080"
    if not px_host:
        sysp = urllib.request.getproxies()  # reads Windows WinINET registry / macOS system proxy
        purl = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or sysp.get("https") or sysp.get("http")
        if purl:
            u = urllib.parse.urlparse(purl if "://" in purl else "http://" + purl)
            px_host, px_port = u.hostname, u.port
    print(f"[citi] proxy: {px_host}:{px_port}" if px_host else
          "[citi] proxy: none (direct). If Excel needs a proxy, set HTTPS_PROXY or pass --proxy host:port.")

    backoff = 3
    while True:
        try:
            token = fetch_token(client_id, client_secret)
            url = f"{WS_URL}?client_id={urllib.parse.quote(client_id)}&access_token=" \
                  + urllib.parse.quote(f"Bearer {token}")
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                        on_error=on_error, on_close=on_close)
            ws._subid_meta = {}
            ws.run_forever(ping_interval=30, ping_timeout=10,
                           http_proxy_host=px_host,
                           http_proxy_port=int(px_port) if px_port else None,
                           proxy_type="http" if px_host else None)
        except Exception as e:
            state.diag["conn_error"] = f"{type(e).__name__}: {e}"   # surface to /curve + pricer
            print(f"[citi] connect error ({type(e).__name__}): {e}")
        state.connected = False; state.diag["connected"] = False
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)  # token refetched on each reconnect (handles ~1h expiry)


# ----------------------------------------------------------------------------
# SUBACK probe: connect once, subscribe every tag, collect SUBACKs (and which
# tick) for a few seconds, print a pass/fail report, exit. Run with --probe.
# ----------------------------------------------------------------------------
def run_probe(client_id, client_secret, mock=False, proxy=None, seconds=12):
    import websocket, urllib.request
    SUBS = build_subs()
    if mock:
        SUBS = [{**s, "tag": f"MOCK.{i+1}"} for i, s in enumerate(SUBS)]
    st = [{"sub": s, "ack": None, "err": None, "ticked": False} for s in SUBS]
    subid_idx = {}; box = {"conn_error": None, "conn_ready": False}

    def on_open(ws): print("[probe] socket OPEN; waiting for CONN_READY...")
    def on_message(ws, message):
        if isinstance(message, (bytes, bytearray)):
            n = (len(message) - 8) // 12
            for k in range(n):
                sid = struct.unpack_from(">i", message, 8 + 12 * k)[0]
                if sid in subid_idx: st[subid_idx[sid]]["ticked"] = True
            return
        try: msg = json.loads(message)
        except Exception: return
        t = msg.get("type")
        if t == "CONN_READY":
            box["conn_ready"] = True
            for i, s in enumerate(SUBS):
                ws.send(json.dumps({"type": "SUB", "id": i + 1, "tag": s["tag"], "pricePoint": "CLOSE"}))
            print(f"[probe] sent {len(SUBS)} SUBs; collecting SUBACKs + ticks for {seconds}s...")
        elif t == "SUBACK":
            i = (msg.get("id") or 0) - 1
            if 0 <= i < len(SUBS):
                if msg.get("status") == "OK":
                    st[i]["ack"] = "OK"; sid = msg.get("subid"); subid_idx[sid] = i
                else:
                    st[i]["ack"] = "ERROR"; st[i]["err"] = msg.get("message")
        elif t == "CONN_ERROR":
            box["conn_error"] = msg.get("message")
    def on_error(ws, e): box["conn_error"] = f"{type(e).__name__}: {e}"

    px_host = px_port = None
    if proxy:
        parts = proxy.replace("http://", "").split(":"); px_host = parts[0]; px_port = parts[1] if len(parts) > 1 else "8080"
    if not px_host:
        sysp = urllib.request.getproxies()
        purl = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or sysp.get("https") or sysp.get("http")
        if purl:
            u = urllib.parse.urlparse(purl if "://" in purl else "http://" + purl); px_host, px_port = u.hostname, u.port

    try:
        token = fetch_token(client_id, client_secret)
    except Exception as e:
        print(f"[probe] token fetch failed: {e}"); return
    url = f"{WS_URL}?client_id={urllib.parse.quote(client_id)}&access_token=" + urllib.parse.quote(f"Bearer {token}")
    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error)
    threading.Timer(seconds, ws.close).start()
    ws.run_forever(ping_interval=30, ping_timeout=10, http_proxy_host=px_host,
                   http_proxy_port=int(px_port) if px_port else None, proxy_type="http" if px_host else None)

    # ---- report ----
    def summ(kind):
        rows = [r for r in st if r["sub"]["kind"] == kind]
        ok = [r for r in rows if r["ack"] == "OK"]; er = [r for r in rows if r["ack"] == "ERROR"]
        no = [r for r in rows if r["ack"] is None]; tk = [r for r in ok if r["ticked"]]
        return rows, ok, er, no, tk
    print("\n" + "=" * 52 + "\n SUBACK PROBE REPORT\n" + "=" * 52)
    if box["conn_error"]: print(f" CONN_ERROR: {box['conn_error']}")
    if not box["conn_ready"]: print(" (never reached CONN_READY — check token / proxy / network)")
    for kind, label in (("par", "PAR  "), ("fwd", "FWD  ")):
        rows, ok, er, no, tk = summ(kind)
        print(f" {label}: {len(ok)}/{len(rows)} SUBACK OK   ({len(tk)} ticked, {len(er)} ERROR, {len(no)} no-reply)")
    errs = [r for r in st if r["ack"] == "ERROR"]
    if errs:
        print(" SUBACK errors (not streamable):")
        for r in errs[:40]: print(f"   {r['sub']['tag']:36s} {r['err']}")
    noreply = [r for r in st if r["ack"] is None]
    if noreply:
        print(f" no SUBACK reply for {len(noreply)} tags (e.g. {', '.join(r['sub']['tag'] for r in noreply[:5])})")
    print("=" * 52)


# ----------------------------------------------------------------------------
# HTTP server (serves /curve JSON + the pricer UI), loopback + CORS, mirrors the
# other bridges. Falls through Windows excluded ports.
# ----------------------------------------------------------------------------
def serve(state, http_port, ui_path, open_browser, fixings_path=None, tona_fixings_path=None,
          sonia_fixings_path=None, corra_fixings_path=None, estr_fixings_path=None, aonia_fixings_path=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import webbrowser

    class H(BaseHTTPRequestHandler):
        def _hdr(self, code, ctype):
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store"); self.end_headers()
        def _json(self, code, obj): self._hdr(code, "application/json"); self.wfile.write(json.dumps(obj).encode())
        def do_OPTIONS(self): self._hdr(204, "application/json")
        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path in ("/curve", "/health"):
                self._json(200, state.payload())
            elif path in ("/sofr_fixings", "/fixings"):
                # realized SOFR fixings + SOFR Index backfill (written by sofr_fixings_pull.py,
                # which start_pricer.bat runs on launch). Used to value seasoned/backdated swaps.
                fp = fixings_path or os.path.join(SCRIPT_DIR, "sofr_fixings.json")
                if os.path.isfile(fp):
                    with open(fp, "rb") as f: data = f.read()
                    self._hdr(200, "application/json"); self.wfile.write(data)
                else:
                    self._json(404, {"error": "sofr_fixings.json not present yet — run sofr_fixings_pull.py "
                                              "(start_pricer.bat does this automatically on launch)"})
            elif path in ("/tona_fixings", "/sonia_fixings", "/corra_fixings", "/estr_fixings", "/aonia_fixings"):
                # realized fixings + compounded index per ccy (written by the
                # boj/boe/boc/ecb/rba pullers; start_pricer.bat runs them on launch).
                # Used to value seasoned/backdated swaps in each ccy.
                fmap = {"/tona_fixings":  (tona_fixings_path,  "boj\\tona_fixings_pull.py"),
                        "/sonia_fixings": (sonia_fixings_path, "boe\\sonia_fixings_pull.py"),
                        "/corra_fixings": (corra_fixings_path, "boc\\corra_fixings_pull.py"),
                        "/estr_fixings":  (estr_fixings_path,  "ecb\\estr_fixings_pull.py"),
                        "/aonia_fixings": (aonia_fixings_path, "rba\\aonia_fixings_pull.py --bbg-incremental")}
                fp, puller = fmap[path]
                fp = fp or os.path.join(SCRIPT_DIR, path.strip("/") + ".json")
                if os.path.isfile(fp):
                    with open(fp, "rb") as f: data = f.read()
                    self._hdr(200, "application/json"); self.wfile.write(data)
                else:
                    self._json(404, {"error": f"{os.path.basename(fp)} not present yet — run {puller} "
                                              "(start_pricer.bat does this automatically on launch)"})
            elif path in ("", "/index.html", "/unifiedpricer.html", "/UnifiedPricer.html", "/swappricer.html", "/SwapPricer.html"):
                if ui_path and os.path.isfile(ui_path):
                    with open(ui_path, "rb") as f: data = f.read()
                    self._hdr(200, "text/html; charset=utf-8"); self.wfile.write(data)
                else:
                    self._hdr(404, "text/html"); self.wfile.write(
                        f"<h3>SwapPricer.html not found at {ui_path}</h3><p>Feed: <a href='/curve'>/curve</a></p>".encode())
            else:
                self._json(404, {"error": "not found"})
        def log_message(self, *a): pass

    httpd = None
    for cand in [http_port, 8199, 8901, 9323, 0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), H); break
        except OSError as e:
            print(f"[citi] port {cand} unavailable ({e}); trying next...")
    if httpd is None:
        raise SystemExit("[citi] could not bind any port.")
    bound = httpd.server_address[1]; base = f"http://localhost:{bound}"
    print("=" * 60)
    print(f"[citi] UID  : {base}/    (serves UnifiedPricer.html)")
    print(f"[citi] feed : {base}/curve")
    print(f"[citi] >>> OPEN THIS IN A BROWSER:  {base}/")
    print("=" * 60)
    if open_browser:
        try: webbrowser.open(base + "/")
        except Exception: pass
    httpd.serve_forever()


def main():
    p = argparse.ArgumentParser(description="CitiVelocity live SOFR par-curve feed (WebSocket bridge)")
    p.add_argument("--serve", action="store_true", help="run the HTTP server for the pricer to fetch")
    p.add_argument("--http-port", type=int, default=8198)
    p.add_argument("--mock", action="store_true", help="subscribe MOCK.n instead of real tags (off-hours test)")
    p.add_argument("--proxy", default=None, help="corporate proxy host:port (else uses HTTPS_PROXY)")
    p.add_argument("--secrets", default=os.path.join(SCRIPT_DIR, "..", "..", "common", "secrets.env"),
                   help="path to KEY=VALUE creds file (default: ../../common/secrets.env in Data Feeds)")
    p.add_argument("--ui", default=os.path.join(SCRIPT_DIR, "UnifiedPricer.html"))
    p.add_argument("--fixings", default=os.path.join(SCRIPT_DIR, "sofr_fixings.json"),
                   help="path to sofr_fixings.json (default: alongside this script; "
                        "start_pricer.bat points this at ..\\..\\nyfed\\sofr_fixings.json)")
    p.add_argument("--tona-fixings", default=os.path.join(SCRIPT_DIR, "tona_fixings.json"),
                   help="path to tona_fixings.json (start_pricer.bat points this at "
                        "..\\..\\boj\\tona_fixings.json)")
    p.add_argument("--sonia-fixings", default=os.path.join(SCRIPT_DIR, "sonia_fixings.json"),
                   help="path to sonia_fixings.json (start_pricer.bat: ..\\..\\boe\\sonia_fixings.json)")
    p.add_argument("--corra-fixings", default=os.path.join(SCRIPT_DIR, "corra_fixings.json"),
                   help="path to corra_fixings.json (start_pricer.bat: ..\\..\\boc\\corra_fixings.json)")
    p.add_argument("--estr-fixings", default=os.path.join(SCRIPT_DIR, "estr_fixings.json"),
                   help="path to estr_fixings.json (start_pricer.bat: ..\\..\\ecb\\estr_fixings.json)")
    p.add_argument("--aonia-fixings", default=os.path.join(SCRIPT_DIR, "aonia_fixings.json"),
                   help="path to aonia_fixings.json (start_pricer.bat: ..\\..\\rba\\aonia_fixings.json)")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--listen-seconds", type=int, default=0, help="snapshot mode: listen N sec then dump /curve JSON")
    p.add_argument("--out", default=None)
    p.add_argument("--print-tags", action="store_true")
    p.add_argument("--test-token", action="store_true", help="diagnose which OAuth request form the token endpoint accepts")
    p.add_argument("--probe", action="store_true", help="subscribe all tags, report which SUBACK OK / ERROR / tick, then exit")
    p.add_argument("--probe-seconds", type=int, default=12, help="how long --probe listens (default 12s)")
    args = p.parse_args()

    if args.print_tags:
        for s in build_subs(): print(f"{s['kind']:4s} {s.get('ccy','USD'):4s} {s.get('leg','ois'):4s} {s['key']:5s} {s['tag']}")
        return

    load_secrets(args.secrets)
    cid = os.environ.get("CITI_CLIENT_ID"); csec = os.environ.get("CITI_CLIENT_SECRET")
    if args.test_token:
        if not cid or not csec:
            raise SystemExit("Need CITI_CLIENT_ID / CITI_CLIENT_SECRET (env or --secrets file).")
        test_token_variants(cid, csec); return
    if args.probe:
        if not cid or not csec:
            raise SystemExit("Need CITI_CLIENT_ID / CITI_CLIENT_SECRET (env or --secrets file) for --probe.")
        run_probe(cid, csec, args.mock, args.proxy, args.probe_seconds); return
    if not args.mock and (not cid or not csec):
        raise SystemExit("Set CITI_CLIENT_ID / CITI_CLIENT_SECRET (env or --secrets file). "
                         "Use --mock to test the pipeline without credentials... "
                         "(mock still needs a token; for a pure offline UI test, load the page and edit quotes).")

    state = CurveState()
    th = threading.Thread(target=run_stream,
                          args=(state, cid, csec, args.mock, args.proxy), daemon=True)
    th.start()

    def _heartbeat():
        while True:
            time.sleep(30); d = state.diag
            print(f"[citi] status: connected={d['connected']} conn_ready={d['conn_ready']} "
                  f"subscribed={d['subscribed']}/{len(build_subs())} ticks={d['binary_msgs']}"
                  + (f" suback_err={len(d['suback_err'])}" if d['suback_err'] else "")
                  + (f" CONN_ERROR={d['conn_error']}" if d['conn_error'] else ""))
    threading.Thread(target=_heartbeat, daemon=True).start()

    if args.listen_seconds:
        print(f"[citi] listening {args.listen_seconds}s...")
        time.sleep(args.listen_seconds)
        payload = state.payload()
        text = json.dumps(payload, indent=2)
        if args.out:
            open(args.out, "w", encoding="utf-8").write(text); print(f"wrote {args.out}")
        print(text)
        return

    if args.serve:
        serve(state, args.http_port, args.ui, not args.no_open, args.fixings, args.tona_fixings,
              args.sonia_fixings, args.corra_fixings, args.estr_fixings, args.aonia_fixings)
    else:
        print("Nothing to do. Use --serve (HTTP bridge) or --listen-seconds N (snapshot).")


if __name__ == "__main__":
    main()
