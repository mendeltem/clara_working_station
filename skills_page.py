"""Builds skills.html: every skill the agents on the workstation can load, as clickable charts.

    python3 skills_page.py --clara DIR --pool DIR --shared DIR --setup DIR

    --clara   folder Clara's loader reads (links to skill folders)
    --pool    Clara's own skill folder (a skill there is loaded only when it is linked into --clara)
    --shared  folder for skills every agent may read
    --setup   folder the setup sessions read

Only the name, counts and the hand-checked purpose line from skills_purpose.json reach the page.
The skill texts themselves are never copied: they may name tools, folders or rules that stay on the machine.
Standard library only."""
import argparse, datetime, html, json, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
LIMIT_LINES, LIMIT_CHARS = 500, 1024          # Agent Skills format: body recommended under 500 lines, description at most 1,024 characters
BRINGS = ("scripts", "references", "assets", "templates", "evals")


def description(front: str) -> str:
    """The description value of the front matter, block scalars and continuation lines joined."""
    out, on = [], False
    for ln in front.splitlines():
        m = KEY_RE.match(ln)
        if m:
            on = m.group(1) == "description"
            if on:
                out.append(re.sub(r"^[>|][+-]?\d*\s*$", "", m.group(2).strip()))
        elif on:
            out.append(ln.strip())
    text = " ".join(" ".join(out).split())
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'" else text


def read_skill(folder: Path) -> dict | None:
    f = folder / "SKILL.md"
    if not f.is_file():
        return None
    text = f.read_text(encoding="utf-8", errors="replace")
    m = FRONT_RE.match(text)
    body = text[m.end():] if m else text
    real = f.resolve().parent
    return {"real": real, "lines": len(body.splitlines()), "chars": len(description(m.group(1))) if m else 0,
            "files": sum(1 for p in real.rglob("*") if p.is_file()) - 1,
            "has": [b for b in BRINGS if (real / b).is_dir()]}


