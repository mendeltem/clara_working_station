"""Builds skills-overview.html: all skills one after another, each with what it does and what a review found.

    python3 skills_overview_page.py --clara DIR --pool DIR --shared DIR --setup DIR

The folders are the same as for skills_page.py, which supplies the counts. The texts on the page come from
skills_review.json: hand-checked lines per skill (what it does, when it loads, findings, proposal, verdict).
The skill texts themselves are never copied: they may name tools, folders or rules that stay on the machine.
Standard library only."""
import argparse, datetime, html, json, re
from pathlib import Path

from skills_page import HERE, LIMIT_CHARS, LIMIT_LINES, collect

VERDICTS = [("good", "Good", "✓", "nothing of weight found"),
            ("usable", "Usable", "●", "works, small gaps"),
            ("needs work", "Needs work", "▲", "a gap that can make it fail or not load"),
            ("outdated", "Outdated", "◷", "describes a setup that no longer exists")]
POINTS = {1: ("Description", "says what the skill does and when to load it"),
          2: ("Instructions", "concrete steps and commands instead of general advice"),
          3: ("Check before done", "says how to verify the result"),
          4: ("Failure section", "says what to do when a step fails"),
          5: ("Outdated content", "tools, services or files that have changed since"),
          6: ("Overlap", "repeats what another skill already covers"),
          7: ("Size and focus", "one job per skill, long material in reference files")}
HOME = {"clara": "Clara's skill folder", "shared": "shared folder for all agents", "setup": "the setup sessions' own folder"}
BRINGS = {"scripts": "scripts", "references": "reference texts", "assets": "assets", "templates": "templates", "evals": "test cases"}
SEVERITY = ["high", "medium", "low"]
E = html.escape


def who(s: dict) -> str:
    return ("Clara and the setup sessions" if s["clara"] and s["setup"] else "Clara" if s["clara"]
            else "the setup sessions" if s["setup"] else "nobody yet (kept in Clara's folder, not linked)")


def meter(value: int, limit: int, unit: str) -> str:
    pct = min(100, round(100 * value / limit))
    return (f'<span class="meter" role="img" aria-label="{value} of {limit} {unit}"><i style="width:{pct}%"></i></span>'
            f'{value:,} <span class="mu">of {limit:,} {unit}</span>')


def state(f: dict) -> str:
    """What the repair did with one finding; empty for a skill that was not repaired."""
    if f.get("state") == "fixed":
        return '<span class="fx">✓ repaired</span>'
    if f.get("state") == "open":
        return f'<span class="op">○ open: {E(f["open_why"])}</span>'
    return ""


