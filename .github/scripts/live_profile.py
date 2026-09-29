#!/usr/bin/env python3
"""Render assets/live/telemetry.svg from the GitHub GraphQL API. Stdlib only.

Replaces third-party stat cards (they 402, move hosts, and GitHub's image proxy
serves them up to 24h stale) with an SVG built here, from the same API, on a
schedule. Every figure is the *public* view: the contribution calendar as visitors
see it plus public repos. Actions' token sees exactly that (its streak, active days
and total match github.com/<user>'s own calendar), and nothing about private repos
is read or published.

The SVG is built with ElementTree (escaping is the library's job) and written
pretty-printed: one element per line, all styling in a single <style> block, so the
file reads well and a data change touches a few lines.

    GITHUB_TOKEN=... python3 .github/scripts/live_profile.py   # write the SVG
    python3 .github/scripts/live_profile.py --selftest         # no network

Run locally with an owner-scoped token (e.g. `gh auth token`) and the calendar also
carries private per-day detail, so streaks and active days read higher than the
public view. Take committed SVGs from CI (the PR dry run uploads one), not a laptop.
"""
from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Template

USER = "Ajitkumar-1001"
OUT = Path("assets/live/telemetry.svg")

# Black-Architect tokens, same as the hand-made GIF panels. Type scale matches them too:
# nothing below BASE px (asserted in --selftest).
BG, PANEL, BORDER = "#000000", "#050b16", "#0b1f3a"
ACCENT, TEXT, MUTED, BLUE, MID = "#7dbbff", "#e6eefc", "#9fb3d1", "#1e90ff", "#2f6fc4"
FONT = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono","Liberation Mono",monospace'
CW = 0.62  # em per glyph. Real monospace fonts are 0.60-0.602, so fitted text never overflows.
BASE = 16
W, H, PAD = 1200, 540, 28