def collect(clara: Path, pool: Path, shared: Path, setup: Path) -> list[dict]:
    purpose = json.loads((HERE / "skills_purpose.json").read_text(encoding="utf-8"))
    names = lambda d: {p.name for p in d.iterdir() if (p / "SKILL.md").is_file()} if d.is_dir() else set()
    sees_clara, sees_setup = names(clara), names(setup)
    skills = {}
    for home, d in (("clara", pool), ("shared", shared), ("setup", setup), ("clara", clara)):
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            s = read_skill(p)
            if s is None or p.name in skills:
                continue
            real = s.pop("real")
            if shared.resolve() in real.parents:
                home_of = "shared"
            elif pool.resolve() in real.parents:
                home_of = "clara"
            else:
                home_of = home
            if p.name not in purpose:
                print("no purpose line yet (add it to skills_purpose.json):", p.name)
            skills[p.name] = {"n": p.name, "p": purpose.get(p.name, ""), "home": home_of,
                              "clara": p.name in sees_clara, "setup": p.name in sees_setup, **s}
    return sorted(skills.values(), key=lambda s: s["n"])


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Clara Working Station: skills</title>
<meta name="description" content="Every skill Clara and the setup sessions can load, as clickable charts: who loads which, and how large each one is.">
<link rel="stylesheet" href="site.css">
<style>
 :root{--mark:#1f9a8e;--pick:#c67c12;--off:#34474f;--track:#14343a;--grid:#1a2a32}
 .tiles4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 6px}
 .st{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:12px 14px}
 .st .v{font-size:24px;font-weight:800;color:var(--ink);line-height:1.2}
 .st .l{color:var(--muted);font-size:11.5px}
 .fbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:22px 0 10px;font-size:12.5px;color:var(--muted);min-height:30px}
 .fbar b{color:var(--ink)}
 button.b{font:inherit;font-size:12px;color:var(--accent2);background:none;border:1px solid var(--line);border-radius:7px;padding:3px 10px;cursor:pointer}
 button.b:hover{border-color:var(--accent2)}
 .two{display:grid;grid-template-columns:1fr 1fr;gap:11px}
 .card{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:14px 14px 8px;margin-bottom:11px}
 .card h3{font-size:13px;font-weight:700;color:var(--ink);margin-bottom:2px}
 .card .h{color:var(--muted);font-size:11.5px;margin-bottom:8px}
 .card svg{display:block;width:100%;height:auto}
 .card svg text{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
 .bar{cursor:pointer}.bar:hover .m,.bar:focus-visible .m{filter:brightness(1.25)}
 .bar:focus-visible{outline:none}.bar:focus-visible .hit{stroke:var(--accent);stroke-width:1.5}
 .plot{cursor:pointer}
 #tip{position:fixed;z-index:9;pointer-events:none;background:#06090b;border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:12px;
      color:var(--muted);max-width:280px;display:none}
 #tip b{display:block;color:var(--ink);font-size:13px}
 #detail h3{font-size:16px;color:var(--accent2);font-weight:800;margin-bottom:4px}
 #detail p{color:var(--muted);font-size:12.5px;margin-bottom:10px}
 #detail dl{display:grid;grid-template-columns:120px 1fr;gap:7px 12px;font-size:12.5px;margin-bottom:8px}
 #detail dt{color:var(--muted)}#detail dd{color:var(--ink)}
 .meter{display:inline-block;vertical-align:middle;width:150px;height:8px;border-radius:4px;background:var(--track);margin-right:9px;overflow:hidden}
 .meter i{display:block;height:100%;background:var(--mark);border-radius:4px}
 table.sk td:first-child{white-space:nowrap}
 table.sk td.num{text-align:right;color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap}
 table.sk th.num{text-align:right}
 table.sk button{font:inherit;font-weight:700;color:var(--accent2);background:none;border:0;padding:0;cursor:pointer;text-align:left}
 table.sk button:hover{text-decoration:underline}
 table.sk tr.sel td{background:#132129}
 table.sk tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--pick)}
 table.sk tr.sel button{padding-left:8px}
 @media (max-width:640px){.tiles4{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}#detail dl{grid-template-columns:1fr}}
</style>
</head><body>
__NAV__<h1>Skills</h1>
<div class="sub">Clara Working Station</div>
<p class="s">A skill is a folder with one instruction file. The agent always has the <b>name and description</b> of every
skill in its prompt and reads the <b>body</b> only when a task calls for it, so a skill costs little until it is used.
The <a class="gh" href="https://agentskills.io/specification" target="_blank" rel="noopener">Agent Skills format</a>
allows a description of at most 1,024 characters and recommends a body under 500 lines.</p>
<p class="s">Click a bar to filter, click a point or a name to see one skill. Counted on __DATE__ on the workstation.</p>
<p class="s"><a class="gh" href="skills-overview.html">All skills, one by one →</a> what each skill does, when it loads, and what a review of its text found.</p>

<div class="tiles4" id="tiles"></div>

<div class="fbar" id="fbar" aria-live="polite"></div>
<div class="two">
 <div class="card"><h3>Who loads it</h3><div class="h">Number of skills. Click a bar to filter.</div><div id="who"></div></div>
 <div class="card"><h3>What it brings along</h3><div class="h">Skills with files beside the instructions.</div><div id="brings"></div></div>
</div>
<div class="card"><h3>Size against the limits of the format</h3>
 <div class="h">One point per skill: length of the description (always in the prompt) against length of the body (read on demand). Click a point.</div>
 <div class="tw"><div id="size" style="min-width:600px"></div></div></div>

<div class="card" id="detail" aria-live="polite"></div>

<h2>All skills</h2>
<div class="tw"><table class="sk" id="tab"></table></div>

<h2>Sources</h2>
<ul class="s">
<li><a class="gh" href="https://agentskills.io/specification" target="_blank" rel="noopener">Agent Skills specification</a> (agentskills.io):
the folder layout, the two fields name and description, the limit of 1,024 characters and the recommendation of 500 lines.</li>
<li>The counts come from the skill folders on the workstation, read by
<a class="gh" href="https://github.com/mendeltem/clara_working_station/blob/main/skills_page.py" target="_blank" rel="noopener">skills_page.py</a>.
Only the name, the counts and a hand-checked purpose line are published; the skill texts stay on the machine.</li>
</ul>
<div class="pn"><a href="system.html">← The system</a><a href="setup.html">Rebuild it →</a></div>
<div id="tip" role="tooltip"></div>
<script>
const D = __DATA__;
const LIM = {lines: __LIMIT_LINES__, chars: __LIMIT_CHARS__};
const NS = "http://www.w3.org/2000/svg";
const C = {mark: "#1f9a8e", pick: "#c67c12", off: "#34474f", ink: "#e8f1f3", muted: "#7d99a2", grid: "#1a2a32", axis: "#22363f", surface: "#0f1a20"};
const WHO = [
  {key: "clara", label: "Clara", name: "loaded by Clara", test: s => s.clara},
  {key: "setup", label: "Setup sessions", name: "loaded by the setup sessions", test: s => s.setup},
  {key: "both", label: "Both", name: "loaded by both", test: s => s.clara && s.setup},
  {key: "nobody", label: "Nobody yet", name: "in Clara's folder, not linked yet", test: s => !s.clara && !s.setup}];
const BRINGS = [
  {key: "none", label: "Instructions only", name: "with instructions only", test: s => s.files === 0},
  {key: "scripts", label: "Scripts", name: "with scripts", test: s => s.has.includes("scripts")},
  {key: "references", label: "References", name: "with reference texts", test: s => s.has.includes("references")},
  {key: "assets", label: "Assets or templates", name: "with assets or templates", test: s => s.has.includes("assets") || s.has.includes("templates")},
  {key: "evals", label: "Test cases", name: "with test cases", test: s => s.has.includes("evals")}];
const HOME = {clara: "Clara's skill folder", shared: "shared folder for all agents", setup: "the setup sessions' own folder"};
let filter = null, picked = null;

const el = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const fmt = n => n.toLocaleString("en-US");
const who = s => s.clara && s.setup ? "Clara and the setup sessions" : s.clara ? "Clara" : s.setup ? "the setup sessions" : "nobody yet";
const shown = s => !filter || filter.test(s);
const tip = document.getElementById("tip");
function showTip(x, y, strong, rest) {
  tip.textContent = ""; tip.appendChild(h("b", "", strong)); tip.appendChild(document.createTextNode(rest));
  tip.style.display = "block";
  const w = tip.offsetWidth, hh = tip.offsetHeight;
  tip.style.left = Math.max(6, Math.min(x + 14, innerWidth - w - 6)) + "px";
  tip.style.top = (y + 16 + hh > innerHeight ? y - hh - 10 : y + 16) + "px";
}
const hideTip = () => { tip.style.display = "none"; };

function setFilter(f) { filter = filter && f && filter.key === f.key ? null : f; draw(); }
function pick(name, scroll) {
  picked = picked === name ? null : name; draw();
  try { history.replaceState(null, "", picked ? "#skill=" + encodeURIComponent(picked) : location.pathname); } catch (e) {}
  if (picked && scroll) document.getElementById("detail").scrollIntoView({block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
}

function tiles() {
  const t = document.getElementById("tiles"); t.textContent = "";
  const over = D.filter(s => s.lines > LIM.lines || s.chars > LIM.chars).length;
  [[D.length, "skills in all"], [D.filter(s => s.clara).length, "loaded by Clara"], [D.filter(s => s.setup).length, "loaded by the setup sessions"], [over, "above a limit of the format"]]
    .forEach(([v, l]) => { const d = h("div", "st"); d.appendChild(h("div", "v", fmt(v))); d.appendChild(h("div", "l", l)); t.appendChild(d); });
}

function bars(id, rows) {
  const W = 340, RH = 30, LW = 150, MAXW = W - LW - 34, top = 4, max = Math.max(...rows.map(r => D.filter(r.test).length));
  const svg = el("svg", {viewBox: `0 0 ${W} ${top + rows.length * RH + 4}`, role: "group"});
  rows.forEach((r, i) => {
    const n = D.filter(r.test).length, y = top + i * RH, w = Math.max(n ? 6 : 0, MAXW * n / max), on = filter && filter.key === r.key;
    const g = el("g", {class: "bar", tabindex: "0", role: "button", "aria-pressed": on ? "true" : "false", "aria-label": `${r.label}: ${n} skills. Filter the list.`});
    g.appendChild(el("rect", {class: "hit", x: 1, y: y + 1, width: W - 2, height: RH - 2, rx: 6, fill: "transparent"}));
    g.appendChild(el("text", {x: LW - 10, y: y + RH / 2 + 4, "text-anchor": "end", "font-size": 12, fill: on ? C.ink : C.muted, "font-weight": on ? 700 : 400}, r.label));
    if (n) g.appendChild(el("path", {class: "m", fill: on ? C.pick : C.mark,
      d: `M${LW},${y + 7}h${w - 4}a4,4 0 0 1 4,4v8a4,4 0 0 1 -4,4h${-(w - 4)}z`}));
    g.appendChild(el("text", {x: LW + w + 7, y: y + RH / 2 + 4, "font-size": 12, fill: C.ink, "font-weight": 700}, fmt(n)));
    const go = () => setFilter(r);
    g.addEventListener("click", go);
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); const again = document.querySelectorAll("#" + id + " .bar")[i]; if (again) again.focus(); } });
    g.addEventListener("pointermove", e => showTip(e.clientX, e.clientY, `${fmt(n)} of ${D.length} skills`, r.name + (on ? " (click to show all)" : " (click to filter)")));
    g.addEventListener("pointerleave", hideTip);
    g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); showTip(b.left + LW, b.top, `${fmt(n)} of ${D.length} skills`, r.name); });
    g.addEventListener("blur", hideTip);
    svg.appendChild(g);
  });
  const box = document.getElementById(id); box.textContent = ""; box.appendChild(svg);
}

