"""
apply_low_prob_patch.py
Run from the folder that holds index.html:   python apply_low_prob_patch.py
Adds the Low-probability days card and the "Hide low-probability days" heatmap toggle.
Writes index.html.bak first, keeps the file's line endings, and refuses to run twice.
"""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "index.html"
raw = open(PATH, encoding="utf-8", newline="").read()
crlf = "\r\n" in raw
t = raw.replace("\r\n", "\n")

if "lpCard" in t:
    sys.exit("index.html already has the low-probability card - nothing to do.")

CSS = r"""
.lp-toggle{ display:flex; align-items:center; gap:8px; cursor:pointer; font-size:11.5px; color:var(--ink-dim); font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace; user-select:none; }
.lp-toggle input{ position:absolute; opacity:0; pointer-events:none; }
.lp-toggle .sw{ width:30px; height:16px; border-radius:10px; background:var(--line); position:relative; transition:background-color .15s ease; flex:0 0 30px; }
.lp-toggle .sw::after{ content:""; position:absolute; top:2px; left:2px; width:12px; height:12px; border-radius:50%; background:var(--ink); transition:transform .15s ease; }
.lp-toggle input:checked + .sw{ background:var(--gold); }
.lp-toggle input:checked + .sw::after{ transform:translateX(14px); }
.lp-toggle input:focus-visible + .sw{ outline:2px solid var(--gold); outline-offset:2px; }
.lp-card{ background:var(--panel-2); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin:18px 0 6px; }
.lp-card h4{ font-size:11px; text-transform:uppercase; letter-spacing:1.2px; color:var(--ink-dim); margin:0 0 4px; font-weight:600; }
.lp-sub{ font-size:11px; color:var(--ink-dim); margin-bottom:14px; line-height:1.5; }
.lp-grid{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-bottom:16px; }
.lp-stat .k-label{ font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--ink-dim); margin-bottom:5px; }
.lp-stat .k-value{ font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace; font-size:20px; font-weight:700; }
.lp-stat .k-note{ font-size:11px; color:var(--ink-dim); margin-top:3px; }
.lp-cmp{ display:grid; grid-template-columns:110px minmax(0,1fr) 150px; gap:10px; align-items:center; padding:5px 0; font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace; font-size:11.5px; }
.lp-cmp .bar{ height:10px; border-radius:5px; background:rgba(127,127,127,.18); overflow:hidden; }
.lp-cmp .bar i{ display:block; height:100%; }
.lp-cmp .ct{ text-align:right; color:var(--ink-dim); }
.lp-delta{ font-size:12px; margin:8px 0 14px; font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace; }
.lp-cols{ display:grid; grid-template-columns:minmax(0,1.2fr) minmax(0,1fr); gap:24px; }
@media (max-width:900px){ .lp-cols{ grid-template-columns:1fr; } }
.lp-list{ display:flex; flex-direction:column; gap:6px; font-size:12px; }
.lp-list .row{ display:flex; align-items:center; gap:8px; justify-content:space-between; }
.lp-list .row .nm{ display:flex; align-items:center; gap:8px; min-width:0; }
.lp-list .row .nm span.t{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lp-list .dot{ width:10px; height:10px; border-radius:3px; flex:0 0 10px; }
.lp-list .n{ font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace; color:var(--ink-dim); white-space:nowrap; }
.lp-empty{ font-size:12px; color:var(--ink-dim); }
"""

HELPERS = r"""let hmHideLow = false;
try { hmHideLow = localStorage.getItem('mmtrades-hide-low') === '1'; } catch(e){}
const LP_TAGS = ['FOMC','CPI','NFP','PPI'];
function lpTagsOf(v){
  const s = Array.isArray(v) ? v.join(' ') : String(v || '');
  return LP_TAGS.filter(t => new RegExp('\\b' + t + '\\b', 'i').test(s));
}
function prevWeekday(d){
  const x = parseD(d);
  do { x.setDate(x.getDate() - 1); } while (x.getDay() === 0 || x.getDay() === 6);
  return x.getFullYear() + '-' + String(x.getMonth()+1).padStart(2,'0') + '-' + String(x.getDate()).padStart(2,'0');
}
// date -> Set of reasons. FOMC/CPI/NFP flag the day and the weekday before; PPI flags only its own day.
function lowMap(data){
  const m = new Map();
  const add = (d, r) => { if (!m.has(d)) m.set(d, new Set()); m.get(d).add(r); };
  (data.events || []).forEach(([d, v]) => {
    lpTagsOf(v).forEach(tag => { add(d, tag); if (tag !== 'PPI') add(prevWeekday(d), tag + ' eve'); });
  });
  return m;
}
"""