# ponytail: first 100 own public repos (24 today); paginate if that ever stops being enough.
QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(first: 100, privacy: PUBLIC, isFork: false, ownerAffiliations: OWNER) {
      totalCount
      nodes { stargazerCount languages(first: 8, orderBy: {field: SIZE, direction: DESC}) { totalSize edges { size node { name } } } }
    }
    contributionsCollection { contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } } }
  }
}"""


# --------------------------------------------------------------------- fetch
def graphql(token: str, login: str) -> dict:
    # ponytail: no retry. A transient API error fails the run (red) and the last good SVG
    # stays up until the next 3-hourly run.
    body = json.dumps({"query": QUERY, "variables": {"login": login}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", body,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "live-profile"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if out.get("errors") or not (out.get("data") or {}).get("user"):
        raise RuntimeError(f"GraphQL failed: {out.get('errors') or 'user not found'}")
    return out["data"]["user"]


# ------------------------------------------------------------------- compute
Streak = tuple[int, "date | None", "date | None"]  # (length, first day, last day)


def streaks(days: list[tuple[date, int]]) -> tuple[Streak, Streak]:
    """(current, longest), each (length, first_day, last_day). `days` is oldest to newest.

    Current streak ends today, or yesterday if today has no contribution yet
    (the day isn't over), matching how GitHub's own graph reads.
    """
    best = (0, None, None)
    run = 0
    for i, (d, n) in enumerate(days):
        run = run + 1 if n > 0 else 0
        if run > best[0]:
            best = (run, days[i - run + 1][0], d)
    end = len(days) - 1
    if days[end][1] == 0:
        end -= 1
    cur = 0
    while end - cur >= 0 and days[end - cur][1] > 0:
        cur += 1
    current = (cur, days[end - cur + 1][0], days[end][0]) if cur else (0, None, None)
    return current, best


def language_share(repos: list[dict]) -> list[tuple[str, int]]:
    """Top 5 languages as % of repos that use them (>=10% of that repo's bytes), most common first.

    Counting repos rather than bytes: one vendored 45 MB HTML file or a notebook full
    of outputs would otherwise make an ML profile read "70% HTML".
    """
    used: Counter[str] = Counter()
    n = 0
    for repo in repos:
        langs = repo["languages"]
        if not langs["totalSize"]:
            continue
        n += 1
        used.update({e["node"]["name"] for e in langs["edges"] if e["size"] / langs["totalSize"] >= 0.10})
    ranked = sorted(used.items(), key=lambda kv: (-kv[1], kv[0]))  # name breaks ties, so output is stable between runs
    return [(name, round(100 * c / n)) for name, c in ranked[:5]] if n else []


def shape(user: dict, today: date) -> dict:
    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [(date.fromisoformat(d["date"]), d["contributionCount"]) for w in cal["weeks"] for d in w["contributionDays"]]
    cur, best = streaks(days)
    repos = user["repositories"]
    return {
        "today": today, "days": days, "total": cal["totalContributions"], "cur": cur, "best": best,
        "active": sum(1 for _, n in days if n), "best_day": max(days, key=lambda d: d[1]),
        "repos": repos["totalCount"], "stars": sum(r["stargazerCount"] for r in repos["nodes"]),
        "langs": language_share(repos["nodes"]),
    }


# ----------------------------------------------------------------------- svg
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# Every colour and text style lives here once; elements carry a class, not repeated attributes.
CSS = Template("""
svg{font-family:$font;font-size:${base}px}
text{fill:$muted}
.txt{fill:$text}
.mut{fill:$muted}
.acc{fill:$accent}
.end{text-anchor:end}
.num{fill:$text;font-weight:700}
.bg{fill:$bg}
.card{fill:$panel;stroke:$border;stroke-width:2}
.rule{stroke:$border;stroke-width:2}
.grid{stroke:$border;stroke-width:1.5}
.track{fill:$border}
.hud{fill:none;stroke:$accent;stroke-width:2}
.trend{fill:none;stroke:$blue;stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.dot{fill:$accent}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}
@keyframes draw{from{stroke-dashoffset:1}}
@keyframes grow{from{transform:scaleX(0)}}
@keyframes pulse{50%{opacity:.3}}
.rise{animation:rise .4s ease-out backwards}
.draw{stroke-dasharray:1;animation:draw 1s ease-out .4s backwards}
.bar{transform-box:fill-box;transform-origin:left center;animation:grow .6s ease-out backwards}
.live{animation:pulse 2s ease-in-out 2}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
""")


def num(v) -> str:
    """52.0 -> '52', 45.46 -> '45.5': compact numbers keep the markup and its diffs small."""
    return f"{round(v, 1):g}" if isinstance(v, float) else str(v)


def E(tag: str, parent: ET.Element | None = None, text: str | None = None, cls: str | None = None, **attrs) -> ET.Element:
    """One SVG element. Keyword names use underscores for dashes (stroke_width -> stroke-width).
    ElementTree escapes every attribute and text node, so an untrusted string cannot break the markup."""
    name = f"{{{SVG_NS}}}{tag}"
    el = ET.Element(name) if parent is None else ET.SubElement(parent, name)
    if cls:
        el.set("class", cls)
    for k, v in attrs.items():
        el.set(k.rstrip("_").replace("_", "-"), num(v) if isinstance(v, (int, float)) else v)
    el.text = text
    return el


def txt(parent: ET.Element, x, y, s: str, cls: str | None = None, size=BASE, **attrs) -> ET.Element:
    """A <text>. font-size is written only when it differs from BASE (the CSS supplies that)."""
    if size != BASE:
        attrs = {"font_size": size, **attrs}
    return E("text", parent, s, cls, x=x, y=y, **attrs)


def card(parent: ET.Element, x, y, w, h, r=14) -> None:
    E("rect", parent, cls="card", x=x, y=y, width=w, height=h, rx=r)


def hud(parent: ET.Element, x, y, w, h) -> None:
    """Corner ticks, 10px long."""
    d = "".join(f"M{px},{py + sy * 10}V{py}H{px + sx * 10}" for px, py, sx, sy in
                [(x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)])
    E("path", parent, cls="hud", d=d)


def fit(s: str, size: float, max_w: float) -> float:
    return min(size, max_w / (CW * len(s)))


def fmt_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def tile(parent: ET.Element, i: int, x: int, y: int, w: int, h: int, label: str, value: str, unit: str, sub: str) -> None:
    g = E("g", parent, cls="rise", style=f"animation-delay:{0.06 * i:g}s")
    card(g, x, y, w, h, 12)
    inner = w - 28
    unit_w = (len(unit) + 1) * CW * BASE if unit else 0
    size = min(46, (inner - unit_w) / (CW * len(value)))     # number shrinks before anything overflows
    txt(g, x + 14, y + 30, label, size=fit(label, BASE, inner))
    txt(g, x + 14, y + 78, value, "num", size, filter="url(#glow)")
    if unit:
        txt(g, x + 14 + len(value) * CW * size + 8, y + 78, unit)
    txt(g, x + 14, y + 101, sub, "acc", fit(sub, BASE, inner))


def activity(parent: ET.Element, x: int, y: int, w: int, h: int, days: list[tuple[date, int]]) -> None:
    g = E("g", parent, id="activity", cls="rise", style="animation-delay:.4s")
    card(g, x, y, w, h)
    last = days[-30:]
    vals = [n for _, n in last]
    peak = max(vals)
    px0, px1, py0, py1 = x + 26, x + w - 26, y + 66, y + h - 44
    step = (px1 - px0) / (len(vals) - 1)
    pts = [(px0 + i * step, py1 - (v / (peak or 1)) * (py1 - py0)) for i, v in enumerate(vals)]
    line = "M" + "L".join(f"{num(a)},{num(b)}" for a, b in pts)
    txt(g, x + 26, y + 34, "ACTIVITY / LAST 30 DAYS", "acc")
    txt(g, x + w - 26, y + 34, f"PEAK {peak}", "end")
    for gy in (py0, (py0 + py1) / 2, py1):
        E("line", g, cls="grid", x1=px0, x2=px1, y1=gy, y2=gy)
    E("path", g, fill="url(#area)", d=f"{line}L{num(pts[-1][0])},{py1}L{num(pts[0][0])},{py1}Z")
    E("path", g, cls="trend draw", pathLength=1, d=line)
    E("circle", g, cls="dot live", cx=pts[-1][0], cy=pts[-1][1], r=5)
    txt(g, px0, y + h - 14, fmt_day(last[0][0]))
    txt(g, px1, y + h - 14, fmt_day(last[-1][0]), "end")


def languages(parent: ET.Element, x: int, y: int, w: int, h: int, langs: list[tuple[str, int]]) -> None:
    g = E("g", parent, id="languages", cls="rise", style="animation-delay:.5s")
    card(g, x, y, w, h)
    txt(g, x + 24, y + 34, "LANGUAGES / % OF REPOS", "acc")
    name_w, pct_w = 170, 52
    bx, bw = x + 24 + name_w, w - 48 - name_w - pct_w - 8
    for i, (name, pct) in enumerate(langs):
        ry = y + 84 + i * 34
        name = name if len(name) <= 17 else name[:16] + "…"  # cap at the name column instead of shrinking the font
        txt(g, x + 24, ry, name, "txt", fit(name, BASE, name_w - 6))
        E("rect", g, cls="track", x=bx, y=ry - 11, width=bw, height=10, rx=5)
        E("rect", g, cls="bar", style=f"animation-delay:{0.55 + 0.08 * i:g}s", x=bx, y=ry - 11,
          width=max(bw * pct / 100, 4), height=10, rx=5, fill="url(#bar)")
        txt(g, x + w - 24, ry, f"{pct}%", "end")
    if not langs:
        txt(g, x + 24, y + 84, "no languages detected yet")


def render(s: dict) -> ET.Element:
    # Labels and subtitles stay <= 14 chars: the most that fits a 6-across tile at 16px.
    tiles = [
        ("CONTRIBUTIONS", f"{s['total']:,}", "", "last 12 months"),
        ("CURRENT STREAK", f"{s['cur'][0]}", "days", f"since {fmt_day(s['cur'][1])}" if s["cur"][0] else "no streak"),
        ("LONGEST STREAK", f"{s['best'][0]}", "days", f"{fmt_day(s['best'][1])}–{fmt_day(s['best'][2])}" if s["best"][0] else "none yet"),
        ("ACTIVE DAYS", f"{s['active']}", "", f"of {len(s['days'])} days"),
        ("BEST DAY", f"{s['best_day'][1]:,}", "", f"on {fmt_day(s['best_day'][0])}"),
        ("ORIGINAL REPOS", f"{s['repos']:,}", "", f"{s['stars']:,} star{'' if s['stars'] == 1 else 's'}"),
    ]
    px, pw = PAD, W - 2 * PAD
    ix, iw = px + 24, pw - 48                      # panel content box
    gap = 16
    tw = (iw - 5 * gap) // 6
    ty, th = 118, 116
    by, bh = ty + th + 22, 232
    lw = 440
    cw = iw - lw - gap
    py, ph = 90, by + bh + 22 - 90
    stamp = f"SYNC: {s['today'].isoformat()}"
    # The chart and bars have no other text alternative, so the figures live here as well.
    desc = (f"{s['total']} contributions in the last 12 months, current streak {s['cur'][0]} days, longest streak {s['best'][0]} days, "
            f"active on {s['active']} days, busiest day {s['best_day'][1]}. {s['repos']} original public repos, {s['stars']} stars. "
            f"Top languages by repo: {', '.join(f'{n} {p}%' for n, p in s['langs']) or 'none'}.")

    svg = E("svg", width=W, height=H, viewBox=f"0 0 {W} {H}", role="img", aria_labelledby="t d")
    E("title", svg, f"Live GitHub telemetry for {USER}", id="t")
    E("desc", svg, "\n" + textwrap.indent(textwrap.fill(desc, 96), "    ") + "\n  ", id="d")
    defs = E("defs", svg)
    area = E("linearGradient", defs, id="area", x1=0, y1=0, x2=0, y2=1)
    E("stop", area, offset=0, stop_color=ACCENT, stop_opacity=".35")
    E("stop", area, offset=1, stop_color=ACCENT, stop_opacity="0")
    bar = E("linearGradient", defs, id="bar", x1=0, y1=0, x2=1, y2=0)
    E("stop", bar, offset=0, stop_color=MID)
    E("stop", bar, offset=1, stop_color=ACCENT)
    glow = E("filter", defs, id="glow", x="-20%", y="-40%", width="140%", height="180%")
    E("feGaussianBlur", glow, stdDeviation=3, result="b")
    merge = E("feMerge", glow)
    E("feMergeNode", merge, in_="b")
    E("feMergeNode", merge, in_="SourceGraphic")
    css = CSS.substitute(font=FONT, base=BASE, bg=BG, panel=PANEL, border=BORDER, accent=ACCENT, text=TEXT, muted=MUTED, blue=BLUE)
    E("style", svg, "\n" + textwrap.indent(css.strip(), "    ") + "\n  ")

    E("rect", svg, cls="bg", width=W, height=H)
    head = E("g", svg, id="header")
    title = txt(head, PAD, 40, "Live Telemetry", "txt", 22)
    E("tspan", title, "/ last 12 months", "mut", font_size=BASE, dx=14)
    E("circle", head, cls="dot live", cx=W - PAD - len(stamp) * CW * BASE - 14, cy=34, r=4)
    txt(head, W - PAD, 40, stamp, "end")
    E("line", head, cls="rule", x1=PAD, x2=W - PAD, y1=60, y2=60)
    E("rect", svg, cls="card", x=px, y=py, width=pw, height=ph, rx=18)
    hud(svg, px + 10, py + 10, pw - 20, ph - 20)
    row = E("g", svg, id="tiles")
    for i, t in enumerate(tiles):
        tile(row, i, ix + i * (tw + gap), ty, tw, th, *t)
    activity(svg, ix, by, cw, bh, s["days"])
    languages(svg, ix + cw + gap, by, lw, bh, s["langs"])
    return svg


def to_svg(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    for t in root.iter(f"{{{SVG_NS}}}tspan"):
        t.tail = None  # indent() would put the closing </text> on its own line
    return ET.tostring(root, encoding="unicode") + "\n"


# ---------------------------------------------------------------------- main
def wellformed(svg: str) -> None:
    """Refuse to ship a broken image (e.g. a control character in a language name is invalid XML).
    We only ever parse our own output, which cannot contain a DOCTYPE/ENTITY (asserted anyway),
    so XXE/billion-laughs do not apply."""
    assert "<!DOCTYPE" not in svg and "<!ENTITY" not in svg
    ET.fromstring(svg)


def selftest() -> None:
    d = lambda n: date(2026, 1, n)
    mk = lambda counts: [(d(i + 1), c) for i, c in enumerate(counts)]
    # (current, longest): today has 0 -> streak carries from yesterday; a longer earlier run wins "longest".
    assert streaks(mk([1, 1, 1, 0, 2, 3, 0]))[0][0] == 2
    assert streaks(mk([1, 1, 1, 0, 2, 3, 0]))[1] == (3, d(1), d(3))
    assert streaks(mk([0, 0, 0]))[0][0] == 0 and streaks(mk([0, 0, 0]))[1][0] == 0
    assert streaks(mk([5]))[0] == (1, d(1), d(1)) and streaks(mk([4, 4, 4]))[0] == (3, d(1), d(3))
    repos = [{"stargazerCount": 2, "languages": {"totalSize": 100, "edges": [{"size": 99, "node": {"name": "HTML"}}, {"size": 1, "node": {"name": "Python"}}]}},
             {"stargazerCount": 1, "languages": {"totalSize": 100, "edges": [{"size": 80, "node": {"name": "Python"}}, {"size": 20, "node": {"name": "TypeScript"}}]}},
             {"stargazerCount": 0, "languages": {"totalSize": 0, "edges": []}}]
    assert dict(language_share(repos)) == {"HTML": 50, "Python": 50, "TypeScript": 50}  # empty repo skipped, 1% Python ignored
    days = [(date(2026, 1, 1) + timedelta(days=i), (i * 7) % 11) for i in range(60)]
    user = {"repositories": {"totalCount": 24, "nodes": repos},
            "contributionsCollection": {"contributionCalendar": {"totalContributions": 123456, "weeks": [{"contributionDays": [
                {"date": a.isoformat(), "contributionCount": n} for a, n in days]}]}}}
    s = shape(user, date(2026, 3, 1))
    assert s["stars"] == 3 and s["active"] == sum(1 for _, n in days if n)

    root = render(s)
    svg = to_svg(root)
    wellformed(svg)
    assert "<script" not in svg and "http://" not in svg.replace(SVG_NS, "")
    # UI: same 16px floor as the GIF panels; complete without motion; no endless blinking.
    assert min(float(e.get("font-size", BASE)) for e in root.iter() if e.tag.endswith(("text", "tspan"))) >= BASE
    assert "prefers-reduced-motion" in svg and "infinite" not in svg
    # Clean source: one element per line, readable widths, no default attributes, no "52.0".
    lines = svg.splitlines()
    assert all(len(re.findall(r"<(?!tspan)[A-Za-z]", ln)) <= 1 for ln in lines)   # an inline <tspan> may share its <text>'s line
    assert max(len(ln) for ln in lines if " d=" not in ln) <= 140            # path data is the only long line
    assert not re.search(r'text-anchor="start"|stroke="none"|\d\.0\b', svg)
    # Nothing dangling: unique ids, every def used and every url(#..) resolves, every class has a rule.
    ids = [e.get("id") for e in root.iter() if e.get("id")]
    assert len(ids) == len(set(ids))
    assert {e.get("id") for e in root.find(f"{{{SVG_NS}}}defs")} == set(re.findall(r"url\(#(\w+)\)", svg))
    css = root.find(f"{{{SVG_NS}}}style").text
    assert all(f".{c}" in css for e in root.iter() for c in e.get("class", "").split())
    hostile = to_svg(render(s | {"langs": [('<img onerror=x> & "q"', 50)]}))  # untrusted text is escaped, still well-formed
    assert "<img" not in hostile
    wellformed(hostile)
    print("selftest ok")


def main() -> int:
    if "--selftest" in sys.argv:
        selftest()
        return 0
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    s = shape(graphql(token, USER), datetime.now(timezone.utc).date())
    svg = to_svg(render(s))
    wellformed(svg)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    changed = not OUT.exists() or OUT.read_text(encoding="utf-8") != svg
    if changed:
        OUT.write_text(svg, encoding="utf-8")
    print(f"{'updated' if changed else 'unchanged'} {OUT} | contributions={s['total']} active_days={s['active']} "
          f"best_day={s['best_day'][1]} repos={s['repos']} stars={s['stars']} current_streak={s['cur'][0]} "
          f"longest_streak={s['best'][0]} langs={s['langs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
