#!/usr/bin/env python3
"""Generate the README architecture diagrams in an Excalidraw-style look.

The reference diagram was made with **Excalidraw** (excalidraw.com) — a
whiteboard that draws with rough.js "sketchy" strokes, a hand-drawn font
(Virgil/Excalifont) and drag-in tech logos, on a dark canvas. We can't run
Excalidraw headless, so this reproduces the same aesthetic with pure,
self-contained SVG:

  • hand-drawn font   → Patrick Hand (OFL), subset + embedded as base64
  • irregular borders → every rect/arrow is drawn with jittered points
  • dark background   → charcoal canvas, light strokes
  • tech logos        → inline vector Python / Telegram / Sheets / Claude / …

    python3 docs/diagrams/gen_diagrams.py

Writes docs/diagrams/architecture.svg and docs/diagrams/pipeline.svg. The
font + its licence live in docs/diagrams/fonts/ so this is reproducible.
"""
from __future__ import annotations

import base64
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# --- palette (matches the reference: dark canvas, green apps, amber deps) ---
BG = "#12141a"
INK = "#e7e2d5"          # off-white "handwriting"
MUTED = "#9099ad"
GREEN = "#5bbf72"        # Briefer's own apps
GREEN_FILL = "#15321d"
AMBER = "#d8a13a"        # external / 3rd-party
AMBER_FILL = "#332810"
BLUE = "#5b9bd8"
BLUE_FILL = "#152840"
LINE = "#3a4150"

FONT = "'Patrick Hand','Segoe Print','Comic Sans MS',ui-rounded,cursive"


def _font_face() -> str:
    path = os.path.join(HERE, "fonts", "PatrickHand-subset.woff2")
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return ("@font-face{font-family:'Patrick Hand';font-style:normal;"
            "font-weight:400;src:url(data:font/woff2;base64," + b64 +
            ") format('woff2');}")


# ---------------------------------------------------------------- primitives
def _rng(seed):
    state = {"s": seed * 9301 + 49297}
    def r():
        state["s"] = (state["s"] * 9301 + 49297) % 233280
        return state["s"] / 233280
    return r


def _rough_rect(x, y, w, h, r=16, seed=1, jitter=1.1):
    """A rounded rect as a gently-wobbly *closed* path — hand-drawn but with no
    torn corners. Each corner shares one jittered anchor so edges always meet."""
    rng = _rng(seed)
    def j():
        return (rng() - 0.5) * 2 * jitter
    # one jittered anchor per corner side, reused so the path closes cleanly
    tl = (x + r + j(), y + j())          # top-left (after radius)
    tr = (x + w - r + j(), y + j())      # top-right (before radius)
    rt = (x + w + j(), y + r + j())      # right-top
    rb = (x + w + j(), y + h - r + j())  # right-bottom
    br = (x + w - r + j(), y + h + j())  # bottom-right
    bl = (x + r + j(), y + h + j())      # bottom-left
    lb = (x + j(), y + h - r + j())      # left-bottom
    lt = (x + j(), y + r + j())          # left-top
    d = (f"M {tl[0]:.1f} {tl[1]:.1f} "
         f"L {tr[0]:.1f} {tr[1]:.1f} "
         f"Q {x+w:.1f} {y:.1f} {rt[0]:.1f} {rt[1]:.1f} "
         f"L {rb[0]:.1f} {rb[1]:.1f} "
         f"Q {x+w:.1f} {y+h:.1f} {br[0]:.1f} {br[1]:.1f} "
         f"L {bl[0]:.1f} {bl[1]:.1f} "
         f"Q {x:.1f} {y+h:.1f} {lb[0]:.1f} {lb[1]:.1f} "
         f"L {lt[0]:.1f} {lt[1]:.1f} "
         f"Q {x:.1f} {y:.1f} {tl[0]:.1f} {tl[1]:.1f} Z")
    return d