function size() {
  const W = 700, H = 380, L = 50, R = 18, T = 16, B = 44, XM = 1100, YM = 540;
  const X = v => L + (W - L - R) * v / XM, Y = v => H - B - (H - T - B) * v / YM;
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", class: "plot",
    "aria-label": "Scatter plot of the skills: description length against body length. The same numbers are in the table below."});
  [0, 250, 500, 750, 1000].forEach(v => {
    svg.appendChild(el("line", {x1: X(v), x2: X(v), y1: T, y2: H - B, stroke: C.grid, "stroke-width": 1}));
    svg.appendChild(el("text", {x: X(v), y: H - B + 16, "text-anchor": "middle", "font-size": 11, fill: C.muted}, fmt(v)));
  });
  [0, 100, 200, 300, 400, 500].forEach(v => {
    svg.appendChild(el("line", {x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: v ? C.grid : C.axis, "stroke-width": 1}));
    svg.appendChild(el("text", {x: L - 8, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: C.muted}, fmt(v)));
  });
  svg.appendChild(el("text", {x: (L + W - R) / 2, y: H - 8, "text-anchor": "middle", "font-size": 11.5, fill: C.muted}, "description, characters"));
  svg.appendChild(el("text", {x: 12, y: (T + H - B) / 2, "text-anchor": "middle", "font-size": 11.5, fill: C.muted, transform: `rotate(-90 12 ${(T + H - B) / 2})`}, "body, lines"));
  svg.appendChild(el("line", {x1: L, x2: W - R, y1: Y(LIM.lines), y2: Y(LIM.lines), stroke: C.muted, "stroke-width": 1}));
  svg.appendChild(el("text", {x: L + 8, y: Y(LIM.lines) - 6, "font-size": 11, fill: C.muted}, "recommended limit: " + fmt(LIM.lines) + " lines"));
  svg.appendChild(el("line", {x1: X(LIM.chars), x2: X(LIM.chars), y1: T, y2: H - B, stroke: C.muted, "stroke-width": 1}));
  svg.appendChild(el("text", {x: X(LIM.chars) - 6, y: Y(440), "text-anchor": "end", "font-size": 11, fill: C.muted}, "limit: " + fmt(LIM.chars) + " characters"));
  const order = D.slice().sort((a, b) => (shown(a) - shown(b)) || ((a.n === picked) - (b.n === picked)));
  order.forEach(s => {
    const p = s.n === picked;
    svg.appendChild(el("circle", {cx: X(s.chars), cy: Y(s.lines), r: p ? 7 : 5, fill: p ? C.pick : shown(s) ? C.mark : C.off, stroke: C.surface, "stroke-width": 2}));
  });
  const label = (s, strong) => {
    const right = X(s.chars) < W - 220;
    svg.appendChild(el("text", {x: X(s.chars) + (right ? 11 : -11), y: Y(s.lines) + 4, "text-anchor": right ? "start" : "end", "font-size": 11.5,
      fill: strong ? C.ink : C.muted, "font-weight": strong ? 700 : 400, stroke: C.surface, "stroke-width": 3, "paint-order": "stroke"}, s.n));
  };
  const pool = D.filter(shown), far = [];
  if (pool.length) { far.push(pool.reduce((a, b) => b.lines > a.lines ? b : a)); const c = pool.reduce((a, b) => b.chars > a.chars ? b : a); if (c !== far[0]) far.push(c); }
  far.filter(s => s.n !== picked).forEach(s => label(s, false));
  const ps = D.find(s => s.n === picked); if (ps) label(ps, true);
  const near = e => {
    const b = svg.getBoundingClientRect(), k = W / b.width, mx = (e.clientX - b.left) * k, my = (e.clientY - b.top) * k;
    let best = null, bd = 24 * 24;
    D.forEach(s => { const d = (X(s.chars) - mx) ** 2 + (Y(s.lines) - my) ** 2 - (shown(s) ? 60 : 0); if (d < bd) { bd = d; best = s; } });
    return best;
  };
  svg.addEventListener("pointermove", e => { const s = near(e); if (s) showTip(e.clientX, e.clientY, `${fmt(s.lines)} lines · ${fmt(s.chars)} characters`, s.n + ", loaded by " + who(s)); else hideTip(); svg.style.cursor = s ? "pointer" : "default"; });
  svg.addEventListener("pointerleave", hideTip);
  svg.addEventListener("click", e => { const s = near(e); if (s) pick(s.n, false); });
  const box = document.getElementById("size"); box.textContent = ""; box.appendChild(svg);
}

