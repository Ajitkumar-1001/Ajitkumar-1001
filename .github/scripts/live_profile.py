#!/usr/bin/env python3
"""Render assets/live/telemetry.svg from the GitHub GraphQL API. Stdlib only.

Replaces third-party stat cards (they 402, move hosts, and GitHub's image proxy
serves them up to 24h stale) with an SVG built here, from the same API, on a
schedule. Every figure is the *public* view: the contribution calendar as visitors
see it plus public repos. Actions' token sees exactly that (its streak, active days
and total match github.com/<user>'s own calendar), and nothing about private repos
is read or published.

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
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

USER = "Ajitkumar-1001"
OUT = Path("assets/live/telemetry.svg")

# Black-Architect tokens, same as the hand-made GIF panels. Type scale matches them too:
# nothing below 16px (asserted in --selftest).
BG, PANEL, BORDER = "#000000", "#050b16", "#0b1f3a"
ACCENT, TEXT, MUTED, BLUE, MID = "#7dbbff", "#e6eefc", "#9fb3d1", "#1e90ff", "#2f6fc4"
FONT = 'ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono","Liberation Mono",monospace'
CW = 0.62  # em per glyph. Real monospace fonts are 0.60-0.602, so fitted text never overflows.
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


# -------------------------------------------------------------------- render
def esc(s: object) -> str:
    return escape(str(s), quote=True)


def fit(s: str, size: float, max_w: float) -> float:
    return min(size, max_w / (CW * len(s))) if s else size


def txt(x, y, s, size, fill=TEXT, anchor="start", extra="") -> str:
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" fill="{fill}" text-anchor="{anchor}" {extra}>{esc(s)}</text>'


def hud(x, y, w, h) -> str:
    t = 10  # tick length
    d = "".join(f"M{px},{py + sy * t}V{py}H{px + sx * t}" for px, py, sx, sy in
                [(x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)])
    return f'<path d="{d}" fill="none" stroke="{ACCENT}" stroke-width="2"/>'


def card(x, y, w, h, r=14) -> str:
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{PANEL}" stroke="{BORDER}" stroke-width="2"/>'


def fmt_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def tile(i: int, x: int, y: int, w: int, h: int, label: str, value: str, unit: str, sub: str) -> str:
    inner = w - 28
    unit_w = (len(unit) + 1) * CW * 16 if unit else 0
    size = min(46, (inner - unit_w) / (CW * len(value)))     # number shrinks before anything overflows
    unit_txt = txt(x + 14 + len(value) * CW * size + 8, y + 78, unit, 16, MUTED) if unit else ""
    return (
        f'<g class="rise" style="animation-delay:{0.06 * i:.2f}s">{card(x, y, w, h, 12)}'
        + txt(x + 14, y + 30, label, fit(label, 16, inner), MUTED)
        + txt(x + 14, y + 78, value, size, TEXT, extra='font-weight="700" filter="url(#glow)"')
        + unit_txt
        + txt(x + 14, y + 101, sub, fit(sub, 16, inner), ACCENT)
        + "</g>"
    )


def activity_card(x: int, y: int, w: int, h: int, days: list[tuple[date, int]]) -> str:
    last = days[-30:]
    vals = [n for _, n in last]
    peak = max(vals)
    px0, px1, py0, py1 = x + 26, x + w - 26, y + 66, y + h - 44
    step = (px1 - px0) / (len(vals) - 1)
    pts = [(px0 + i * step, py1 - (v / (peak or 1)) * (py1 - py0)) for i, v in enumerate(vals)]
    line = "M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    area = f"{line}L{pts[-1][0]:.1f},{py1}L{pts[0][0]:.1f},{py1}Z"
    grid = "".join(f'<line x1="{px0}" x2="{px1}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{BORDER}" stroke-width="1.5"/>'
                   for gy in (py0, (py0 + py1) / 2, py1))
    return (
        f'<g class="rise" style="animation-delay:.4s">{card(x, y, w, h)}'
        + txt(x + 26, y + 34, "ACTIVITY / LAST 30 DAYS", 16, ACCENT)
        + txt(x + w - 26, y + 34, f"PEAK {peak}", 16, MUTED, "end")
        + grid
        + f'<path d="{area}" fill="url(#area)" stroke="none"/>'
        f'<path class="draw" pathLength="1" d="{line}" fill="none" stroke="{BLUE}" stroke-width="3" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle class="live" cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="5" fill="{ACCENT}"/>'
        + txt(px0, y + h - 14, fmt_day(last[0][0]), 16, MUTED)
        + txt(px1, y + h - 14, fmt_day(last[-1][0]), 16, MUTED, "end")
        + "</g>"
    )


def language_card(x: int, y: int, w: int, h: int, langs: list[tuple[str, int]]) -> str:
    name_w, pct_w = 170, 52
    bx, bw = x + 24 + name_w, w - 48 - name_w - pct_w - 8
    rows = ""
    for i, (name, pct) in enumerate(langs):
        ry = y + 84 + i * 34
        name = name if len(name) <= 17 else name[:16] + "…"  # cap at the name column instead of shrinking the font
        rows += (
            txt(x + 24, ry, name, fit(name, 16, name_w - 6), TEXT)
            + f'<rect x="{bx}" y="{ry - 11}" width="{bw}" height="10" rx="5" fill="{BORDER}"/>'
            f'<rect class="bar" style="animation-delay:{0.55 + 0.08 * i:.2f}s" x="{bx}" y="{ry - 11}" '
            f'width="{max(bw * pct / 100, 4):.1f}" height="10" rx="5" fill="url(#bar)"/>'
            + txt(x + w - 24, ry, f"{pct}%", 16, MUTED, "end")
        )
    return (
        f'<g class="rise" style="animation-delay:.5s">{card(x, y, w, h)}'
        + txt(x + 24, y + 34, "LANGUAGES / % OF REPOS", 16, ACCENT)
        + (rows or txt(x + 24, y + 84, "no languages detected yet", 16, MUTED)) + "</g>"
    )


def render(s: dict) -> str:
    # Labels and subtitles stay <= 14 chars: the most that fits a 6-across tile at 16px.
    tiles = [
        ("CONTRIBUTIONS", f"{s['total']:,}", "", "last 12 months"),
        ("CURRENT STREAK", f"{s['cur'][0]}", "days", f"since {fmt_day(s['cur'][1])}" if s["cur"][0] else "no streak"),
        ("LONGEST STREAK", f"{s['best'][0]}", "days", f"{fmt_day(s['best'][1])}–{fmt_day(s['best'][2])}" if s["best"][0] else ""),
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
    body = "".join(tile(i, ix + i * (tw + gap), ty, tw, th, *t) for i, t in enumerate(tiles))
    body += activity_card(ix, by, cw, bh, s["days"]) + language_card(ix + cw + gap, by, lw, bh, s["langs"])
    py, ph = 90, by + bh + 22 - 90
    stamp = f"SYNC: {s['today'].isoformat()}"
    stamp_w = len(stamp) * CW * 16
    title = f"Live GitHub telemetry for {USER}"
    # The chart and bars have no other text alternative, so the figures live here as well.
    desc = (f"{s['total']} contributions in the last 12 months, current streak {s['cur'][0]} days, longest streak {s['best'][0]} days, "
            f"active on {s['active']} days, busiest day {s['best_day'][1]}. {s['repos']} original public repos, {s['stars']} stars. "
            f"Top languages by repo: {', '.join(f'{n} {p}%' for n, p in s['langs']) or 'none'}.")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
<title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>
<defs>
<linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{ACCENT}" stop-opacity=".35"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></linearGradient>
<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{MID}"/><stop offset="1" stop-color="{ACCENT}"/></linearGradient>
<filter id="glow" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<style>
text{{font-family:{FONT}}}
@keyframes rise{{from{{opacity:0;transform:translateY(8px)}}}}
@keyframes draw{{from{{stroke-dashoffset:1}}}}
@keyframes grow{{from{{transform:scaleX(0)}}}}
@keyframes pulse{{50%{{opacity:.3}}}}
.rise{{animation:rise .4s ease-out backwards}}
.draw{{stroke-dasharray:1;animation:draw 1s ease-out .4s backwards}}
.bar{{transform-box:fill-box;transform-origin:left center;animation:grow .6s ease-out backwards}}
.live{{animation:pulse 2s ease-in-out 2}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style>
<rect width="{W}" height="{H}" fill="{BG}"/>
<text x="{PAD}" y="40" font-size="22" fill="{TEXT}">Live Telemetry<tspan font-size="16" fill="{MUTED}" dx="14">/ last 12 months</tspan></text>
<circle class="live" cx="{W - PAD - stamp_w - 14:.1f}" cy="34" r="4" fill="{ACCENT}"/>
{txt(W - PAD, 40, stamp, 16, MUTED, "end")}
<line x1="{PAD}" x2="{W - PAD}" y1="60" y2="60" stroke="{BORDER}" stroke-width="2"/>
<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="18" fill="{PANEL}" stroke="{BORDER}" stroke-width="2"/>
{hud(px + 10, py + 10, pw - 20, ph - 20)}
{body}
</svg>
"""


# ---------------------------------------------------------------------- main
def wellformed(svg: str) -> None:
    """Refuse to ship a broken image. We only ever parse our own output: untrusted text is
    escaped, so no DOCTYPE/ENTITY can appear (asserted anyway), which rules out XXE/billion-laughs."""
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
    svg = render(s)
    wellformed(svg)
    assert "<script" not in svg and "http://" not in svg.replace("http://www.w3.org/2000/svg", "")
    assert min(float(v) for v in re.findall(r'font-size="([\d.]+)"', svg)) >= 16   # same floor as the GIF panels
    assert "prefers-reduced-motion" in svg and "infinite" not in svg                # complete without motion, no endless blinking
    wellformed(render(s | {"langs": [('<img onerror=x> & "q"', 50)]}))            # untrusted text is escaped, still well-formed
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
    svg = render(s)
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
