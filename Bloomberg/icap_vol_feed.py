#!/usr/bin/env python3
"""
icap_vol_feed.py
================
Pulls the ICAP USD swaption *normal* (Bachelier) vol surface from Bloomberg via
the Python Desktop API (`blpapi`) and exposes it to the Swaption Pricer artifact.

It reproduces the EXACT ticker grid from RatesMon.xlsm M23:V40 (12Y row dropped):
    USSNA<expiry><tail> ICPL Curncy   ->   PX_LAST

Two modes
---------
1. Snapshot (default): one ReferenceDataRequest, write JSON to --out and print it.
       python icap_vol_feed.py --out icap_surface.json

2. Server: serves BOTH the pricer UI and the data feed from one origin (so the
   in-app "LOAD ICAP" button works with no CORS / mixed-content issues), cached and
   periodically refreshed so you don't hammer the terminal.
       python icap_vol_feed.py --serve --http-port 8195 --interval 60
   Then open  http://localhost:8195/  in a browser, go to the Vol Surface tab and
   click "LOAD ICAP".  (Requires SwaptionPricer.html next to this script, or --ui.)
   The raw JSON is also at  http://localhost:8195/surface .

Requirements
------------
- A logged-in Bloomberg Terminal on this machine (Desktop API / DAPI on :8194),
  or a B-PIPE/SAPI session (pass --host/--port accordingly).
- blpapi:  pip install --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi

JSON payload shape (consumed by the artifact)
---------------------------------------------
{
  "asof": "2026-06-18T14:32:05Z",
  "source": "ICAP/Parameta via Bloomberg blpapi (USSNA.. ICPL Curncy)",
  "field": "PX_LAST",
  "surface": { "1M": {"1Y": 90.1, "2Y": 84.0, ...}, ... },
  "tickers": { "1M|1Y": "USSNAA1 ICPL Curncy", ... },
  "missing": ["10Y|10Y", ...]
}
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# Grid + ticker construction  (identical to the artifact / RatesMon.xlsm)
# ----------------------------------------------------------------------------
SURF_EXP = ["1M", "2M", "3M", "6M", "9M", "1Y", "18M", "2Y", "3Y",
            "4Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
SURF_TEN = ["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]

EXP_TOKEN = {"1M": "A", "2M": "B", "3M": "C", "6M": "F", "9M": "I",
             "1Y": "1", "18M": "1F", "2Y": "2", "3Y": "3", "4Y": "4",
             "5Y": "5", "7Y": "7", "10Y": "10", "15Y": "15", "20Y": "20", "30Y": "30"}
TAIL_NUM = {"1Y": "1", "2Y": "2", "3Y": "3", "5Y": "5", "7Y": "7",
            "10Y": "10", "15Y": "15", "20Y": "20", "30Y": "30"}
LONG_LETTER = {"10Y": "J", "15Y": "O", "20Y": "T", "30Y": "Z"}
SUFFIX = " ICPL Curncy"


def icap_ticker(exp: str, tail: str) -> str:
    """Exact per-cell replication of the RatesMon BDP formulas."""
    long_tail = tail in LONG_LETTER
    if exp == "18M":
        body = "1F" + (LONG_LETTER[tail] if long_tail else TAIL_NUM[tail])
    elif exp in ("10Y", "15Y", "20Y", "30Y"):
        body = (LONG_LETTER[exp] if long_tail else EXP_TOKEN[exp]) + TAIL_NUM[tail]
    else:
        body = EXP_TOKEN[exp] + TAIL_NUM[tail]
    return f"USSNA{body}{SUFFIX}"


def build_ticker_map():
    """{ 'EXP|TAIL': ticker }  and reverse { ticker: (EXP,TAIL) }."""
    fwd, rev = {}, {}
    for e in SURF_EXP:
        for t in SURF_TEN:
            tk = icap_ticker(e, t)
            fwd[f"{e}|{t}"] = tk
            rev[tk] = (e, t)
    return fwd, rev


# ----------------------------------------------------------------------------
# Bloomberg pull
# ----------------------------------------------------------------------------
def fetch_surface(host="localhost", port=8194, field="PX_LAST", timeout_s=30):
    """Return (surface_dict, ticker_map, missing_list). Raises on session failure."""
    try:
        import blpapi  # noqa
    except ImportError as e:
        raise SystemExit(
            "blpapi is not installed. Install with:\n"
            "  pip install --index-url "
            "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi\n"
            f"(import error: {e})"
        )

    fwd, rev = build_ticker_map()
    securities = sorted(set(fwd.values()))

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError(f"Failed to start blpapi session on {host}:{port} "
                           "(is the Terminal running / DAPI enabled?)")
    try:
        if not session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        refdata = session.getService("//blp/refdata")
        request = refdata.createRequest("ReferenceDataRequest")
        for s in securities:
            request.getElement("securities").appendValue(s)
        request.getElement("fields").appendValue(field)
        # ICAP marks are quoted in bp; PX_LAST comes back in the security's native unit.
        session.sendRequest(request)

        values = {}      # ticker -> float
        deadline = time.time() + timeout_s
        done = False
        while not done and time.time() < deadline:
            ev = session.nextEvent(500)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    tk = sd.getElementAsString("security")
                    if sd.hasElement("securityError"):
                        continue
                    fd = sd.getElement("fieldData")
                    if fd.hasElement(field):
                        try:
                            values[tk] = float(fd.getElementAsFloat(field))
                        except Exception:
                            pass
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        session.stop()

    # map back into nested expiry->tail dict
    surface = {e: {t: None for t in SURF_TEN} for e in SURF_EXP}
    missing = []
    for key, tk in fwd.items():
        e, t = key.split("|")
        v = values.get(tk)
        surface[e][t] = v
        if v is None:
            missing.append(key)
    return surface, fwd, missing


def make_payload(surface, ticker_map, missing, field):
    return {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ICAP/Parameta via Bloomberg blpapi (USSNA.. ICPL Curncy)",
        "field": field,
        "surface": surface,
        "tickers": ticker_map,
        "missing": missing,
    }


# ----------------------------------------------------------------------------
# HTTP server (cached, periodic refresh, CORS-enabled)
# ----------------------------------------------------------------------------
class _Cache:
    def __init__(self, host, port, field, interval):
        self.host, self.port, self.field, self.interval = host, port, field, interval
        self.lock = threading.Lock()
        self.payload = None
        self.last = 0.0

    def get(self, force=False):
        with self.lock:
            stale = (time.time() - self.last) > self.interval
            if force or stale or self.payload is None:
                try:
                    surface, tk, missing = fetch_surface(self.host, self.port, self.field)
                    self.payload = make_payload(surface, tk, missing, self.field)
                    self.last = time.time()
                except Exception as e:
                    if self.payload is None:
                        self.payload = {"error": str(e), "surface": {}}
            return self.payload


def serve(host, port, field, http_port, interval, ui_path, open_browser):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    cache = _Cache(host, port, field, interval)

    class Handler(BaseHTTPRequestHandler):
        def _headers(self, code, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _send(self, code, body):  # JSON / text
            self._headers(code, "application/json")
            self.wfile.write(body.encode("utf-8"))

        def _send_html(self):
            if ui_path and os.path.isfile(ui_path):
                with open(ui_path, "rb") as f:
                    data = f.read()
                self._headers(200, "text/html; charset=utf-8")
                self.wfile.write(data)
            else:
                self._headers(404, "text/html; charset=utf-8")
                self.wfile.write(
                    (f"<h3>UI file not found</h3><p>Expected SwaptionPricer.html at "
                     f"<code>{ui_path}</code>. Put it next to this script, or pass --ui &lt;path&gt;.</p>"
                     f"<p>The data feed is live at <a href='/surface'>/surface</a>.</p>").encode("utf-8"))

        def do_OPTIONS(self):
            self._headers(204, "application/json")

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path in ("/surface", "/health"):
                force = "force" in self.path or "refresh" in self.path
                self._send(200, json.dumps(cache.get(force=force)))
            elif path == "/snapshot":
                snap = os.path.join(SCRIPT_DIR, "icap_surface.json")
                if os.path.isfile(snap):
                    with open(snap, encoding="utf-8") as fh:
                        self._send(200, fh.read())
                else:
                    self._send(404, json.dumps({"error": f"icap_surface.json not found in {SCRIPT_DIR}"}))
            elif path == "/open-folder":
                try:
                    if hasattr(os, "startfile"):
                        os.startfile(SCRIPT_DIR)  # Windows Explorer
                    else:
                        import subprocess
                        subprocess.Popen(["xdg-open", SCRIPT_DIR])
                    self._send(200, json.dumps({"ok": True, "dir": SCRIPT_DIR}))
                except Exception as e:
                    self._send(200, json.dumps({"ok": False, "error": str(e)}))
            elif path in ("", "/index.html", "/swaptionpricer.html", "/SwaptionPricer.html"):
                self._send_html()
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def log_message(self, *a):  # quiet
            pass

    # Bind to loopback only. Binding "0.0.0.0" (all interfaces) is what triggers
    # WinError 10013 on locked-down Windows; 127.0.0.1 is enough for a local UI.
    # Fall back through alternate ports if the requested one is in a Windows /
    # Hyper-V / WSL "excluded port range" (also raises 10013). 0 = OS-chosen free port.
    candidates = [http_port, 8787, 8899, 9321, 0]
    httpd = None
    for cand in candidates:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            break
        except OSError as e:
            print(f"[icap_vol_feed] port {cand} unavailable ({e}); trying next...")
    if httpd is None:
        raise SystemExit("[icap_vol_feed] could not bind any port. "
                         "Run with --http-port <N> using a known-free port.")
    bound = httpd.server_address[1]
    base = f"http://localhost:{bound}"
    print("=" * 60)
    print(f"[icap_vol_feed] UI    : {base}/")
    print(f"[icap_vol_feed] feed  : {base}/surface")
    print(f"[icap_vol_feed] blpapi: {host}:{port}  refresh {interval}s")
    if not (ui_path and os.path.isfile(ui_path)):
        print(f"[icap_vol_feed] NOTE: UI file not found at {ui_path} — save SwaptionPricer.html there.")
    print(f"[icap_vol_feed] >>> OPEN THIS IN A BROWSER:  {base}/")
    print("=" * 60)
    if open_browser:
        try:
            webbrowser.open(base + "/")
        except Exception:
            pass
    cache.get(force=True)  # warm
    httpd.serve_forever()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="ICAP swaption normal-vol feed via Bloomberg blpapi")
    p.add_argument("--host", default="localhost", help="blpapi server host (default localhost / DAPI)")
    p.add_argument("--port", type=int, default=8194, help="blpapi server port (default 8194)")
    p.add_argument("--field", default="PX_LAST", help="Bloomberg field (default PX_LAST)")
    p.add_argument("--out", default=None, help="write JSON snapshot to this path")
    p.add_argument("--serve", action="store_true", help="run HTTP server for the artifact to fetch")
    p.add_argument("--http-port", type=int, default=8195, help="HTTP server port (default 8195)")
    p.add_argument("--interval", type=int, default=60, help="server cache refresh seconds (default 60)")
    p.add_argument("--ui", default=os.path.join(SCRIPT_DIR, "SwaptionPricer.html"),
                   help="path to the pricer HTML to serve at '/' (default: alongside this script)")
    p.add_argument("--no-open", action="store_true", help="do not auto-open the browser when serving")
    p.add_argument("--print-tickers", action="store_true", help="just print the ticker grid and exit")
    args = p.parse_args()

    if args.print_tickers:
        fwd, _ = build_ticker_map()
        for k, v in fwd.items():
            print(f"{k:10s} {v}")
        return

    if args.serve:
        ser