def box(x, y, w, h, label, sub="", *, stroke=GREEN, fill=GREEN_FILL,
        logo="", dashed=False, seed=1, label_dy=0):
    dash = 'stroke-dasharray="8 6" ' if dashed else ""
    p1 = _rough_rect(x, y, w, h, seed=seed, jitter=1.1)
    cx = x + w / 2
    label_y = y + h / 2 + (5 if not sub else -4) + label_dy
    out = [
        f'<path d="{p1}" fill="{fill}" stroke="{stroke}" stroke-width="2.6" '
        f'stroke-linejoin="round" stroke-linecap="round" {dash}/>',
        f'<text x="{cx:.0f}" y="{label_y:.0f}" fill="{INK}" '
        f'font-size="20" text-anchor="middle">{label}</text>',
    ]
    if sub:
        out.append(
            f'<text x="{cx:.0f}" y="{y+h/2+16:.0f}" fill="{MUTED}" '
            f'font-size="14.5" text-anchor="middle">{sub}</text>')
    if logo:
        out.append(LOGOS[logo](x + w - 32, y + 9))
    return "\n".join(out)


def arrow(x1, y1, x2, y2, *, color="#aab2c5", seed=3, curve=0.0, label=""):
    rng = _rng(seed)
    def j():
        return (rng() - 0.5) * 2.2
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1
    ox, oy = -dy / dist, dx / dist
    cxp, cyp = mx + ox * curve + j(), my + oy * curve + j()
    d = f"M {x1+j():.1f} {y1+j():.1f} Q {cxp:.1f} {cyp:.1f} {x2+j():.1f} {y2+j():.1f}"
    ang = math.atan2(y2 - cyp, x2 - cxp)
    hl = 12
    a1, a2 = ang + math.radians(156), ang - math.radians(156)
    h1 = f"M {x2:.1f} {y2:.1f} L {x2+hl*math.cos(a1):.1f} {y2+hl*math.sin(a1):.1f}"
    h2 = f"M {x2:.1f} {y2:.1f} L {x2+hl*math.cos(a2):.1f} {y2+hl*math.sin(a2):.1f}"
    out = [
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" '
        f'stroke-linecap="round"/>',
        f'<path d="{h1}" stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>',
        f'<path d="{h2}" stroke="{color}" stroke-width="2.2" stroke-linecap="round"/>',
    ]
    if label:
        out.append(f'<text x="{cxp:.0f}" y="{cyp-7:.0f}" fill="{MUTED}" '
                   f'font-size="14" text-anchor="middle">{label}</text>')
    return "\n".join(out)


def text(x, y, s, *, size=17, color=INK, anchor="start"):
    return (f'<text x="{x:.0f}" y="{y:.0f}" fill="{color}" font-size="{size}" '
            f'text-anchor="{anchor}">{s}</text>')


# --------------------------------------------------------------- tech logos
# Small, recognizable inline emblems (like the JS/Python badges in the ref).
def _python(x, y, s=22):
    b, yl = "#4B8BBE", "#FFD43B"
    u = s / 24
    def P(scale, col, rot):
        return (f'<g transform="translate({x},{y}) scale({u}) '
                f'rotate({rot},12,12)">'
                f'<path fill="{col}" d="M12 2c-2.5 0-4 1-4 3v2h5v1H6c-2 0-3 1.3-3 4'
                f's1 4 3 4h1.5v-2.2c0-1.8 1.4-3.3 3.5-3.3h3c1.6 0 3-1.3 3-3V5c0-2-1.5-3-4-3z'
                f'M8.7 4.4a1 1 0 110 2 1 1 0 010-2z"/></g>')
    return P(1, b, 0) + P(1, yl, 180)


def _telegram(x, y, s=22):
    return (f'<g transform="translate({x},{y})">'
            f'<circle cx="{s/2}" cy="{s/2}" r="{s/2}" fill="#2AABEE"/>'
            f'<path fill="#fff" d="M{s*0.22} {s*0.5} L{s*0.8} {s*0.28} '
            f'L{s*0.68} {s*0.76} L{s*0.5} {s*0.6} L{s*0.4} {s*0.7} '
            f'L{s*0.4} {s*0.55} L{s*0.66} {s*0.36} L{s*0.36} {s*0.5} Z"/></g>')


def _sheets(x, y, s=22):
    return (f'<g transform="translate({x},{y})">'
            f'<rect x="1" y="0" width="{s-2}" height="{s}" rx="3" fill="#188038"/>'
            f'<rect x="{s*0.26}" y="{s*0.28}" width="{s*0.5}" height="{s*0.5}" '
            f'fill="#fff"/>'
            f'<line x1="{s*0.26}" y1="{s*0.45}" x2="{s*0.76}" y2="{s*0.45}" '
            f'stroke="#188038" stroke-width="1.2"/>'
            f'<line x1="{s*0.26}" y1="{s*0.61}" x2="{s*0.76}" y2="{s*0.61}" '
            f'stroke="#188038" stroke-width="1.2"/>'
            f'<line x1="{s*0.51}" y1="{s*0.28}" x2="{s*0.51}" y2="{s*0.78}" '
            f'stroke="#188038" stroke-width="1.2"/></g>')