function detail() {
  const d = document.getElementById("detail"); d.textContent = "";
  const s = D.find(x => x.n === picked);
  if (!s) { d.appendChild(h("p", "", "Click a point in the chart or a name in the table to see one skill here.")).style.marginBottom = "6px"; return; }
  d.appendChild(h("h3", "", s.n));
  d.appendChild(h("p", "", s.p || "No purpose line written yet."));
  const dl = h("dl");
  const row = (k, v) => { dl.appendChild(h("dt", "", k)); const dd = h("dd"); if (typeof v === "string") dd.textContent = v; else dd.appendChild(v); dl.appendChild(dd); };
  const meter = (v, lim, unit) => { const f = document.createDocumentFragment(), m = h("span", "meter"), i = h("i"); i.style.width = Math.min(100, 100 * v / lim) + "%"; m.appendChild(i); f.appendChild(m);
    f.appendChild(document.createTextNode(`${fmt(v)} of ${fmt(lim)} ${unit} (${Math.round(100 * v / lim)} %)`)); return f; };
  row("Loaded by", who(s));
  row("Lives in", HOME[s.home]);
  row("Description", meter(s.chars, LIM.chars, "characters"));
  row("Body", meter(s.lines, LIM.lines, "lines"));
  row("Brings along", s.files ? `${fmt(s.files)} file${s.files > 1 ? "s" : ""}` + (s.has.length ? " in " + s.has.join(", ") : "") : "instructions only");
  d.appendChild(dl);
  const b = h("button", "b", "Close"); b.addEventListener("click", () => pick(s.n, false)); d.appendChild(b);
}