def card(no: int, total: int, s: dict, r: dict, films: dict) -> str:
    key, label, icon, _ = next(v for v in VERDICTS if v[0] == r["verdict"])
    pts = sorted({f["point"] for f in r["findings"]})
    does = "".join(f"<li>{E(d)}</li>" for d in r["does"])
    if r["findings"]:
        finds = "<ul class=\"fd\">" + "".join(
            f'<li><span class="sv sv-{E(f["severity"])}">{E(f["severity"])}</span><span class="ft2">{E(f["text"])} '
            f'<span class="pt">{E(POINTS[f["point"]][0])}</span>{state(f)}</span></li>' for f in sorted(r["findings"], key=lambda f: SEVERITY.index(f["severity"]))) + "</ul>"
    else:
        finds = '<p class="none">No finding.</p>'
    brings = ", ".join(BRINGS[b] for b in s["has"]) if s["has"] else "instructions only"
    film = films.get(s["n"])
    film_row = f'<dt>Related film</dt><dd><a class="gh" href="{E(film["href"])}">{E(film["title"])}</a></dd>' if film else ""
    strength = f'<h4>Strength</h4><p>{E(r["strength"])}</p>' if r.get("strength") else ""
    proposal = f'<h4>Proposed change</h4><p>{E(r["proposal"])}</p>' if r.get("proposal") else ""
    rp = r.get("repair")
    repaired = f'<span class="rp" title="findings repaired after the review">repaired {rp["fixed"]}/{rp["of"]}</span>' if rp else ""
    return f"""<article class="sk" id="s-{E(s['n'])}" data-v="{E(key)}" data-r="{'1' if rp else '0'}" data-points="{' '.join(map(str, pts))}" tabindex="-1">
 <header><span class="no">{no:02d}<span class="of">/{total}</span></span><h3>{E(s['n'])}</h3>{repaired}<span class="vd v-{key.replace(' ', '-')}"><span aria-hidden="true">{icon}</span> {label}</span></header>
 <p class="pp">{E(s['p'])}</p>
 <div class="cols">
  <div><h4>Loads when</h4><p>{E(r['when'])}</p><h4>What it does</h4><ul class="do">{does}</ul>{strength}</div>
  <div><h4>Review findings</h4>{finds}{proposal}</div>
 </div>
 <dl class="meta"><dt>Loaded by</dt><dd>{E(who(s))}</dd><dt>Kept in</dt><dd>{E(HOME[s['home']])}</dd>
  <dt>Instructions</dt><dd>{meter(s['lines'], LIMIT_LINES, 'lines')}</dd><dt>Description</dt><dd>{meter(s['chars'], LIMIT_CHARS, 'characters')}</dd>
  <dt>Brings</dt><dd>{E(brings)}</dd>{film_row}</dl>
 <div class="cn"><a href="#index">↑ index</a></div>
</article>
"""


def chart_verdicts(topics: list, by_topic: dict) -> str:
    """Stacked horizontal bars: one row per topic, one segment per verdict. Segments are buttons that filter."""
    W, L, R, row, top = 560, 252, 30, 30, 8
    span = W - L - R
    most = max(len(v) for v in by_topic.values())
    out = [f'<svg viewBox="0 0 {W} {top + row * len(topics) + 6}" role="group" aria-label="Verdicts by topic">']
    for i, (tkey, tname) in enumerate(topics):
        y = top + i * row
        rows = by_topic[tkey]
        out.append(f'<text x="{L - 10}" y="{y + 15}" text-anchor="end" font-size="11.5" fill="#e8f1f3">{E(tname)}</text>')
        x = L
        for key, label, _, _ in VERDICTS:
            n = sum(1 for r in rows if r["verdict"] == key)
            if not n:
                continue
            w = span * n / most
            out.append(f'<g class="bar" tabindex="0" role="button" data-f="v:{E(key)}" data-t="{E(tkey)}" data-tip="{E(tname)}|{n} of {len(rows)} skills: {E(label.lower())}">'
                       f'<rect class="m v-{key.replace(" ", "-")}" x="{x + 1:.1f}" y="{y + 3}" width="{max(2, w - 2):.1f}" height="17" rx="4"/>'
                       f'<rect class="hit" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{row - 6}" fill="transparent"/></g>')
            x += w
        out.append(f'<text x="{x + 7:.1f}" y="{y + 15}" font-size="11.5" fill="#7d99a2">{len(rows)}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="lg"><i class="v-{k.replace(" ", "-")}"></i><span aria-hidden="true">{ic}</span> {E(lb)}</span>' for k, lb, ic, _ in VERDICTS)
    return "".join(out) + f'<div class="legend">{legend}</div>'