def _claude(x, y, s=22):
    # Anthropic "sunburst" mark
    rays = []
    cx = cy = s / 2
    for k in range(12):
        a = math.radians(k * 30)
        x1, y1 = cx + 2 * math.cos(a), cy + 2 * math.sin(a)
        x2, y2 = cx + (s / 2) * math.cos(a), cy + (s / 2) * math.sin(a)
        rays.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
                    f'y2="{y2:.1f}" stroke="#D97757" stroke-width="2.4" '
                    f'stroke-linecap="round"/>')
    return f'<g transform="translate({x},{y})">' + "".join(rays) + "</g>"


def _docker(x, y, s=22):
    boxes = "".join(
        f'<rect x="{2+c*4.2:.1f}" y="{9-r*4.2:.1f}" width="3.6" height="3.6" '
        f'fill="#2496ED"/>'
        for r in range(2) for c in range(4) if not (r == 1 and c >= 3))
    return (f'<g transform="translate({x},{y})">{boxes}'
            f'<path d="M1 13 h{s-2} a5 5 0 01-5 5 H7 a6 6 0 01-6-5z" '
            f'fill="#2496ED"/></g>')


def _globe(x, y, s=22):
    r = s / 2
    return (f'<g transform="translate({x},{y})">'
            f'<circle cx="{r}" cy="{r}" r="{r-1}" fill="none" stroke="#5b9bd8" '
            f'stroke-width="1.8"/>'
            f'<ellipse cx="{r}" cy="{r}" rx="{r*0.45}" ry="{r-1}" fill="none" '
            f'stroke="#5b9bd8" stroke-width="1.4"/>'
            f'<line x1="1" y1="{r}" x2="{s-1}" y2="{r}" stroke="#5b9bd8" '
            f'stroke-width="1.4"/></g>')


def _db(x, y, s=22):
    return (f'<g transform="translate({x},{y})" fill="none" stroke="#5bbf72" '
            f'stroke-width="1.8">'
            f'<ellipse cx="{s/2}" cy="4" rx="{s/2-1}" ry="3.2" fill="#15321d"/>'
            f'<path d="M1 4 V{s-4} a{s/2-1} 3.2 0 00{s-2} 0 V4"/>'
            f'<path d="M1 {s*0.5} a{s/2-1} 3.2 0 00{s-2} 0"/></g>')


LOGOS = {"python": _python, "telegram": _telegram, "sheets": _sheets,
         "claude": _claude, "docker": _docker, "globe": _globe, "db": _db}


def svg(width, height, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="{FONT}">\n'
        f'<defs><style>{_font_face()}</style></defs>\n'
        f'<rect width="{width}" height="{height}" rx="12" fill="{BG}"/>\n'
        f'{body}\n</svg>\n')