function table() {
  const t = document.getElementById("tab"); t.textContent = "";
  const head = h("tr"); [["Skill"], ["What it is for"], ["Loaded by"], ["Lines", 1], ["Characters", 1]].forEach(([x, num]) => head.appendChild(h("th", num ? "num" : "", x))); t.appendChild(head);
  D.filter(shown).forEach(s => {
    const tr = h("tr", s.n === picked ? "sel" : ""), td = h("td"), b = h("button", "", s.n);
    b.setAttribute("aria-pressed", s.n === picked ? "true" : "false"); b.addEventListener("click", () => pick(s.n, true)); td.appendChild(b); tr.appendChild(td);
    tr.appendChild(h("td", "", s.p)); tr.appendChild(h("td", "", who(s))); tr.appendChild(h("td", "num", fmt(s.lines))); tr.appendChild(h("td", "num", fmt(s.chars)));
    t.appendChild(tr);
  });
}

function fbar() {
  const f = document.getElementById("fbar"); f.textContent = "";
  const n = D.filter(shown).length, sp = h("span");
  sp.appendChild(document.createTextNode("Showing ")); sp.appendChild(h("b", "", `${n} of ${D.length}`)); sp.appendChild(document.createTextNode(" skills" + (filter ? ": " + filter.name : "")));
  f.appendChild(sp);
  if (filter) { const b = h("button", "b", "Show all"); b.addEventListener("click", () => setFilter(null)); f.appendChild(b); }
}