def chart_points(reviews: list) -> str:
    """Horizontal bars: number of skills with at least one finding under each review point."""
    W, L, R, row, top = 560, 150, 40, 30, 8
    counts = {p: sum(1 for r in reviews if any(f["point"] == p for f in r["findings"])) for p in POINTS}
    most = max(max(counts.values()), 1)
    out = [f'<svg viewBox="0 0 {W} {top + row * len(POINTS) + 6}" role="group" aria-label="Skills with a finding, by review point">']
    for i, (p, (name, what)) in enumerate(POINTS.items()):
        y = top + i * row
        w = (W - L - R) * counts[p] / most
        out.append(f'<text x="{L - 10}" y="{y + 14}" text-anchor="end" font-size="11.5" fill="#e8f1f3">{E(name)}</text>'
                   f'<g class="bar" tabindex="0" role="button" data-f="p:{p}" data-tip="{E(name)}|{counts[p]} of {len(reviews)} skills have a finding here. The point: {E(what)}.">'
                   f'<rect x="{L}" y="{y + 3}" width="{W - L - R}" height="14" rx="4" fill="#14343a"/>'
                   f'<rect class="m" x="{L}" y="{y + 3}" width="{max(w, 2 if counts[p] else 0):.1f}" height="14" rx="4" fill="#1f9a8e"/>'
                   f'<rect class="hit" x="{L}" y="{y}" width="{W - L - R}" height="{row - 4}" fill="transparent"/></g>'
                   f'<text x="{L + w + 7:.1f}" y="{y + 14}" font-size="11.5" fill="#e8f1f3">{counts[p]}</text>')
    out.append("</svg>")
    return "".join(out)


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Clara Working Station: all skills, one by one</title>
<meta name="description" content="All __N__ skills of the workstation one after another: what each one does, when it loads, and what a review of its text found.">
<link rel="stylesheet" href="site.css">
<style>
 :root{--mark:#1f9a8e;--track:#14343a;--good:__C_good__;--usable:__C_usable__;--needs:__C_needs__;--old:__C_old__}
 body{max-width:1120px}
 .tiles4{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0 6px}
 .st{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:12px 14px}
 .st .v{font-size:24px;font-weight:800;color:var(--ink);line-height:1.2}
 .st .l{color:var(--muted);font-size:11.5px}
 .two{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:11px}
 .card{border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:14px 14px 10px}
 .card h3{font-size:13px;font-weight:700;color:var(--ink);margin-bottom:2px}
 .card .h{color:var(--muted);font-size:11.5px;margin-bottom:8px}
 .card svg{display:block;width:100%;height:auto}
 .card svg text{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
 .bar{cursor:pointer}.bar:hover .m,.bar:focus-visible .m{filter:brightness(1.25)}
 .bar:focus-visible{outline:none}.bar:focus-visible .hit{stroke:var(--accent);stroke-width:1.5}
 rect.v-good,i.v-good{fill:var(--good);background:var(--good)}rect.v-usable,i.v-usable{fill:var(--usable);background:var(--usable)}
 rect.v-needs-work,i.v-needs-work{fill:var(--needs);background:var(--needs)}rect.v-outdated,i.v-outdated{fill:var(--old);background:var(--old)}
 .legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11.5px;color:var(--muted);margin-top:6px}
 .lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
 .fbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:8px 10px;flex-wrap:wrap;margin:22px 0 6px;padding:9px 0;font-size:12.5px;
       color:var(--muted);background:var(--bg);border-bottom:1px solid var(--line)}
 .fbar b{color:var(--ink)}.fbar .sp{flex:1}
 button.b{font:inherit;font-size:12px;color:var(--accent2);background:none;border:1px solid var(--line);border-radius:7px;padding:3px 10px;cursor:pointer}
 button.b:hover{border-color:var(--accent2)}button.b[aria-pressed=true]{border-color:var(--accent);color:var(--ink)}
 ol.ix{list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(205px,1fr));gap:5px 12px;margin:10px 0 4px;font-size:12px}
 ol.ix a{display:flex;gap:7px;align-items:baseline;color:var(--ink);text-decoration:none;padding:2px 0}
 ol.ix a:hover .nm{color:var(--accent2);text-decoration:underline}
 ol.ix .n{color:var(--muted);font-variant-numeric:tabular-nums}
 ol.ix .dot{margin-left:auto;font-size:10.5px}
 .c-good{color:var(--good)}.c-usable{color:var(--usable)}.c-needs-work{color:var(--needs)}.c-outdated{color:var(--old)}
 ol.ix li.off,article.sk.off,h2.tp.off{display:none}
 article.sk{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:16px 18px 10px;margin-bottom:12px;scroll-margin-top:56px}
 article.sk:focus{outline:none;border-color:var(--accent2)}
 article.sk header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
 .no{color:var(--muted);font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}.no .of{font-weight:400;font-size:11px}
 article.sk h3{font-size:17px;font-weight:800;color:var(--accent2)}
 .vd{margin-left:auto;font-size:11.5px;color:var(--ink);border:1px solid var(--line);border-radius:20px;padding:2px 11px;white-space:nowrap}
 .vd span{margin-right:3px}.vd.v-good span{color:var(--good)}.vd.v-usable span{color:var(--usable)}.vd.v-needs-work span{color:var(--needs)}.vd.v-outdated span{color:var(--old)}
 .vd.v-good{border-color:var(--good)}.vd.v-usable{border-color:var(--usable)}.vd.v-needs-work{border-color:var(--needs)}.vd.v-outdated{border-color:var(--old)}
 .pp{color:var(--ink);font-size:13px;margin:7px 0 4px}
 .cols{display:grid;grid-template-columns:1fr 1fr;gap:4px 30px}
 article.sk h4{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:13px 0 5px}
 article.sk p,article.sk li{color:var(--muted);font-size:12.5px}
 ul.do{margin-left:17px}ul.do li{margin-bottom:3px}
 ul.fd{list-style:none}ul.fd li{display:flex;gap:9px;align-items:baseline;margin-bottom:6px}
 .sv{flex:none;width:54px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;text-align:center;border-radius:4px;padding:1px 0;color:var(--ink);border:1px solid var(--line)}
 .sv-high{border-color:var(--needs)}.sv-medium{border-color:#56707a}.sv-low{border-style:dashed}
 .pt{white-space:nowrap;font-size:10.5px;color:#5f7d87;margin-left:4px}
 p.none{font-style:italic}
 .fx,.op{display:block;font-size:11px;margin-top:1px}.fx{color:var(--good)}.op{color:var(--ink)}
 .rp{margin-left:auto;font-size:11.5px;color:var(--good);white-space:nowrap}.rp+.vd{margin-left:10px}
 dl.meta{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:5px 14px;font-size:11.5px;margin-top:14px;padding-top:11px;border-top:1px solid var(--line)}
 dl.meta dt{color:var(--muted)}dl.meta dd{color:var(--ink)}.mu{color:var(--muted)}
 .meter{display:inline-block;vertical-align:middle;width:110px;height:7px;border-radius:4px;background:var(--track);margin-right:8px;overflow:hidden}
 .meter i{display:block;height:100%;background:var(--mark);border-radius:4px}
 .cn{text-align:right;font-size:11px;margin-top:6px}.cn a{color:var(--muted);text-decoration:none}.cn a:hover{color:var(--accent2)}
 h2.tp{display:flex;gap:10px;align-items:baseline}h2.tp span{margin-left:auto;text-transform:none;letter-spacing:0}
 #tip{position:fixed;z-index:9;pointer-events:none;background:#06090b;border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:12px;
      color:var(--muted);max-width:290px;display:none}
 #tip b{display:block;color:var(--ink);font-size:13px}
 kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;font:inherit;font-size:11px;color:var(--ink)}
 @media (max-width:820px){.two,.cols{grid-template-columns:1fr}.fbar{position:static}article.sk{scroll-margin-top:8px}dl.meta{grid-template-columns:auto 1fr}.vd,.rp{margin-left:0}}
