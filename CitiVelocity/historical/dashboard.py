"""
dashboard.py -- rough-shod RV monitor across ALL products, one tab per currency.

Per instrument: LAST, 3M High/Low, 3M Carry/Roll/Carry+Roll (forwards only),
and 1Y/3Y/5Y/10Y High/Low/Z-score. Window groups toggle via checkboxes; product
rows filter via checkboxes; click any column header to sort (desc then asc).

Trailing-window stats computed in DuckDB (one pass). Self-contained HTML.
Regenerate any time:  python dashboard.py
"""
from __future__ import annotations
import json
import numpy as np
import pandas as pd
import analytics as A

PRODUCTS = ("swap_par", "forward_swap", "swap_spread", "midcurve", "bond_tips", "vol")
CCYS = ["USD", "EUR", "GBP", "AUD", "JPY", "CAD"]
WIN_DAYS = {"1Y": 365, "3Y": 1095, "5Y": 1825, "10Y": 3650}

STATS_SQL = r"""
WITH base AS (
  SELECT tag,date,value,currency,product,basis,index_name,expiry,tenor,vol_type,measure
  FROM rates
  WHERE product IN ('swap_par','forward_swap','swap_spread','midcurve','bond_tips','vol')
    AND value IS NOT NULL
    AND (index_name IS NULL OR index_name NOT LIKE '%\_%' ESCAPE '\')
    AND (
      (product IN ('swap_par','forward_swap','swap_spread') AND (
        (currency IN ('USD','GBP','CAD','JPY') AND basis='RFR') OR
        (currency IN ('EUR','AUD') AND basis='IBOR')))
      OR product IN ('midcurve','bond_tips','vol')
    )
),
md AS (SELECT tag, max(date) ld FROM base GROUP BY tag),
j AS (SELECT b.*, date_diff('day', b.date, md.ld) AS age FROM base b JOIN md USING(tag))
SELECT tag,
  any_value(currency) currency, any_value(product) product, any_value(basis) basis,
  any_value(expiry) expiry, any_value(tenor) tenor, any_value(vol_type) vol_type,
  any_value(measure) measure,
  arg_max(value,date) lastv,
  max(value) FILTER(WHERE age<=90)   h3m,  min(value) FILTER(WHERE age<=90)   l3m,
  max(value) FILTER(WHERE age<=365)  h1,   min(value) FILTER(WHERE age<=365)  l1,
  avg(value) FILTER(WHERE age<=365)  a1,   stddev_pop(value) FILTER(WHERE age<=365)  s1,
  max(value) FILTER(WHERE age<=1095) h3,   min(value) FILTER(WHERE age<=1095) l3,
  avg(value) FILTER(WHERE age<=1095) a3,   stddev_pop(value) FILTER(WHERE age<=1095) s3,
  max(value) FILTER(WHERE age<=1825) h5,   min(value) FILTER(WHERE age<=1825) l5,
  avg(value) FILTER(WHERE age<=1825) a5,   stddev_pop(value) FILTER(WHERE age<=1825) s5,
  max(value) FILTER(WHERE age<=3650) h10,  min(value) FILTER(WHERE age<=3650) l10,
  avg(value) FILTER(WHERE age<=3650) a10,  stddev_pop(value) FILTER(WHERE age<=3650) s10
FROM j GROUP BY tag
"""

CARRY_SQL = r"""
SELECT currency,expiry,tenor,measure,value,basis
FROM rates
WHERE product='roll_carry' AND (index_name IS NULL OR index_name NOT LIKE '%\_%' ESCAPE '\')
QUALIFY row_number() OVER (PARTITION BY tag ORDER BY date DESC)=1
"""


def _unit(p, vt, meas):
    if p in ("swap_par", "forward_swap"):
        return "%"
    if p == "swap_spread":
        return "bp"
    if p == "midcurve":
        return "bpvol"
    if p == "vol":
        return "bpvol" if vt == "NORMAL" else "prem"
    if p == "bond_tips":
        return {"YIELD": "%", "REAL_YIELD": "%", "BREAKEVENS": "bp", "PRICE": "px"}.get(meas, "")
    return ""


def _label(p, exp, ten, vt, meas, basis):
    if p in ("swap_par", "swap_spread"):
        return ten
    if p in ("forward_swap", "midcurve"):
        return f"{exp}x{ten}"
    if p == "vol":
        return f"{exp}x{ten} {vt}" + ("" if basis == "RFR" else " (leg)")
    if p == "bond_tips":
        return f"{ten} {meas}"
    return p


def _z(last, a, s):
    if a is None or s is None or s == 0 or pd.isna(s):
        return None
    return float((last - a) / s)