function draw() { hideTip(); fbar(); bars("who", WHO); bars("brings", BRINGS); size(); detail(); table(); }
addEventListener("scroll", hideTip, {passive: true});
tiles();
const m = /^#skill=(.+)$/.exec(location.hash);
if (m && D.some(s => s.n === decodeURIComponent(m[1]))) picked = decodeURIComponent(m[1]);
draw();
</script>
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("clara", "pool", "shared", "setup"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--out", type=Path, default=HERE / "skills.html")
    a = ap.parse_args()
    skills = collect(a.clara.expanduser(), a.pool.expanduser(), a.shared.expanduser(), a.setup.expanduser())
    nav = re.search(r"<nav class=\"nav\".*?</nav>\n", (HERE / "workflow.html").read_text(encoding="utf-8"), re.DOTALL).group(0)
    nav = nav.replace(' class="here" aria-current="page"', "").replace('<a href="skills.html">', '<a class="here" aria-current="page" href="skills.html">')
    today = datetime.date.today()
    page = (PAGE.replace("__NAV__", nav).replace("__DATE__", f"{today.day} {today:%B %Y}")
            .replace("__DATA__", json.dumps(skills, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__LIMIT_LINES__", str(LIMIT_LINES)).replace("__LIMIT_CHARS__", str(LIMIT_CHARS)))
    a.out.write_text(page, encoding="utf-8")
    print(f"{a.out.name}: {len(skills)} skills, {sum(s['clara'] for s in skills)} loaded by Clara, {sum(s['setup'] for s in skills)} by the setup sessions, "
          f"{sum(s['lines'] > LIMIT_LINES or s['chars'] > LIMIT_CHARS for s in skills)} above a limit")


if __name__ == "__main__":
    main()