</style>
</head><body>
__NAV__<h1>All skills, <span class="a">one by one</span></h1>
<div class="sub">Clara Working Station · review of __DATE__</div>
<p class="s">Each of the <b>__N__ skills</b> was read in full and judged from its text against the seven points listed at the
bottom of this page. A card says what the skill does, when it loads, what the review found and the one change proposed.
The charts on the page <a class="gh" href="skills.html">Skills</a> show who loads which skill and how large each one is.</p>
<div class="tiles4">__TILES__</div>
__REPAIR__
<div class="two">
<div class="card"><h3>Verdict by topic</h3><div class="h">Number of skills. Click a segment to see only those skills.</div>__CHART_V__</div>
<div class="card"><h3>Where the findings are</h3><div class="h">Skills with at least one finding under a review point. Click a bar to see them.</div>__CHART_P__</div>
</div>
<div class="fbar" id="index"><span id="state" aria-live="polite"></span><span class="sp"></span>__BUTTONS__<button class="b" id="all" type="button">Show all</button></div>
<ol class="ix">__INDEX__</ol>
<p class="s" style="margin-top:8px">Keys: <kbd>→</kbd> or <kbd>j</kbd> next skill, <kbd>←</kbd> or <kbd>k</kbd> previous skill.</p>
__CARDS__
<h2>How the review was done</h2>
<ul class="s">
<li><b>What was read.</b> The instruction file of every skill in full, and the list of files in its folder, in four reading passes.
Every finding rests on a passage of the skill file; a program checked that each such passage occurs there word for word.
Some findings also rest on a read-only look at the machine: does a named tool, file or service still exist. No skill was run on a task.</li>
<li><b>The seven points.</b> __POINTS__</li>
<li><b>The verdicts.</b> __VERDICTS__</li>
<li><b>What a verdict is not.</b> A reading of the text, not a measurement. Whether a description really loads its skill
has to be measured with test requests, and the four reading passes may differ a little in strictness.
“May be outdated” in a finding means: the text points to a setup that has changed, and the step was not re-tested.</li>
<li><b>The repair.</b> After the review the __RN__ skills judged “needs work” or “outdated” were reworked along their
findings; only the instruction texts were changed, nothing was removed or merged, and every command a text newly names
was checked to exist on the machine. “Repaired” means the text was changed for that finding, not that the skill was
run on a task. The verdict on a card is still the one of the review: the skills were not judged a second time.</li>
<li><b>What is published.</b> Only names, counts and hand-checked lines; the skill texts stay on the machine.
The limits shown (1,024 characters for a description, 500 lines recommended for the instructions) are those of the
<a class="gh" href="https://agentskills.io/specification" target="_blank" rel="noopener">Agent Skills specification</a>.</li>
</ul>
<div class="pn"><a href="skills.html">← Skills: the charts</a><a href="setup.html">Rebuild it →</a></div>
<div id="tip" role="tooltip"></div>
<script>
const cards = [...document.querySelectorAll("article.sk")], items = [...document.querySelectorAll("ol.ix li")], heads = [...document.querySelectorAll("h2.tp")];
const state = document.getElementById("state"), tip = document.getElementById("tip"), total = cards.length;
const NAMES = __NAMES__;
let filter = null;
const match = c => !filter || (filter.kind === "v" ? c.dataset.v === filter.val && (!filter.topic || c.dataset.t === filter.topic)
                                                   : c.dataset.points.split(" ").includes(filter.val));
