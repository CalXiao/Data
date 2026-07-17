#!/usr/bin/env python3
r"""
structures.py — SHARED swap-structure definitions for the RV stack (carry/roll,
correlation decomposition, mean reversion). Swap-native sibling of
Seasonals\universe.py (which is govvie cash); import this from both stacks
rather than re-defining flies in a third place.

A structure = {"name", "legs": [(expiry, tenor, weight), ...]}
  expiry None  -> spot par pillar (store product swap_par, tenor key)
  expiry "5Y"  -> forward point   (store product forward_swap, FWD.<expiry>.<tenor>)
  weight       -> rate-space weight in the level definition (bp of structure =
                  Σ w · rate_bp). Conventional weights here (curves ±1, flies
                  -1/+2/-1); PCA/regression weights are a v2 overlay computed
                  from history, NOT hardcoded.

v1 universe (Calvin, 2026-07-08): USD first; pillar focus {2,5,7,10,12,15,20,
25,30}; tails {1Y,2Y,5Y}; classic spot curves/flies + generated forward-space
combinations + explicit mixed-tail pairs. Generators VALIDATE against the
available (expiry, tenor) grid at runtime and report anything dropped.
"""

PILLARS = ["2Y", "5Y", "7Y", "10Y", "12Y", "15Y", "20Y", "25Y", "30Y"]
TAILS = ["1Y", "2Y", "5Y"]
FWD_EXPIRY_FOCUS = ["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "12Y", "15Y", "20Y", "25Y", "30Y"]

# ---- spot structures (par pillars) ----
SPOT_CURVES = {
    "2s5s":   [(None, "2Y", -1), (None, "5Y", 1)],
    "2s10s":  [(None, "2Y", -1), (None, "10Y", 1)],
    "5s10s":  [(None, "5Y", -1), (None, "10Y", 1)],
    "5s30s":  [(None, "5Y", -1), (None, "30Y", 1)],
    "10s30s": [(None, "10Y", -1), (None, "30Y", 1)],
}
SPOT_FLIES = {
    "2s5s10s":   [(None, "2Y", -1), (None, "5Y", 2), (None, "10Y", -1)],
    "5s10s30s":  [(None, "5Y", -1), (None, "10Y", 2), (None, "30Y", -1)],
    "5s7s10s":   [(None, "5Y", -1), (None, "7Y", 2), (None, "10Y", -1)],
    "10s20s30s": [(None, "10Y", -1), (None, "20Y", 2), (None, "30Y", -1)],
}

# ---- explicit mixed-tail forward pairs (Calvin's examples) ----
EXPLICIT_FWD_CURVES = {
    "1y1y-2y1y": [("1Y", "1Y", -1), ("2Y", "1Y", 1)],
    "1y1y-2y3y": [("1Y", "1Y", -1), ("2Y", "3Y", 1)],
    "3y2y-5y5y": [("3Y", "2Y", -1), ("5Y", "5Y", 1)],
    "5y2y-7y3y": [("5Y", "2Y", -1), ("7Y", "3Y", 1)],
    "5y2y-7y5y": [("5Y", "2Y", -1), ("7Y", "5Y", 1)],
}


def _n(t):  # "10Y" -> 10
    return int(t[:-1])


# 1Y-tail structures beyond the 10y point are marking noise (Calvin 2026-07-08):
# any leg with a 1Y tail must satisfy expiry + tenor <= 10y.
def _leg_ok(e, t):
    return not (t == "1Y" and _n(e) + 1 > 10)


def gen_convexity_curves(available, tails=("5Y", "10Y"), expiries=FWD_EXPIRY_FOCUS):
    """Long-end convexity pairs (Calvin 2026-07-08): CONJOINT two-leg curves in
    5y/10y tails — the second window starts exactly where the first ends.
    Examples: 10y5y-15y5y, 10y10y-20y10y, 5y5y-10y10y, 5y10y-15y10y."""
    ys = sorted({_n(p) for p in expiries})
    out = {}
    for t1 in tails:
        for t2 in tails:
            for a in ys:
                b = a + _n(t1)
                if a + _n(t1) + _n(t2) > 40:
                    continue
                legs = [(f"{a}Y", t1, -1), (f"{b}Y", t2, 1)]
                if all((e, t) in available for e, t, _ in legs):
                    out[f"{a}y{_n(t1)}y-{b}y{_n(t2)}y"] = legs
    return out


def gen_fwd_curves(available, tails=TAILS, expiries=FWD_EXPIRY_FOCUS, max_gap=3):
    """Same-tail forward curves: (e1,t)-(e2,t) for consecutive-ish pillar
    expiries (e2 within max_gap positions of e1). available = set of
    (expiry, tenor) present in the store."""
    out = {}
    for t in tails:
        es = [e for e in expiries if (e, t) in available and _leg_ok(e, t)]
        for i in range(len(es)):
            for j in range(i + 1, min(i + 1 + max_gap, len(es))):
                e1, e2 = es[i], es[j]
                out[f"{_n(e1)}y{_n(t)}y-{_n(e2)}y{_n(t)}y"] = [(e1, t, -1), (e2, t, 1)]
    return out


def gen_fwd_flies(available, tails=TAILS, expiries=FWD_EXPIRY_FOCUS):
    """Constant-tail expiry flies — NON-OVERLAPPING only (2026-07-08, Calvin):
    each leg's underlying window [e, e+t] must not overlap the next leg's, i.e.
    expiry step >= tail. 1y1y-2y1y-3y1y survives (tiles 1-2-3-4); the old
    5y5y-7y5y-10y5y style (windows 5-10/7-12/10-15) is stripped — post-PCA those
    legs are near-duplicates and the fly is hedge-messy. Contiguous coverage of
    mixed-width buckets comes from gen_bucket_flies below."""
    out = {}
    for t in tails:
        es = [e for e in expiries if (e, t) in available and _leg_ok(e, t)]
        for i in range(len(es) - 2):
            e1, e2, e3 = es[i], es[i + 1], es[i + 2]
            # CONJOINT only (2026-07-08): each window must END where the next
            # BEGINS — no overlap AND no gap (5y2y-7y2y-10y2y has a 9-10y hole;
            # the gap-free version of that idea is the bucket fly 5y2y-7y3y-10y2y).
            if _n(e2) != _n(e1) + _n(t) or _n(e3) != _n(e2) + _n(t):
                continue
            out[f"{_n(e1)}y{_n(t)}y-{_n(e2)}y{_n(t)}y-{_n(e3)}y{_n(t)}y"] = \
                [(e1, t, -1), (e2, t, 2), (e3, t, -1)]
    return out


def gen_bucket_flies(available, pillars=FWD_EXPIRY_FOCUS, max_span=15):
    """Bucket flies: three CONTIGUOUS non-overlapping forward windows tiling the
    curve between four pillar split-points a<b<c<d — legs (a, b-a), (b, c-b),
    (c, d-c), weights -1/+2/-1. Calvin's example 5y5y-10y2y-12y3y = splits
    {5,10,12,15}. Every window is a distinct curve bucket (clean post-PCA
    interpretation, tradable legs). max_span caps d-a to keep the set liquid."""
    from itertools import combinations
    ys = sorted({_n(p) for p in pillars})
    out = {}
    for a, b, c, d in combinations(ys, 4):
        if d - a > max_span:
            continue
        w = [b - a, c - b, d - c]
        if max(w) > 5 * min(w):
            continue   # grotesquely asymmetric buckets (1y wing vs 10y belly) — not tradable flies
        legs = [(f"{a}Y", f"{b-a}Y", -1), (f"{b}Y", f"{c-b}Y", 2), (f"{c}Y", f"{d-c}Y", -1)]
        if not all(_leg_ok(e, t) for e, t, _ in legs):
            continue   # 1y-wide buckets past the 10y point = marking noise
        if all((e, t) in available for e, t, _ in legs):
            out[f"{a}y{b-a}y-{b}y{c-b}y-{c}y{d-c}y"] = legs
    return out


def build_universe(available_fwd, available_par):
    """Assemble the full validated universe. available_fwd = set of (expiry,
    tenor); available_par = set of tenor. Returns (universe, dropped)."""
    uni, dropped = {}, []
    def ok(legs):
        for e, t, w in legs:
            if e is None:
                if t not in available_par: return False
            elif (e, t) not in available_fwd: return False
        return True
    for group in (SPOT_CURVES, SPOT_FLIES, EXPLICIT_FWD_CURVES,
                  gen_fwd_curves(available_fwd), gen_fwd_flies(available_fwd),
                  gen_bucket_flies(available_fwd), gen_convexity_curves(available_fwd)):
        for name, legs in group.items():
            (uni.__setitem__(name, legs) if ok(legs) else dropped.append(name))
    return uni, dropped


def level(legs, par, fwd):
    """Structure level in bp. par: {tenor: %}; fwd: {(expiry,tenor): %}."""
    s = 0.0
    for e, t, w in legs:
        v = par.get(t) if e is None else fwd.get((e, t))
        if v is None:
            return None
        s += w * v * 100.0
    return s
