"""ChelScout minicard -- a branded PNG scouting card, drawn with Pillow.

No browser, no headless Chrome: everything here is rectangles, bars and text,
which renders in tens of milliseconds and adds a few MB to the image rather
than a few hundred. Fonts come from the font-roboto wheel so the container
never has to have system fonts installed.

Design notes, because they are load-bearing rather than taste:

* Bars are DIVERGING from a 50th-percentile midline, not filled left-to-right.
  A percentile's meaning is polarity -- above or below a typical player -- so
  the baseline belongs at the middle. "Is he better or worse than average?" is
  then answered by which side the bar sits on, before reading any number.
* Colour encodes that polarity and nothing else, in two validated hues plus a
  neutral midpoint. Bar length already carries magnitude, so colouring by value
  would spend the colour channel re-encoding what length shows.
* Categories are ordered by what matters AT HIS POSITION -- a centre is judged
  on scoring first, a defenceman on impact and physicality, a goalie on save
  percentage. See ROWS_BY_POS.
* Scoring and playmaking are separate rows. "Can he finish" and "can he set up"
  are different questions and a combined points rate hides which one he is.

Percentiles come from pool.json, built offline by build_pool.py and ranked
within a player's own position -- see that file for why.
"""
import bisect
import io
import json
import os

from PIL import Image, ImageDraw, ImageFont

import ea

# ---------------------------------------------------------------- palette
# Diverging poles, validated against the dark surface for the OKLCH lightness
# band, chroma floor, CVD separation (protanopia/deuteranopia, Machado 2009 at
# severity 1.0) and contrast. Do not hand-tweak these without re-validating:
#   #3B8EF5  L=0.648  C=0.173  5.78:1  |  #E5484D  L=0.626  C=0.193  4.87:1
#   pair: normal dE 33.0, protan 27.7, deutan 25.0  (target >= 8)
BG = (14, 16, 20)
PANEL = (22, 25, 31)
LINE = (40, 44, 54)
TEXT = (240, 242, 246)
MUTED = (138, 143, 158)
DIM = (92, 97, 112)
BLUE = (59, 142, 245)      # above average
RED = (229, 72, 77)        # below average
NEUTRAL = (110, 115, 130)  # at the midpoint

W = 1080
PAD = 60
MID_TOL = 4  # percentile points either side of 50 that count as "average"

_pool = None

# Which rows to show, in order of what actually matters at that position.
# Centres and wingers are judged on offence first; defencemen on the results
# and the physical game; goalies on an entirely different set.
ROWS_BY_POS = {
    "C":  ["scoring", "playmaking", "impact", "physicality", "discipline"],
    "LW": ["scoring", "playmaking", "impact", "physicality", "discipline"],
    "RW": ["scoring", "playmaking", "impact", "physicality", "discipline"],
    "D":  ["impact", "physicality", "playmaking", "scoring", "discipline"],
    "G":  ["savepct", "gaa", "workload", "shutouts"],
}

LABELS = {
    "scoring": "SCORING", "playmaking": "PLAYMAKING", "production": "PRODUCTION",
    "physicality": "PHYSICALITY", "discipline": "DISCIPLINE", "impact": "PLUS/MINUS",
    "savepct": "SAVE %", "gaa": "GOALS AGAINST", "workload": "WORKLOAD",
    "shutouts": "SHUTOUTS",
}

# Shown to the right of each bar, so a reader sees the raw rate, not only a rank.
def _fmt(metric: str, v: float) -> str:
    if metric == "savepct":
        return f"{v:.3f}".lstrip("0")
    if metric in ("gaa",):
        return f"{v:.2f}"
    if metric == "shutouts":
        return f"{v * 100:.0f}%"
    return f"{v:.2f}"


def pool() -> dict:
    global _pool
    if _pool is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.json")
        try:
            with open(path) as f:
                _pool = json.load(f)
        except Exception:
            _pool = {"breakpoints": {}, "counts": {}}
    return _pool


def _font(name: str, size: int):
    # The wheel exposes each weight as a path constant; plain regular is
    # "Roboto", not "RobotoRegular".
    from font_roboto import Roboto, RobotoBlack, RobotoBold, RobotoMedium
    paths = {"black": RobotoBlack, "bold": RobotoBold,
             "medium": RobotoMedium, "regular": Roboto}
    return ImageFont.truetype(paths[name], size)


MIN_POOL_N = 150
FORWARDS = ("C", "LW", "RW")


def _breakpoints(pos: str, metric: str):
    """Breakpoints for this position, falling back to all forwards if thin."""
    p = pool()
    bp = p.get("breakpoints", {}).get(pos, {}).get(metric)
    n = p.get("counts", {}).get(pos, {}).get(metric, 0)
    if bp and n >= MIN_POOL_N:
        return bp, pos
    if pos in FORWARDS:
        alt = p.get("breakpoints", {}).get("F", {}).get(metric)
        if alt and p.get("counts", {}).get("F", {}).get(metric, 0) >= MIN_POOL_N:
            return alt, "F"
    return bp, pos