function apply() {
  let n = 0;
  cards.forEach((c, i) => { const on = match(c); c.classList.toggle("off", !on); items[i].classList.toggle("off", !on); n += on; });
  heads.forEach(h => h.classList.toggle("off", !cards.some(c => c.dataset.t === h.dataset.t && !c.classList.contains("off"))));
  state.innerHTML = "";
  const b = document.createElement("b"); b.textContent = filter ? "Showing " + n + " of " + total + " skills" : "All " + total + " skills";
  state.appendChild(b);
  if (filter) state.appendChild(document.createTextNode(": " + filter.name));
  document.querySelectorAll(".fbar button[data-f]").forEach(x => x.setAttribute("aria-pressed", String(!!filter && !filter.topic && x.dataset.f === filter.kind + ":" + filter.val)));
  document.getElementById("all").style.visibility = filter ? "visible" : "hidden";
}
function setFilter(f, topic) {
  const [kind, val] = [f.slice(0, 1), f.slice(2)];
  const same = filter && filter.kind === kind && filter.val === val && (filter.topic || "") === (topic || "");
  filter = same ? null : {kind, val, topic: topic || "", name: (kind === "v" ? NAMES.v[val] : "with a finding under “" + NAMES.p[val] + "”") + (topic ? ", topic “" + NAMES.t[topic] + "”" : "")};
  apply();
}
document.querySelectorAll("[data-f]").forEach(e => {
  e.addEventListener("click", () => setFilter(e.dataset.f, e.dataset.t));
  e.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); setFilter(e.dataset.f, e.dataset.t); } });
});
document.getElementById("all").addEventListener("click", () => { filter = null; apply(); });
document.querySelectorAll("[data-tip]").forEach(e => {
  const show = ev => { const [a, b] = e.dataset.tip.split("|"); tip.innerHTML = ""; const s = document.createElement("b"); s.textContent = a; tip.appendChild(s); tip.appendChild(document.createTextNode(b));
    tip.style.display = "block"; const r = ev.clientX != null && ev.type !== "focus" ? {x: ev.clientX, y: ev.clientY} : (() => { const q = e.getBoundingClientRect(); return {x: q.left + q.width / 2, y: q.bottom}; })();
    tip.style.left = Math.max(6, Math.min(r.x + 14, innerWidth - tip.offsetWidth - 8)) + "px"; tip.style.top = Math.min(r.y + 14, innerHeight - tip.offsetHeight - 8) + "px"; };
  e.addEventListener("mousemove", show); e.addEventListener("focus", show);
  e.addEventListener("mouseleave", () => tip.style.display = "none"); e.addEventListener("blur", () => tip.style.display = "none");
});
function step(d) {
  const vis = cards.filter(c => !c.classList.contains("off")); if (!vis.length) return;
  const line = 70; let i = vis.findIndex(c => c.getBoundingClientRect().top > line - 2);
  let cur = i === -1 ? vis.length - 1 : i - 1;
  if (vis[i] && Math.abs(vis[i].getBoundingClientRect().top - 56) < 4) cur = i;
  const next = vis[Math.max(0, Math.min(vis.length - 1, cur + d))];
  next.scrollIntoView({block: "start"}); next.focus({preventScroll: true}); history.replaceState(null, "", "#" + next.id);
}
document.addEventListener("keydown", ev => {
  if (ev.target.closest("button,[role=button],input,a") || ev.altKey || ev.ctrlKey || ev.metaKey) return;
  if (ev.key === "ArrowRight" || ev.key === "j") { ev.preventDefault(); step(1); }
  if (ev.key === "ArrowLeft" || ev.key === "k") { ev.preventDefault(); step(-1); }
});
apply();
</script>
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("clara", "pool", "shared", "setup"):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--out", type=Path, default=HERE / "skills-overview.html")
    a = ap.parse_args()
    skills = {s["n"]: s for s in collect(a.clara.expanduser(), a.pool.expanduser(), a.shared.expanduser(), a.setup.expanduser())}
    data = json.loads((HERE / "skills_review.json").read_text(encoding="utf-8"))
    topics, reviews, films = [(t["key"], t["name"]) for t in data["topics"]], data["skills"], data.get("films", {})
    order = [n for t in data["topics"] for n in t["skills"]]
    missing, extra = sorted(set(skills) - set(order)), sorted(set(order) - set(skills))
    if missing or extra or set(order) != set(reviews):
        raise SystemExit(f"skills_review.json does not match the folders: no review for {missing}, review without skill {extra}")
    colors = data["colors"]
    by_topic = {t["key"]: [reviews[n] for n in t["skills"]] for t in data["topics"]}
    cards, index, no = [], [], 0
    for t in data["topics"]:
        cards.append(f'<h2 class="tp" data-t="{E(t["key"])}">{E(t["name"])}<span>{len(t["skills"])} skills</span></h2>\n')
        for n in t["skills"]:
            no += 1
            r = reviews[n]
            icon = next(v[2] for v in VERDICTS if v[0] == r["verdict"])
            cards.append(card(no, len(order), skills[n], r, films).replace('<article class="sk"', f'<article class="sk" data-t="{E(t["key"])}"', 1))
            index.append(f'<li><a href="#s-{E(n)}"><span class="n">{no:02d}</span><span class="nm">{E(n)}</span>'
                         f'<span class="dot c-{r["verdict"].replace(" ", "-")}" title="{E(r["verdict"])}">{icon}</span></a></li>')
    count = {k: sum(1 for r in reviews.values() if r["verdict"] == k) for k, *_ in VERDICTS}
    tiles = f'<div class="st"><div class="v">{len(order)}</div><div class="l">skills reviewed</div></div>' + "".join(
        f'<div class="st"><div class="v">{count[k]}</div><div class="l"><span class="c-{k.replace(" ", "-")}" aria-hidden="true">{ic}</span> {E(lb.lower())}: {E(what)}</div></div>'
        for k, lb, ic, what in VERDICTS if k != "outdated" or count[k])
    buttons = "".join(f'<button class="b" type="button" data-f="v:{E(k)}" aria-pressed="false"><span class="c-{k.replace(" ", "-")}" aria-hidden="true">{ic}</span> {E(lb)} {count[k]}</button>'
                      for k, lb, ic, _ in VERDICTS if count[k])
    nav = re.search(r"<nav class=\"nav\".*?</nav>\n", (HERE / "workflow.html").read_text(encoding="utf-8"), re.DOTALL).group(0)
    nav = nav.replace(' class="here" aria-current="page"', "").replace('<a href="skills.html">', '<a class="here" href="skills.html">')
    names = {"v": {k: lb.lower() for k, lb, *_ in VERDICTS}, "p": {str(p): v[0] for p, v in POINTS.items()}, "t": dict(topics)}
    today = datetime.date.fromisoformat(data["date"])
    page = (PAGE.replace("__NAV__", nav).replace("__DATE__", f"{today.day} {today:%B %Y}").replace("__N__", str(len(order)))
            .replace("__TILES__", tiles).replace("__CHART_V__", chart_verdicts(topics, by_topic)).replace("__CHART_P__", chart_points(list(reviews.values())))
            .replace("__BUTTONS__", buttons).replace("__INDEX__", "".join(index)).replace("__CARDS__", "".join(cards))
            .replace("__POINTS__", " ".join(f"<b>{E(n)}</b>: {E(w)}." for n, w in POINTS.values()))
            .replace("__VERDICTS__", " ".join(f"<b>{E(lb)}</b>: {E(w)}." for _, lb, _, w in VERDICTS))
            .replace("__NAMES__", json.dumps(names).replace("</", "<\\/"))
            .replace("__C_good__", colors["good"]).replace("__C_usable__", colors["usable"]).replace("__C_needs__", colors["needs work"]).replace("__C_old__", colors["outdated"]))
    done = [r["repair"] for r in reviews.values() if r.get("repair")]
    fixed, of = sum(d["fixed"] for d in done), sum(d["of"] for d in done)
    rd = datetime.date.fromisoformat(data["repair_date"]) if done else None
    repair = (f'<p class="s rpl"><b>Repair, {rd.day} {rd:%B %Y}:</b> {len(done)} skills reworked, <b>{fixed} of {of}</b> of their findings repaired, '
              f'{of - fixed} left open with the reason on the card. The verdicts and both charts show the state at the review.</p>') if done else ""
    page = page.replace("__REPAIR__", repair).replace("__RN__", str(len(done)))
    a.out.write_text(page, encoding="utf-8")
    print(f"{a.out.name}: {len(order)} skills in {len(topics)} topics; " + ", ".join(f"{count[k]} {k}" for k, *_ in VERDICTS))


if __name__ == "__main__":
    main()