# ----------------------------------------------------------------------------
# Diagram 1 — system architecture (mirrors the reference layout)
# ----------------------------------------------------------------------------
def architecture():
    W, H = 940, 540
    b = []
    # outside actor: Telegram user (the "Browser" of the reference)
    b.append(box(30, 220, 140, 96, "Telegram", sub="you + team",
                 stroke=BLUE, fill=BLUE_FILL, logo="telegram", seed=2))
    b.append(text(208, 258, "forward", size=14, color=MUTED, anchor="middle"))
    b.append(arrow(174, 272, 244, 272, curve=0))

    # container ("Briefer's image" in the reference)
    b.append(f'<path d="{_rough_rect(244, 44, 660, 452, r=24, seed=11, jitter=2.4)}" '
             f'fill="#171a21" stroke="{LINE}" stroke-width="2.4"/>')
    b.append(text(272, 78, "Briefer’s server", size=18, color=MUTED))
    b.append(text(272, 99, "venv · tmux / systemd · self-healing", size=14, color=MUTED))
    b.append('<line x1="588" y1="118" x2="588" y2="470" stroke="%s" '
             'stroke-width="1.8" stroke-dasharray="3 8"/>' % LINE)
    b.append(text(420, 140, "the bot", size=17, color=GREEN, anchor="middle"))
    b.append(text(742, 140, "external services", size=17, color=AMBER, anchor="middle"))

    # left column — the app (green, Python)
    b.append(box(286, 158, 262, 70, "telegram_bot", sub="auth · queue · handlers",
                 logo="python", seed=3))
    b.append(box(286, 256, 262, 70, "pipeline", sub="enrich · analyse · verify",
                 logo="python", seed=4))
    b.append(box(286, 354, 262, 62, "storage", sub="queue · dedup · reminders",
                 logo="db", seed=5))
    b.append(box(286, 430, 262, 50, "media · browser",
                 sub="yt-dlp · whisper  (optional)",
                 logo="python", dashed=True, seed=6, label_dy=-3))

    # right column — external
    b.append(box(620, 158, 250, 70, "Claude API", sub="analyse · verify · vision",
                 stroke=AMBER, fill=AMBER_FILL, logo="claude", seed=7))
    b.append(box(620, 256, 250, 70, "Google Sheets", sub="+ Drive images",
                 stroke=AMBER, fill=AMBER_FILL, logo="sheets", seed=8))
    b.append(box(620, 354, 250, 62, "web search", sub="DuckDuckGo · Brave · fetch",
                 stroke=AMBER, fill=AMBER_FILL, logo="globe", seed=9))
    b.append(box(620, 438, 165, 46, "the web", sub="pages · videos",
                 stroke=BLUE, fill=BLUE_FILL, logo="globe", seed=16))

    # arrows
    b.append(arrow(417, 228, 417, 256, seed=10))
    b.append(arrow(360, 326, 360, 354, seed=11))
    b.append(arrow(470, 326, 470, 430, curve=6, seed=12))
    b.append(arrow(548, 278, 620, 196, curve=-20, seed=13))
    b.append(arrow(548, 290, 620, 290, seed=14))
    b.append(arrow(548, 305, 620, 380, curve=20, seed=15))
    b.append(arrow(548, 456, 620, 460, seed=17))

    # watermark (Docker whale, like the reference) — bottom-right, clear of boxes
    b.append(text(858, 462, "Briefer", size=16, color=MUTED, anchor="end"))
    b.append(_docker(866, 449, 24))
    return svg(W, H, "\n".join(b))


# ----------------------------------------------------------------------------
# Diagram 2 — the pipeline, drawn in the SAME hand-drawn frame/language
# ----------------------------------------------------------------------------
def pipeline():
    W, H = 580, 640
    b = []
    b.append(f'<path d="{_rough_rect(140, 20, 300, 600, r=22, seed=30, jitter=2.4)}" '
             f'fill="#171a21" stroke="{LINE}" stroke-width="2.4"/>')
    b.append(text(W / 2, 52, "One submission,", size=19, color=INK, anchor="middle"))
    b.append(text(W / 2, 74, "step by step", size=19, color=INK, anchor="middle"))

    steps = [
        ("enrich", "fetch · files · media", GREEN, GREEN_FILL, "python"),
        ("classify", "article or event", BLUE, BLUE_FILL, ""),
        ("analyse", "summary + your angle", AMBER, AMBER_FILL, "claude"),
        ("verify", "re-check the facts", AMBER, AMBER_FILL, "claude"),
        ("enrich +", "web-search details", AMBER, AMBER_FILL, "globe"),
        ("write", "sheet + image", GREEN, GREEN_FILL, "sheets"),
        ("notify", "catch + reminders", GREEN, GREEN_FILL, "telegram"),
    ]
    x, w = 165, 250
    y0, gap, bh = 96, 12, 56
    for i, (label, sub, st, fl, logo) in enumerate(steps):
        y = y0 + i * (bh + gap)
        b.append(box(x, y, w, bh, label, sub=sub, stroke=st, fill=fl,
                     logo=logo, seed=31 + i, label_dy=-2))
        if i < len(steps) - 1:
            b.append(arrow(x + w / 2, y + bh + 1, x + w / 2, y + bh + gap - 1,
                           seed=50 + i))
    b.append(_db(158, 592, 20))
    b.append(text(186, 608, "queued in SQLite · resumes after a restart",
                  size=13, color=MUTED))
    return svg(W, H, "\n".join(b))


def main():
    for name, fn in (("architecture", architecture), ("pipeline", pipeline)):
        path = os.path.join(HERE, f"{name}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fn())
        print("wrote", path)


if __name__ == "__main__":
    main()