def percentile(pos: str, metric: str, value: float) -> int | None:
    """Where this rate lands among players who mainly play the same position.

    Discipline and GAA are inverted: fewer penalty minutes and a lower goals
    against average are better, so a low raw value has to score high.
    """
    bp, _ = _breakpoints(pos, metric)
    if not bp:
        return None
    p = max(0, min(100, bisect.bisect_left(bp, value)))
    return 100 - p if metric in ("discipline", "gaa") else p


def _pole(p: int) -> tuple:
    if p >= 50 + MID_TOL:
        return BLUE
    if p <= 50 - MID_TOL:
        return RED
    return NEUTRAL


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _text(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _positions(m: dict) -> list[tuple[str, int]]:
    out = [(pos, int(ea._num(m.get(key)))) for key, pos in ea.POSITIONS if ea._num(m.get(key)) > 0]
    return sorted(out, key=lambda t: -t[1])


def _rates(m: dict) -> dict:
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    r = {}
    if skater_gp:
        goals, assists = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
        r.update(scoring=goals / skater_gp, playmaking=assists / skater_gp,
                 production=(goals + assists) / skater_gp,
                 physicality=ea._num(m.get("skhits")) / skater_gp,
                 discipline=ea._num(m.get("skpim")) / skater_gp,
                 impact=ea._num(m.get("skplusmin")) / skater_gp)
    if glgp:
        r.update(savepct=ea._savepct(m), gaa=ea._num(m.get("glgaa")),
                 workload=ea._num(m.get("glsaves")) / glgp,
                 shutouts=ea._num(m.get("glso")) / glgp)
    return r


def _verdict(primary: str, rows: list, is_goalie: bool) -> str:
    """One line at the top saying what he is -- the thing a scout needs first."""
    ranked = [(lbl, p) for lbl, _, _, p in rows if p is not None]
    if not ranked:
        return "NOT ENOUGH DATA"
    best = max(ranked, key=lambda t: t[1])
    worst = min(ranked, key=lambda t: t[1])
    if best[1] < 40:
        return f"BELOW AVERAGE {primary} ACROSS THE BOARD"
    if worst[1] >= 60:
        return f"STRONG {primary} EVERYWHERE"
    return f"{best[0]} {primary}  ·  WEAK {worst[0]}"


def render(m: dict, read: str | None = None) -> bytes:
    """Draw the card for one player. `read` is optional prose under the stats."""
    name = str(m.get("name") or "unknown")
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    goals, assists = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
    points = goals + assists
    posns = _positions(m)
    primary = posns[0][0] if posns else "?"
    is_goalie = primary == "G"
    rates = _rates(m)

    f_brand = _font("black", 31)
    f_brandsub = _font("medium", 17)
    f_kicker = _font("bold", 17)
    f_name = _font("black", 64)
    f_verdict = _font("black", 27)
    f_sub = _font("medium", 21)
    f_pos = _font("black", 20)
    f_posn = _font("medium", 17)
    f_stat = _font("black", 46)
    f_statlbl = _font("bold", 16)
    f_bar = _font("bold", 20)
    f_val = _font("bold", 19)
    f_note = _font("medium", 16)
    f_read = _font("regular", 22)
    f_foot = _font("bold", 17)

    metrics = ROWS_BY_POS.get(primary, ROWS_BY_POS["C"])
    rows = []
    for key in metrics:
        if key not in rates:
            continue
        rows.append((LABELS[key], key, rates[key], percentile(primary, key, rates[key])))

    verdict = _verdict(primary, rows, is_goalie)

    read_lines: list[str] = []
    if read:
        tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        maxw = W - 2 * PAD - 36
        line = ""
        for wd in read.split():
            trial = f"{line} {wd}".strip()
            if tmp.textlength(trial, font=f_read) <= maxw:
                line = trial
            else:
                read_lines.append(line)
                line = wd
        if line:
            read_lines.append(line)
        read_lines = read_lines[:5]

    H = (150            # brand
         + 78           # name
         + 46           # verdict
         + 74           # position strip
         + 132          # stat tiles
         + 62           # bars header + legend
         + len(rows) * 52
         + (len(read_lines) * 32 + 40 if read_lines else 0)
         + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)

    # ---- brand
    y = 44
    _text(d, (PAD, y), "Chel", f_brand, TEXT)
    bw = d.textlength("Chel", font=f_brand)
    _text(d, (PAD + bw, y), "Scout", f_brand, BLUE)
    bw += d.textlength("Scout", font=f_brand)
    _text(d, (PAD + bw + 4, y + 14), ".net", f_brandsub, MUTED)
    _text(d, (W - PAD, y + 9), "PUBS SCOUTING REPORT", f_kicker, DIM, anchor="ra")

    # ---- name
    y += 62
    _text(d, (PAD, y), name[:17], f_name, TEXT)
    _text(d, (W - PAD, y + 30), f"{gp:.0f} GAMES", f_sub, MUTED, anchor="ra")

    # ---- verdict: the headline read, before any number
    y += 80
    _text(d, (PAD, y), verdict[:46], f_verdict, BLUE)

    # ---- position strip: share of games, primary called out explicitly
    y += 50
    _text(d, (PAD, y), "POSITIONS", f_statlbl, DIM)
    y += 26
    total = sum(n for _, n in posns) or 1
    x = PAD
    barw = W - 2 * PAD
    for i, (pos, n) in enumerate(posns):
        seg = barw * n / total
        # 3px of the segment goes to the gap between fills, so anything
        # narrower than that has no width left to draw.
        if seg < 6:
            x += seg
            continue
        col = BLUE if i == 0 else (PANEL if i > 1 else (46, 58, 78))
        d.rounded_rectangle([x, y, x + seg - 3, y + 30], radius=5, fill=col)
        if seg > 78:
            _text(d, (x + 12, y + 6), pos, f_pos, TEXT if i == 0 else MUTED)
            _text(d, (x + 12 + d.textlength(pos, font=f_pos) + 8, y + 9),
                  f"{n}", f_posn, TEXT if i == 0 else DIM)
        x += seg
    y += 42
    _text(d, (PAD, y), f"MAIN POSITION: {primary}", f_note, MUTED)

    # ---- headline tiles
    y += 34
    tiles = ([("SV%", _fmt("savepct", rates.get("savepct", 0))),
              ("GAA", _fmt("gaa", rates.get("gaa", 0))),
              ("GP", f"{glgp:.0f}"), ("SO", f"{ea._num(m.get('glso')):.0f}")]
             if is_goalie else
             [("PTS", f"{points:.0f}"), ("G", f"{goals:.0f}"), ("A", f"{assists:.0f}"),
              ("P/GP", f"{points / skater_gp:.2f}" if skater_gp else "-")])
    tw = (W - 2 * PAD) / len(tiles)
    _rr = d.rounded_rectangle
    _rr([PAD, y, W - PAD, y + 110], radius=14, fill=PANEL)
    for i, (lbl, val) in enumerate(tiles):
        cx = PAD + tw * i + tw / 2
        if i:
            d.line([PAD + tw * i, y + 20, PAD + tw * i, y + 90], fill=LINE)
        _text(d, (cx, y + 42), val, f_stat, TEXT, anchor="mm")
        _text(d, (cx, y + 84), lbl, f_statlbl, MUTED, anchor="mm")
    y += 146

    # ---- diverging percentile bars
    ref = _breakpoints(primary, rows[0][1])[1] if rows else primary
    n_pool = max(pool().get("counts", {}).get(ref, {}).values() or [0])
    ref_name = "FORWARDS" if ref == "F" else primary
    _text(d, (PAD, y), f"RANKED VS {n_pool} {ref_name} WITH 50+ GAMES", f_statlbl, DIM)
    y += 24
    _text(d, (PAD, y), "centre line = average player · right is better", f_note, DIM)
    y += 30

    x0, x1 = PAD + 210, W - PAD - 118
    mid = (x0 + x1) / 2
    bars_top = y + 10
    for lbl, metric, value, p in rows:
        _text(d, (PAD, y + 8), lbl, f_bar, TEXT)
        d.rounded_rectangle([x0, y + 12, x1, y + 30], radius=4, fill=(28, 32, 40))
        if p is not None:
            col = _pole(p)
            half = (x1 - x0) / 2
            off = half * (p - 50) / 50
            if abs(p - 50) <= MID_TOL:
                d.rounded_rectangle([mid - 3, y + 12, mid + 3, y + 30], radius=3, fill=col)
            elif off > 0:
                d.rounded_rectangle([mid, y + 12, mid + off, y + 30], radius=4, fill=col)
            else:
                d.rounded_rectangle([mid + off, y + 12, mid, y + 30], radius=4, fill=col)
            _text(d, (W - PAD, y + 8), _ordinal(p), f_val, TEXT, anchor="ra")
        else:
            _text(d, (W - PAD, y + 8), "n/a", f_val, DIM, anchor="ra")
        # raw rate, so the card shows the actual number and not only a rank
        _text(d, (x0 - 16, y + 9), _fmt(metric, value), f_val, MUTED, anchor="ra")
        y += 52
    # One reference line spanning every row, drawn last so it reads as a scale
    # rather than as part of any single bar.
    d.line([mid, bars_top, mid, y - 16], fill=(150, 156, 172), width=2)

    # ---- the read
    if read_lines:
        y += 14
        top = y
        for ln in read_lines:
            _text(d, (PAD + 20, y), ln, f_read, (208, 212, 222))
            y += 32
        d.rectangle([PAD, top - 2, PAD + 3, y - 8], fill=BLUE)

    # ---- footer
    y = H - 56
    d.line([PAD, y - 20, W - PAD, y - 20], fill=LINE)
    _text(d, (PAD, y), "chelscout.net", f_foot, BLUE)
    _text(d, (W - PAD, y), "SCOUT SMARTER. CHIRP RESPONSIBLY.", f_foot, DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
