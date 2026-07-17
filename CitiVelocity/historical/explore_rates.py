"""
explore_rates.py -- generate an interactive HTML explorer of the rates universe.

Reads the discovery inventory (what you're entitled to pull) and builds a single
self-contained HTML file you open in a browser. Drill into
dataset -> currency/index -> product type; see tag counts, the actual dimension
values (tenors / expiries / vol grid), and example tags; tick the products you
want and export a selection (which you hand back to curate the pull).

ZERO API calls -- works entirely off the local inventory file.

    python explore_rates.py
    python explore_rates.py --inventory discovery/rates_tag_inventory.parquet \
                            --out rates_explorer.html
"""
from __future__ import annotations
import argparse, json, os, datetime as dt
import pandas as pd

KNOWN = {"USD", "EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "SEK", "NOK"}


def _split(token):
    u = str(token).upper()
    if u in KNOWN:
        return u, None
    if "_" in u and u.split("_", 1)[0] in KNOWN:
        h, r = u.split("_", 1)
        return h, r
    return None, None


def build_tree(df, max_field=9, cap=80, examples=3):
    out_ds = []
    for ds, dgrp in df.groupby("dataset"):
        groups = []
        for g2, ggrp in dgrp.groupby(dgrp["f2"].fillna("?")):
            prods = []
            for p, pgrp in ggrp.groupby(ggrp["f3"].fillna("(none)")):
                fields = []
                maxf = int(pgrp["n_fields"].max())
                for pos in range(4, min(maxf, max_field)):
                    col = f"f{pos}"
                    if col not in pgrp:
                        continue
                    vals = [v for v in pgrp[col].dropna().unique()]
                    if not vals:
                        continue
                    vals = sorted(map(str, vals), key=lambda x: (len(x), x))
                    fields.append({"pos": pos, "n": len(vals), "values": vals[:cap]})
                prods.append({"key": str(p), "n": int(len(pgrp)),
                              "fields": fields,
                              "examples": pgrp["tag"].head(examples).tolist()})
            ccy, idx = _split(g2)
            groups.append({"key": str(g2), "ccy": ccy, "index": idx, "n": int(len(ggrp)),
                           "products": sorted(prods, key=lambda x: -x["n"])})
        out_ds.append({"name": ds, "n": int(len(dgrp)),
                       "groups": sorted(groups, key=lambda x: -x["n"])})
    return {"total": int(len(df)),
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "datasets": sorted(out_ds, key=lambda x: -x["n"])}


TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>Citi Velocity Rates Explorer</title>
<style>
:root{--bg:#0f1419;--card:#1a212b;--line:#2b3440;--fg:#dfe6ee;--mut:#8aa;--acc:#4ea1ff;--good:#37c87a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:#0b0f14;border-bottom:1px solid var(--line);
padding:12px 18px;z-index:5}
h1{font-size:16px;margin:0 0 8px}.sub{color:var(--mut);font-size:12px}
.bar{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
input[type=text]{background:var(--card);border:1px solid var(--line);color:var(--fg);
padding:7px 10px;border-radius:6px;width:320px}
button{background:var(--acc);color:#04121f;border:0;padding:7px 12px;border-radius:6px;
font-weight:600;cursor:pointer}button.ghost{background:var(--card);color:var(--fg);border:1px solid var(--line)}
.wrap{padding:14px 18px;max-width:1150px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0}
details>summary{cursor:pointer;padding:9px 12px;list-style:none;display:flex;
justify-content:space-between;gap:12px;align-items:center}
summary::-webkit-details-marker{display:none}
.ds>summary{font-weight:700;font-size:15px}
.grp{margin:6px 10px}.grp>summary{color:var(--acc)}
.cnt{color:var(--mut);font-variant-numeric:tabular-nums;font-size:12px}
.prod{margin:6px 12px;border-top:1px solid var(--line);padding:8px 2px}
.prow{display:flex;gap:10px;align-items:center}
.prow .k{font-weight:600;min-width:130px}
.tag{color:var(--mut);font-family:ui-monospace,Menlo,monospace;font-size:11px}
.fields{margin:6px 0 2px 24px}
.f{margin:3px 0}.f b{color:var(--good);font-weight:600}
.chip{display:inline-block;background:#10212f;border:1px solid var(--line);border-radius:5px;
padding:1px 6px;margin:2px 3px 0 0;font-family:ui-monospace,monospace;font-size:11px}
.ex{margin:5px 0 2px 24px;color:var(--mut);font-family:ui-monospace,monospace;font-size:11px}
.badge{background:#10212f;border:1px solid var(--line);border-radius:20px;padding:3px 10px;color:var(--good)}
#exp{width:100%;height:150px;background:#0b0f14;color:var(--fg);border:1px solid var(--line);
border-radius:6px;font-family:ui-monospace,monospace;font-size:12px;margin-top:8px;display:none;padding:8px}
.hide{display:none!important}
label.sel{display:flex;align-items:center;gap:6px;cursor:pointer}
</style></head><body>
<header>
  <h1>Citi Velocity Rates Explorer</h1>
  <div class="sub">__TOTAL__ tags entitled &middot; generated __GEN__ &middot; offline, no API calls</div>
  <div class="bar">
    <input id="q" type="text" placeholder="filter: e.g. SOFR, PAR, FWD, VOL, 10Y, EUR_EUROSTR">
    <span class="badge" id="selcnt">0 products / 0 tags selected</span>
    <button id="expbtn">Export selection</button>
    <button class="ghost" id="clrbtn">Clear</button>
    <button class="ghost" id="exall">Expand all</button>
    <button class="ghost" id="colall">Collapse all</button>
  </div>
  <textarea id="exp" readonly></textarea>
</header>
<div class="wrap" id="root"></div>
<script>
const DATA = __DATA__;
const sel = new Map();
function fmt(n){return n.toLocaleString()}
function chips(vals,total){let s=vals.map(v=>'<span class="chip">'+v+'</span>').join('');
 if(total>vals.length)s+=' <span class="cnt">(+'+(total-vals.length)+' more)</span>';return s}
function render(){
 const root=document.getElementById('root');root.innerHTML='';
 for(const ds of DATA.datasets){
   const d=document.createElement('details');d.className='ds';d.open=false;
   d.innerHTML='<summary><span>'+ds.name+'</span><span class="cnt">'+fmt(ds.n)+' tags</span></summary>';
   for(const g of ds.groups){
     const gd=document.createElement('details');gd.className='grp';
     const idx=g.index?(' &middot; '+g.index):'';
     gd.innerHTML='<summary><span>'+g.key+(g.ccy?(' ['+g.ccy+idx+']'):'')+'</span><span class="cnt">'+fmt(g.n)+'</span></summary>';
     for(const p of g.products){
       const id=ds.name+'||'+g.key+'||'+p.key;
       const pd=document.createElement('div');pd.className='prod';pd.dataset.id=id;
       let fl='';for(const f of p.fields){fl+='<div class="f"><b>f'+f.pos+'</b> ('+f.n+'): '+chips(f.values,f.n)+'</div>'}
       let ex=p.examples.map(t=>'<div>'+t+'</div>').join('');
       pd.innerHTML='<div class="prow"><label class="sel"><input type="checkbox" data-id="'+id+'" data-n="'+p.n+'">'+
         '<span class="k">'+p.key+'</span></label><span class="cnt">'+fmt(p.n)+' tags</span></div>'+
         '<div class="fields">'+fl+'</div><div class="ex">'+ex+'</div>';
       gd.appendChild(pd);
     }
     d.appendChild(gd);
   }
   root.appendChild(d);
 }
 bindChecks();applyFilter();
}
function bindChecks(){document.querySelectorAll('input[type=checkbox]').forEach(cb=>{
  cb.checked=sel.has(cb.dataset.id);
  cb.onchange=()=>{if(cb.checked)sel.set(cb.dataset.id,+cb.dataset.n);else sel.delete(cb.dataset.id);upd();};});}
function upd(){let tags=0;sel.forEach(v=>tags+=v);
  document.getElementById('selcnt').textContent=sel.size+' products / '+fmt(tags)+' tags selected';}
function applyFilter(){const q=document.getElementById('q').value.trim().toUpperCase();
  document.querySelectorAll('.ds').forEach(ds=>{let dsHit=false;
   ds.querySelectorAll('.grp').forEach(g=>{let gHit=false;
    g.querySelectorAll('.prod').forEach(p=>{const hit=!q||p.dataset.id.toUpperCase().includes(q)||p.textContent.toUpperCase().includes(q);
      p.classList.toggle('hide',!hit);if(hit)gHit=true;});
    g.classList.toggle('hide',!gHit);if(gHit){dsHit=true;if(q)g.open=true;}});
   ds.classList.toggle('hide',!dsHit);if(dsHit&&q)ds.open=true;});}
document.getElementById('q').addEventListener('input',applyFilter);
document.getElementById('expbtn').onclick=()=>{const t=document.getElementById('exp');
  const rules=[...sel.keys()].map(k=>{const[dataset,group,product]=k.split('||');
    return{dataset,group,product,n:sel.get(k)};});
  let tot=0;sel.forEach(v=>tot+=v);
  t.value=JSON.stringify({selected_tags:tot,rules},null,2);t.style.display='block';t.select();};
document.getElementById('clrbtn').onclick=()=>{sel.clear();upd();document.querySelectorAll('input[type=checkbox]').forEach(c=>c.checked=false);document.getElementById('exp').style.display='none';};
document.getElementById('exall').onclick=()=>document.querySelectorAll('details').forEach(d=>d.open=true);
document.getElementById('colall').onclick=()=>document.querySelectorAll('details').forEach(d=>d.open=false);
render();
</script></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate the interactive rates explorer HTML.")
    ap.add_argument("--inventory", default="discovery/rates_tag_inventory.parquet")
    ap.add_argument("--out", default="rates_explorer.html")
    args = ap.parse_args(argv)
    inv = (pd.read_parquet(args.inventory) if args.inventory.endswith(".parquet")
           else pd.read_csv(args.inventory))
    tree = build_tree(inv)
    html = (TEMPLATE.replace("__DATA__", json.dumps(tree))
                    .replace("__TOTAL__", f"{tree['total']:,}")
                    .replace("__GEN__", tree["generated"]))
    with open(args.out, "w") as fh:
        fh.write(html)
    print(f"Wrote {args.out}  ({os.path.getsize(args.out)/1024:.0f} KB) "
          f"from {tree['total']:,} tags across {len(tree['datasets'])} datasets.")


if __name__ == "__main__":
    raise SystemExit(main())