RENDER_LOW = r"""function renderLow(M){
  const el = document.getElementById('lpCard');
  const low = lowMap(hmData);
  if (!low.size){
    el.innerHTML = '<h4>Low-probability days</h4><div class="lp-empty">No FOMC / CPI / NFP / PPI tags found yet. The sync needs to write an <code>events</code> list into am-frameworks.json.</div>';
    return;
  }
  const fws = M.fws, dayMap = new Map(hmData.days);
  const real = d => (dayMap.get(d) || []).filter(i => !fws[i].neutral);
  const keyed = new Set(); hmData.days.forEach(([d]) => { if (real(d).length) keyed.add(d.slice(0,7)); });
  const all = [...new Set([...hmData.logged, ...hmData.days.map(x => x[0])])]
    .filter(d => { const w = parseD(d).getDay(); return w >= 1 && w <= 5 && keyed.has(d.slice(0,7)); });
  const lowD = all.filter(d => low.has(d)), highD = all.filter(d => !low.has(d));
  const lowF = lowD.filter(d => real(d).length), highF = highD.filter(d => real(d).length);
  const lr = lowD.length ? lowF.length / lowD.length : 0;
  const hr = highD.length ? highF.length / highD.length : 0;
  const delta = Math.round((hr - lr) * 100);
  const per = {};
  lowD.forEach(d => low.get(d).forEach(r => { per[r] = (per[r] || 0) + 1; }));
  const reasons = [];
  LP_TAGS.forEach(tag => { if (per[tag]) reasons.push([tag, per[tag]]); if (per[tag + ' eve']) reasons.push([tag + ' day before', per[tag + ' eve']]); });
  const fwc = new Array(fws.length).fill(0);
  lowF.forEach(d => real(d).forEach(i => { fwc[i]++; }));
  const fwRows = fws.filter(f => fwc[f.idx] > 0).sort((a,b) => fwc[b.idx] - fwc[a.idx])
    .map(f => `<div class="row"><div class="nm"><span class="dot" style="background:${f.color}"></span><span class="t">${esc(f.name)}</span></div><div class="n">\u00d7${fwc[f.idx]}</div></div>`).join('')
    || '<div class="lp-empty">No framework was logged on a low-probability day.</div>';
  const cmp = (label, n, tot, rate, color) => `<div class="lp-cmp"><div>${label}</div>
    <div class="bar"><i style="width:${(rate*100).toFixed(0)}%;background:${color}"></i></div>
    <div class="ct">${n}/${tot} \u00b7 ${pct(rate)}</div></div>`;
  el.innerHTML = `<h4>Low-probability days</h4>
    <div class="lp-sub">FOMC, CPI and NFP days plus the weekday before each, and PPI days. Counted over keyed months only, so it matches the heatmap. Not affected by the toggle.</div>
    <div class="lp-grid">
      <div class="lp-stat"><div class="k-label">Low-probability days</div><div class="k-value">${lowD.length}</div><div class="k-note">of ${all.length} logged days \u00b7 ${all.length ? pct(lowD.length/all.length) : '0%'}</div></div>
      <div class="lp-stat"><div class="k-label">With a framework</div><div class="k-value">${lowF.length}</div><div class="k-note">${lowD.length - lowF.length} had none</div></div>
      <div class="lp-stat"><div class="k-label">Normal days</div><div class="k-value">${highD.length}</div><div class="k-note">no event tag, not the day before FOMC / CPI / NFP</div></div>
    </div>
    ${cmp('Normal days', highF.length, highD.length, hr, 'rgb(var(--hm-rgb))')}
    ${cmp('Low-prob days', lowF.length, lowD.length, lr, 'var(--stale-c)')}
    <div class="lp-delta ${delta > 0 ? 'pos' : delta < 0 ? 'neg' : 'flat'}">Framework rate on normal days is ${delta >= 0 ? '+' : ''}${delta} percentage points vs low-probability days</div>
    <div class="lp-cols">
      <div><h4>Frameworks on low-probability days</h4><div class="lp-list">${fwRows}</div></div>
      <div><h4>Days by reason <span style="text-transform:none;letter-spacing:0">(a day can count twice)</span></h4><div class="lp-list">${reasons.map(([r,n]) => `<div class="row"><div class="nm"><span class="t">${r}</span></div><div class="n">${n} day${n===1?'':'s'}</div></div>`).join('')}</div></div>
    </div>`;
}

"""

WIRE = r"""const hideBox = document.getElementById('hmHideLow');
hideBox.checked = hmHideLow;
hideBox.addEventListener('change', () => {
  hmHideLow = hideBox.checked;
  try { localStorage.setItem('mmtrades-hide-low', hmHideLow ? '1' : '0'); } catch(e){}
  render();
});
"""

TOGGLE_HTML = '<label class="lp-toggle"><input type="checkbox" id="hmHideLow"><span class="sw"></span>Hide low-probability days</label>\n        '
OLD_ALLDATES = "const allDates = [...set].sort().filter(d => { const w = parseD(d).getDay(); return w >= 1 && w <= 5; });"
NEW_ALLDATES = ("const lowSet = hmHideLow ? lowMap(data) : null;\n"
                "  const allDates = [...set].sort().filter(d => { const w = parseD(d).getDay(); return w >= 1 && w <= 5 && !(lowSet && lowSet.has(d)); });")

edits = [
    ("</style>", CSS + "</style>"),
    ('<div class="seg-control" id="hmYearControl" style="display:none"></div>',
     TOGGLE_HTML + '<div class="seg-control" id="hmYearControl" style="display:none"></div>'),
    ('<div class="hm-kpis" id="hmKpis"></div>',
     '<div class="lp-card" id="lpCard"></div>\n    <div class="hm-kpis" id="hmKpis"></div>'),
    ("let hmYear = null;", HELPERS + "let hmYear = null;"),
    (OLD_ALLDATES, NEW_ALLDATES),
    ("function render(){", RENDER_LOW + "function render(){"),
    ("\n}\n\nfunction loadHeatmap(){",
     "\n  if (hmHideLow) meta.textContent += ' \\u00b7 low-probability days hidden';\n  renderLow(M);\n}\n\nfunction loadHeatmap(){"),
    ("render();        // draw instantly from the embedded snapshot", WIRE + "render();        // draw instantly from the embedded snapshot"),
]

for old, new in edits:
    n = t.count(old)
    if n != 1:
        sys.exit(f"Could not apply: expected 1 match, found {n} for:\n  {old[:90]!r}\nindex.html was NOT changed.")
    t = t.replace(old, new, 1)

open(PATH + ".bak", "w", encoding="utf-8", newline="").write(raw)
open(PATH, "w", encoding="utf-8", newline="").write(t.replace("\n", "\r\n") if crlf else t)
print("Done. index.html updated (backup saved as index.html.bak).")
