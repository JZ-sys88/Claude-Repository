"""Build a local HTML contact sheet so you approve the cut list before render."""
import base64
import html
import os

from .model import hhmmss

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --mut:#666; --card:#f4f4f5; --line:#e4e4e7; }
@media (prefers-color-scheme: dark) { :root { --bg:#0c0c0d; --fg:#f4f4f5; --mut:#a1a1aa; --card:#18181b; --line:#27272a; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--mut); margin-bottom:20px; font-size:13px; }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card.off { opacity:.35; }
.card img { width:100%; display:block; aspect-ratio:16/9; object-fit:cover; background:#000; }
.card .noimg { width:100%; aspect-ratio:16/9; display:grid; place-items:center; color:var(--mut); font-size:12px; background:#000; }
.meta { padding:10px 12px; }
.row { display:flex; align-items:center; gap:8px; justify-content:space-between; }
.t { font-weight:600; font-variant-numeric:tabular-nums; }
.badge { font-size:11px; padding:2px 7px; border-radius:99px; border:1px solid var(--line); color:var(--mut); }
.badge.clips { background:#7c3aed22; color:#8b5cf6; border-color:#7c3aed55; }
.badge.chat  { background:#0891b222; color:#22d3ee; border-color:#0891b255; }
.badge.audio { background:#ca8a0422; color:#eab308; border-color:#ca8a0455; }
.badge.speech { background:#16a34a22; color:#4ade80; border-color:#16a34a55; }
.badge.onset { background:#dc262622; color:#f87171; border-color:#dc262655; }
.quote { font-size:12px; color:var(--fg); opacity:.8; margin-top:6px;
  border-left:2px solid var(--line); padding-left:8px; font-style:italic; }
.title { font-size:13px; color:var(--mut); margin-top:6px; word-break:break-word; }
.bar { height:4px; background:var(--line); border-radius:99px; margin-top:8px; overflow:hidden; }
.bar i { display:block; height:100%; background:linear-gradient(90deg,#22d3ee,#8b5cf6); }
.tools { position:sticky; top:0; background:var(--bg); padding:12px 0; margin-bottom:12px;
  border-bottom:1px solid var(--line); display:flex; gap:10px; align-items:center; flex-wrap:wrap; z-index:5; }
button { font:inherit; padding:6px 12px; border-radius:7px; border:1px solid var(--line);
  background:var(--card); color:var(--fg); cursor:pointer; }
code { background:var(--card); padding:3px 7px; border-radius:5px; font-size:13px; word-break:break-all; }
label { display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px; }
"""

JS = """
function sel(){return [...document.querySelectorAll('input[type=checkbox]')]
  .filter(c=>c.checked).map(c=>c.dataset.i);}
function upd(){
  document.querySelectorAll('input[type=checkbox]').forEach(c=>
    c.closest('.card').classList.toggle('off', !c.checked));
  const s=sel();
  document.getElementById('cmd').textContent =
    s.length ? `python highlights.py render candidates.json --pick ${s.join(',')} -o reel.mp4`
             : 'select at least one clip';
  document.getElementById('n').textContent = s.length;
}
function all(v){document.querySelectorAll('input[type=checkbox]').forEach(c=>c.checked=v);upd();}
document.addEventListener('change',e=>{if(e.target.type==='checkbox')upd();});
document.addEventListener('DOMContentLoaded',upd);
"""


def build(cands, out_path, thumbs=None, title="Highlight candidates", stats=None):
    thumbs = thumbs or {}
    cards = []
    for i, c in enumerate(cands):
        src = c.get("sources", [c["source"]])
        badges = "".join(
            f'<span class="badge {html.escape(s)}">{html.escape(s)}</span>' for s in src
        )
        tp = thumbs.get(i)
        if tp and os.path.exists(tp):
            with open(tp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            img = f'<img src="data:image/jpeg;base64,{b64}" alt="">'
        else:
            img = '<div class="noimg">no thumbnail</div>'
        score = c.get("final_score", c.get("score", 0))
        pct = max(2, min(100, int(score / 1.5 * 100)))
        quote = (c.get("detail") or {}).get("quote")
        quote_html = (f'<div class="quote">{html.escape(quote[:180])}</div>'
                      if quote else "")
        cards.append(f"""
<div class="card">
  {img}
  <div class="meta">
    <div class="row">
      <label><input type="checkbox" data-i="{i}" checked><span class="t">{hhmmss(c['start'])}</span></label>
      <span>{badges}</span>
    </div>
    <div class="bar"><i style="width:{pct}%"></i></div>
    <div class="title">#{i} &middot; {score:.2f} &middot; {int(c['duration'])}s</div>
    {quote_html}
  </div>
</div>""")

    statline = ""
    if stats:
        statline = " &middot; ".join(
            f"{html.escape(str(k))}: <b>{html.escape(str(v))}</b>" for k, v in stats.items()
        )

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">{len(cands)} candidates{' &middot; ' + statline if statline else ''}</div>
<div class="tools">
  <button onclick="all(true)">select all</button>
  <button onclick="all(false)">clear</button>
  <span class="sub" style="margin:0"><b id="n">0</b> selected &rarr;</span>
  <code id="cmd"></code>
</div>
<div class="grid">{''.join(cards)}</div>
<script>{JS}</script></body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path
