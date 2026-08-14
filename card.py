"""ChelScout minicard -- a branded PNG scouting card, drawn with Pillow.

No browser, no headless Chrome: everything here is rectangles, bars and text,
which renders in tens of milliseconds and adds a few MB to the image rather
than a few hundred. Fonts come from the font-roboto wheel so the container
never has to have system fonts installed.

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
BG = (11, 11, 14)
PANEL = (19, 19, 24)
LINE = (38, 38, 46)
TEXT = (238, 238, 242)
MUTED = (128, 130, 142)
DIM = (86, 88, 98)
RED = (233, 58, 64)
GREEN = (46, 190, 116)
BLUE = (56, 152, 236)

W = 1000
PAD = 56

_pool = None


def pool() -> dict:
    global _pool
    if _pool is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.json")
        try:
            with open(path) as f:
                _pool = json.load(f)
        except Exception:
            _pool = {"breakpoints": {}}
    return _pool


def _font(name: str, size: int):
    # The wheel exposes each weight as a path constant; plain regular is
    # "Roboto", not "RobotoRegular".
    from font_roboto import Roboto, RobotoBlack, RobotoBold, RobotoMedium
    paths = {"black": RobotoBlack, "bold": RobotoBold,
             "medium": RobotoMedium, "regular": Roboto}
    return ImageFont.truetype(paths[name], size)


# Below this a pool is too small to give an honest percentile -- ranks start
# moving in visible jumps rather than smoothly.
MIN_POOL_N = 150
FORWARDS = ("C", "LW", "RW")


def _breakpoints(pos: str, metric: str):
    """Breakpoints for this position, falling back to all forwards if thin.

    Right wing is a rare primary position, so its own pool stays small however
    hard the sampler runs. Ranking a right winger against every forward is
    coarser than against right wings, but far better than a scale built from
    a few dozen players.
    """
    p = pool()
    bp = p.get("breakpoints", {}).get(pos, {}).get(metric)
    n = p.get("counts", {}).get(pos, {}).get(metric, 0)
    if bp and n >= MIN_POOL_N:
        return bp, pos
    if pos in FORWARDS:
        alt = p.get("breakpoints", {}).get("F", {}).get(metric)
        if alt and p.get("counts", {}).get("F", {}).get(metric, 0) >= MIN_POOL_N:
            return alt, "F"
    return bp, pos  # thin, but the only thing available


def percentile(pos: str, metric: str, value: float) -> int | None:
    """Where this rate lands among players who mainly play the same position.

    Discipline and GAA are inverted: fewer penalty minutes and a lower goals
    against average are better, so a low raw value has to score high.
    """
    bp, _ = _breakpoints(pos, metric)
    if not bp:
        return None
    p = bisect.bisect_left(bp, value)
    p = max(0, min(100, p))
    if metric in ("discipline", "gaa"):
        p = 100 - p
    return p


def _pct_color(p: int) -> tuple:
    if p >= 75:
        return GREEN
    if p >= 45:
        return BLUE
    return RED


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _text(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _rrect(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def _positions(m: dict) -> list[tuple[str, int]]:
    out = [(pos, int(ea._num(m.get(key)))) for key, pos in ea.POSITIONS if ea._num(m.get(key)) > 0]
    return sorted(out, key=lambda t: -t[1])


def render(m: dict, read: str | None = None) -> bytes:
    """Draw the card for one player. `read` is optional prose under the stats."""
    name = str(m.get("name") or "unknown")
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    goals = ea._num(m.get("skgoals"))
    assists = ea._num(m.get("skassists"))
    points = goals + assists
    posns = _positions(m)
    primary = posns[0][0] if posns else "?"
    is_goalie = primary == "G"
    standout = ea.standout_trait(m)

    f_brand = _font("black", 30)
    f_brandsub = _font("medium", 17)
    f_kicker = _font("bold", 18)
    f_name = _font("black", 66)
    f_sub = _font("regular", 24)
    f_chip = _font("bold", 19)
    f_stat = _font("black", 52)
    f_statlbl = _font("bold", 17)
    f_bar = _font("bold", 21)
    f_barval = _font("bold", 20)
    f_read = _font("regular", 23)
    f_foot = _font("bold", 18)

    # rows we will draw, so height can be computed before the canvas exists
    if is_goalie:
        rows = [("SAVE %", "savepct", ea._savepct(m)),
                ("GOALS AGAINST", "gaa", ea._num(m.get("glgaa")))]
    else:
        rows = []
        if skater_gp:
            rows = [
                ("PRODUCTION", "production", points / skater_gp),
                ("PHYSICALITY", "physicality", ea._num(m.get("skhits")) / skater_gp),
                ("DISCIPLINE", "discipline", ea._num(m.get("skpim")) / skater_gp),
                ("IMPACT", "impact", ea._num(m.get("skplusmin")) / skater_gp),
            ]

    read_lines: list[str] = []
    if read:
        f = f_read
        words, line = read.split(), ""
        maxw = W - 2 * PAD - 40
        tmp = Image.new("RGB", (10, 10))
        td = ImageDraw.Draw(tmp)
        for wd in words:
            trial = f"{line} {wd}".strip()
            if td.textlength(trial, font=f) <= maxw:
                line = trial
            else:
                read_lines.append(line)
                line = wd
        if line:
            read_lines.append(line)
        read_lines = read_lines[:6]

    # chip text is needed before the canvas exists, since its presence changes
    # the height. standout_trait() only grades skaters -- goalies get their
    # save-percentage band instead of an empty slot.
    if is_goalie:
        chip = f"{ea._band(ea._savepct(m), ea.GOALIE_SVPCT_BANDS).upper()} SAVE %"
    elif standout:
        chip = f"{standout['grade'].upper()} {standout['trait'].upper()}"
    else:
        chip = None

    H = (196                                     # brand + name + position line
         + (74 if chip else 30)                  # standout chip
         + 158                                   # stat tiles
         + 34 + len(rows) * 54                   # pool header + bars
         + (len(read_lines) * 34 + 56 if read_lines else 0)
         + 104)                                  # divider + footer
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # top accent
    d.rectangle([0, 0, W, 5], fill=RED)

    y = 46
    # ---- brand
    _text(d, (PAD, y), "Chel", f_brand, TEXT)
    bw = d.textlength("Chel", font=f_brand)
    _text(d, (PAD + bw, y), "Scout", f_brand, BLUE)
    bw += d.textlength("Scout", font=f_brand)
    _text(d, (PAD + bw + 4, y + 13), ".net", f_brandsub, MUTED)
    _text(d, (W - PAD, y + 8), "PUBS SCOUTING REPORT", f_kicker, DIM, anchor="ra")

    # ---- name + position
    y += 66
    _text(d, (PAD, y), name[:18], f_name, TEXT)
    nw = d.textlength(name[:18], font=f_name)
    badge = [PAD + nw + 18, y + 16, PAD + nw + 18 + 30 + len(primary) * 14, y + 56]
    _rrect(d, badge, 8, fill=RED)
    _text(d, ((badge[0] + badge[2]) / 2, (badge[1] + badge[3]) / 2), primary, f_chip, TEXT, anchor="mm")

    y += 84
    split = " · ".join(f"{p} {n}" for p, n in posns[:4]) or "no position data"
    _text(d, (PAD, y), f"{split}  ·  {gp:.0f} GP", f_sub, MUTED)

    # ---- standout chip
    y += 46
    if chip:
        cw = d.textlength(chip, font=f_chip) + 44
        _rrect(d, [PAD, y, PAD + cw, y + 44], 22, outline=RED, width=2)
        _text(d, (PAD + cw / 2, y + 22), chip, f_chip, RED, anchor="mm")
        y += 74
    else:
        y += 30

    # ---- stat tiles
    tiles = ([("SV%", f"{ea._savepct(m):.3f}".lstrip("0")), ("GAA", f"{ea._num(m.get('glgaa')):.2f}"),
              ("GP", f"{glgp:.0f}"), ("W", f"{ea._num(m.get('glwins')):.0f}")]
             if is_goalie else
             [("PTS", f"{points:.0f}"), ("G", f"{goals:.0f}"),
              ("A", f"{assists:.0f}"), ("P/GP", f"{points / skater_gp:.2f}" if skater_gp else "-")])
    tw = (W - 2 * PAD) / len(tiles)
    _rrect(d, [PAD, y, W - PAD, y + 118], 14, fill=PANEL)
    for i, (lbl, val) in enumerate(tiles):
        cx = PAD + tw * i + tw / 2
        if i:
            d.line([PAD + tw * i, y + 22, PAD + tw * i, y + 96], fill=LINE)
        _text(d, (cx, y + 44), val, f_stat, TEXT, anchor="mm")
        _text(d, (cx, y + 90), lbl, f_statlbl, MUTED, anchor="mm")
    y += 158

    # ---- percentile bars
    # Name the pool actually used, which may be the forwards fallback -- the
    # card should never claim a comparison it didn't make.
    ref = _breakpoints(primary, rows[0][1])[1] if rows else primary
    n = max(pool().get("counts", {}).get(ref, {}).values() or [0])
    header = (f"VS {'FORWARD' if ref == 'F' else ref} POOL"
              + (f" · {n} PLAYERS" if n else ""))
    _text(d, (PAD, y), header, f_statlbl, DIM)
    y += 34
    for lbl, metric, value in rows:
        p = percentile(primary, metric, value)
        _text(d, (PAD, y + 10), lbl, f_bar, TEXT)
        x0, x1 = PAD + 250, W - PAD - 90
        d.rounded_rectangle([x0, y + 12, x1, y + 26], radius=7, fill=(32, 32, 40))
        if p is not None:
            col = _pct_color(p)
            fillw = x0 + (x1 - x0) * max(p, 2) / 100
            d.rounded_rectangle([x0, y + 12, fillw, y + 26], radius=7, fill=col)
            _text(d, (W - PAD, y + 10), _ordinal(p), f_barval, col, anchor="ra")
        else:
            _text(d, (W - PAD, y + 10), "n/a", f_barval, DIM, anchor="ra")
        y += 54

    # ---- the read
    if read_lines:
        y += 16
        top = y
        for ln in read_lines:
            _text(d, (PAD + 22, y), ln, f_read, (206, 208, 216))
            y += 34
        d.rectangle([PAD, top - 4, PAD + 4, y - 8], fill=RED)
        y += 20

    # ---- footer
    y = H - 62
    d.line([PAD, y - 18, W - PAD, y - 18], fill=LINE)
    _text(d, (PAD, y), "chelscout.net", f_foot, RED)
    _text(d, (W - PAD, y), "SCOUT SMARTER. CHIRP RESPONSIBLY.", f_foot, DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