def build(config):
    con = A.connect(config)
    df = con.execute(STATS_SQL).df()
    rc = con.execute(CARRY_SQL).df()
    con.close()

    # carry basis must match the ccy rule used above
    def keep_basis(r):
        if r.currency in ("USD", "GBP", "CAD", "JPY"):
            return r.basis == "RFR"
        return r.basis == "IBOR"
    rc = rc[rc.apply(keep_basis, axis=1)]
    carry = {(r.currency, r.expiry, r.tenor, r.measure): r.value for r in rc.itertuples()}

    def r4(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else round(float(v), 4)
    def r2(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else round(float(v), 2)

    out = {}
    for ccy in CCYS:
        d = df[df.currency == ccy].copy()
        if d.empty:
            continue
        # vol: dedup per (expiry,tenor), prefer RFR (ATM_RFR) over legacy ATM
        dv = d[d["product"] == "vol"].copy()
        if len(dv):
            dv["_rfr"] = (dv["basis"] == "RFR").astype(int)
            dv = dv.sort_values("_rfr", ascending=False).drop_duplicates(["expiry", "tenor"])
        d = pd.concat([d[d["product"] != "vol"], dv], ignore_index=True)
        rows = []
        for r in d.itertuples():
            is_fwd = r.product == "forward_swap"
            rows.append({
                "product": r.product,
                "inst": _label(r.product, r.expiry, r.tenor, r.vol_type, r.measure, r.basis),
                "unit": _unit(r.product, r.vol_type, r.measure),
                "last": r4(r.lastv), "h3m": r4(r.h3m), "l3m": r4(r.l3m),
                "carry": r2(carry.get((ccy, r.expiry, r.tenor, "CARRY"))) if is_fwd else None,
                "roll": r2(carry.get((ccy, r.expiry, r.tenor, "ROLL"))) if is_fwd else None,
                "total": r2(carry.get((ccy, r.expiry, r.tenor, "TOTAL_CARRY"))) if is_fwd else None,
                "h1": r4(r.h1), "l1": r4(r.l1), "z1": r2(_z(r.lastv, r.a1, r.s1)),
                "h3": r4(r.h3), "l3": r4(r.l3), "z3": r2(_z(r.lastv, r.a3, r.s3)),
                "h5": r4(r.h5), "l5": r4(r.l5), "z5": r2(_z(r.lastv, r.a5, r.s5)),
                "h10": r4(r.h10), "l10": r4(r.l10), "z10": r2(_z(r.lastv, r.a10, r.s10)),
            })
        rows.sort(key=lambda x: (x["product"], x["inst"]))
        out[ccy] = {"rows": rows, "n": len(rows)}
    return out


TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Rates RV Monitor</title>
<style>
:root{--bg:#0f1419;--card:#161d27;--line:#2a3340;--fg:#e3e9f0;--mut:#8aa0b4;--acc:#1f6feb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:10px 14px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#0b0f14;z-index:4}
h1{font-size:15px;margin:0 0 8px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.tab{padding:5px 13px;border:1px solid var(--line);border-radius:6px;cursor:pointer;background:var(--card)}
.tab.active{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}
.controls{font-size:12px;color:var(--mut);display:flex;gap:18px;flex-wrap:wrap}
.controls label{cursor:pointer;margin-right:8px}
.wrap{padding:8px 10px;overflow:auto;height:calc(100vh - 120px)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:3px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background:#11161d;color:var(--mut);font-weight:600;font-size:11px;cursor:pointer;user-select:none}
th:hover{color:#fff}
td.l,th.l{text-align:left}
td.prod,th.prod{text-align:left;position:sticky;left:0;background:var(--bg)}
td.inst,th.inst{text-align:left;position:sticky;left:74px;background:var(--bg);font-family:ui-monospace,Menlo,monospace}
.grp{border-left:2px solid var(--line)}
.hide{display:none}
tbody tr:hover td{background:#1a2330}
.muted{color:var(--mut)}
.arrow{font-size:9px;color:var(--acc)}
</style></head><body>
<header>
  <h1>Rates RV Monitor <span class="muted" id="meta"></span></h1>
  <div class="tabs" id="tabs"></div>
  <div class="controls">
    <div>windows:
      <label><input type="checkbox" class="wtog" value="1" checked>1Y</label>
      <label><input type="checkbox" class="wtog" value="3" checked>3Y</label>
      <label><input type="checkbox" class="wtog" value="5" checked>5Y</label>
      <label><input type="checkbox" class="wtog" value="10" checked>10Y</label></div>
    <div id="prodtog">products:</div>
    <div class="muted">click a header to sort &middot; %=rate, bp/bpvol &middot; z=(last-mean)/std</div>
  </div>
</header>
<div class="wrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
<script>
const DATA = __DATA__;
const WINS=[["1","1Y"],["3","3Y"],["5","5Y"],["10","10Y"]];
let cur=Object.keys(DATA)[0], sortKey=null, sortDir=-1;
const COLS=[
 {k:'product',t:'Product',c:'prod l',num:false},
 {k:'inst',t:'Instrument',c:'inst l',num:false},
 {k:'unit',t:'Unit',c:'l',num:false},
 {k:'last',t:'Last',num:true},{k:'h3m',t:'3M Hi',num:true},{k:'l3m',t:'3M Lo',num:true},
 {k:'carry',t:'3M Carry',num:true},{k:'roll',t:'3M Roll',num:true},{k:'total',t:'3M C+R',num:true},
];
const WINCOLS=w=>[{k:'h'+w,t:w+'Y Hi',win:w,grp:1},{k:'l'+w,t:w+'Y Lo',win:w},{k:'z'+w,t:w+'Y Z',win:w,z:1}];
function allCols(){let c=[...COLS];WINS.forEach(([w])=>c.push(...WINCOLS(w)));return c;}
function fmt(v,unit,z){if(v==null)return '<span class=muted>-</span>';
 if(z)return zcell(v);
 if(typeof v!=='number')return v;
 const dp=(unit==='%'||unit==='px')?3:1;return v.toFixed(dp);}
function zcell(z){const a=Math.min(Math.abs(z)/3,1);
 const col=z>0?`rgba(255,120,90,${0.12+0.5*a})`:`rgba(90,160,200,${0.12+0.5*a})`;
 return `<span style="background:${col};padding:1px 5px;border-radius:4px">${z.toFixed(2)}</span>`;}
function winsOn(){return new Set([...document.querySelectorAll('.wtog:checked')].map(c=>c.value));}
function prodsOn(){return new Set([...document.querySelectorAll('.ptog:checked')].map(c=>c.value));}
function tabs(){const t=document.getElementById('tabs');t.innerHTML='';
 Object.keys(DATA).forEach(c=>{const d=document.createElement('div');d.className='tab'+(c===cur?' active':'');
  d.textContent=c+' ('+DATA[c].n+')';d.onclick=()=>{cur=c;sortKey=null;render()};t.appendChild(d);});}
function buildProdTog(){const set=new Set();Object.values(DATA).forEach(d=>d.rows.forEach(r=>set.add(r.product)));
 const box=document.getElementById('prodtog');box.innerHTML='products: ';
 [...set].sort().forEach(p=>{const l=document.createElement('label');
  l.innerHTML=`<input type="checkbox" class="ptog" value="${p}" checked>${p}`;box.appendChild(l);});
 document.querySelectorAll('.ptog').forEach(c=>c.onchange=render);}
function header(){const won=winsOn();let h='<tr>';
 allCols().forEach(col=>{if(col.win&&!won.has(col.win))return;
  const cls=(col.c||'')+(col.grp?' grp':'');
  const ar=sortKey===col.k?` <span class=arrow>${sortDir<0?'▼':'▲'}</span>`:'';
  h+=`<th class="${cls}" data-k="${col.k}" data-num="${col.num!==false}">${col.t}${ar}</th>`;});
 h+='</tr>';document.querySelector('#tbl thead').innerHTML=h;
 document.querySelectorAll('#tbl thead th').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;if(sortKey===k){sortDir*=-1}else{sortKey=k;sortDir=-1}render();});}
function render(){try{
 tabs();buildProdTogIfNeeded();header();
 const won=winsOn(),pon=prodsOn();
 let rows=DATA[cur].rows.filter(r=>pon.has(r.product));
 if(sortKey){const num=document.querySelector(`#tbl thead th[data-k="${sortKey}"]`).dataset.num==='true';
  rows=[...rows].sort((x,y)=>{let a=x[sortKey],b=y[sortKey];
   if(a==null&&b==null)return 0;if(a==null)return 1;if(b==null)return -1;
   if(num)return (a-b)*sortDir; return (''+a).localeCompare(''+b)*sortDir;});}
 let body='';
 for(const r of rows){let tr='<tr>';
  allCols().forEach(col=>{if(col.win&&!won.has(col.win))return;
   const cls=(col.c||'')+(col.grp?' grp':'');
   tr+=`<td class="${cls}">${fmt(r[col.k],r.unit,col.z)}</td>`;});
  body+=tr+'</tr>';}
 document.querySelector('#tbl tbody').innerHTML=body;
 document.getElementById('meta').textContent='  '+cur+' : '+rows.length+' instruments';
}catch(e){var tb=document.querySelector('#tbl tbody');if(tb)tb.innerHTML='<tr><td>render error: '+e.message+'</td></tr>';}}
let _ptbuilt=false;function buildProdTogIfNeeded(){if(!_ptbuilt){buildProdTog();_ptbuilt=true;}}
document.querySelectorAll('.wtog').forEach(c=>c.onchange=render);
render();
</script></body></html>"""


def main():
    cfg = A.load_config()
    data = build(cfg)
    html = TEMPLATE.replace("__DATA__", json.dumps(data))
    with open("rates_dashboard.html", "w") as fh:
        fh.write(html)
    n = sum(v["n"] for v in data.values())
    print(f"wrote rates_dashboard.html : {len(data)} ccy tabs, {n} instruments")


if __name__ == "__main__":
    main()
