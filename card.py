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
import math
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
# Official ChelScout blue, sampled from the logo. #0069FA clears the mark
# threshold on this surface (L .565, C .231, 3.99:1) so it is used for fills;
# small text takes the lighter step, which clears the 4.5:1 text bar at 5.30:1.
BLUE = (0, 105, 250)       # brand fill -- accent only, never a rating
BLUE_TEXT = (43, 132, 255)  # brand blue for type
NAVY = (16, 37, 64)        # logo navy
# Rating scale. Red/green is the classic colour-blind failure, so the green is
# pushed toward teal: that is what carries the pair past the CVD threshold.
# Validated on the dark surface, all pairs above the dE 8 target:
#   #E5484D L=.626 | #C4841F L=.663 | #2A9D8F L=.630
#   red/amber protan 17.8 deutan 12.8 - red/green protan 18.0 deutan 9.7
#   amber/green protan 12.3 deutan 17.9
RED = (229, 72, 77)        # weak, bender, shitter
AMBER = (196, 132, 31)     # mid
GREEN = (42, 157, 143)     # solid, stud, elite
NEUTRAL = (110, 115, 130)

W = 940
PAD = 60
MID_TOL = 4  # percentile points either side of 50 that count as "average"

_pool = None

# Which rows to show, in order of what actually matters at that position.
# Centres and wingers are judged on offence first; defencemen on the results
# and the physical game; goalies on an entirely different set.
# Four rows, not five, and only the ones a decision actually turns on.
# Forwards are judged on whether they produce and whether the puck ends up in
# the right net; physicality is not why anyone picks a winger, so it is off the
# forward card. A defenceman is the opposite case -- how he defends and moves
# the puck is the whole question, and his raw scoring matters least.
ROWS_BY_POS = {
    "C":  ["scoring", "playmaking", "impact", "discipline"],
    "LW": ["scoring", "playmaking", "impact", "discipline"],
    "RW": ["scoring", "playmaking", "impact", "discipline"],
    "D":  ["impact", "physicality", "playmaking", "discipline"],
    "G":  ["savepct", "gaa", "shutouts"],
}

# Used in the compact secondary block, where a long label runs into its value.
SHORT_LABELS = {"savepct": "SV%", "gaa": "GAA", "scoring": "GOALS",
                "playmaking": "ASSISTS", "impact": "+/-"}

LABELS = {
    "scoring": "SCORING", "playmaking": "PLAYMAKING", "production": "PRODUCTION",
    "physicality": "PHYSICALITY", "discipline": "DISCIPLINE", "impact": "PLUS/MINUS",
    "savepct": "SAVE %", "gaa": "GOALS AGAINST", "shutouts": "SHUTOUTS",
}

# Goalie note: saves-per-game ("workload") used to be a graded axis and was
# actively misleading -- it measures how many shots the team in front of him
# allows, so a goalie behind a good defence graded RED while one getting
# shelled graded green. Measured live: a 2.79-GAA goalie scored 10th
# percentile, a 4.81-GAA goalie scored 85th. Shots faced is now printed as a
# context tile instead, and no recalibration was involved -- the direction
# was wrong, not the scale.
#
# The radar draws EVERY graded skill for the position, not just the four the
# bar rows pick out -- a fifth axis is what turns a diamond into a shape. The
# pool has percentile bands for all five skater metrics at every position and
# all four goalie metrics, so nothing here is on a made-up scale. Fixed order:
# adjacent axes are related (the two scoring skills together, the two
# "how he plays" skills together), so the outline reads as a profile.
RADAR_AXES = {
    "skater": ["scoring", "playmaking", "impact", "physicality", "discipline"],
    "G": ["savepct", "gaa", "shutouts"],
}
RADAR_R = 140          # radius of the 100th-percentile ring
RADAR_LABEL_ROOM = 48  # vertical room for the two-line labels above and below


def radar_axes(primary: str, rates: dict, is_goalie: bool) -> list[tuple[str, str, int]]:
    """(label, metric, percentile) per axis, in RADAR_AXES order.

    Goes through the same percentile() call as the bar rows, on the same
    rates dict, so a skill that appears in both places can never carry two
    different numbers. An axis with no band (or a goalie with no recorded
    saves, the same exclusion the bars make) is dropped rather than drawn at
    zero -- a missing grade is not a bad grade.
    """
    out = []
    for key in RADAR_AXES["G" if is_goalie else "skater"]:
        if key not in rates:
            continue
        p = percentile(primary, key, rates[key])
        if p is None:
            continue
        out.append((LABELS[key], key, p))
    return out


def radar_points(cx: float, cy: float, r: float, values: list) -> list[tuple[float, float]]:
    """One vertex per value (0-100): first axis straight up, then clockwise.

    Pure geometry so it can be checked in isolation -- a value of 100 lands
    exactly r from the centre on its axis, 50 lands at r/2, 0 at the centre.
    """
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        a = -math.pi / 2 + 2 * math.pi * i / n
        d = r * max(0, min(100, v)) / 100
        pts.append((cx + d * math.cos(a), cy + d * math.sin(a)))
    return pts


def _radar(img, cx: float, cy: float, r: float, axes: list, f_lbl, f_word) -> list:
    """Draw the shape onto img. Returns the vertex points it drew.

    Rings at 25/50/75/100 with the 50th ring brighter -- that is the "typical
    player" reference, the same one the bars' legend names. The fill is the
    brand blue at low opacity on an RGBA overlay, so the card stays black and
    the outline, not the fill, carries the shape. Each vertex is labelled with
    the metric and the SAME tier word + ordinal the bar rows print, in the
    tier colour, so the reading never depends on judging a distance.
    """
    n = len(axes)
    vals = [p for _, _, p in axes]
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for ring in (25, 50, 75, 100):
        col = (70, 76, 92, 255) if ring == 50 else (LINE[0], LINE[1], LINE[2], 255)
        d.polygon(radar_points(cx, cy, r, [ring] * n), outline=col)
    for x, y in radar_points(cx, cy, r, [100] * n):
        d.line([(cx, cy), (x, y)], fill=(LINE[0], LINE[1], LINE[2], 255), width=1)
    # a 0th-percentile vertex still gets a visible nub so the outline closes
    pts = radar_points(cx, cy, r, [max(v, 3) for v in vals])
    d.polygon(pts, fill=(BLUE[0], BLUE[1], BLUE[2], 95))
    d.line(pts + [pts[0]], fill=(BLUE_TEXT[0], BLUE_TEXT[1], BLUE_TEXT[2], 255),
           width=3, joint="curve")
    for x, y in pts:
        d.ellipse([x - 5, y - 5, x + 5, y + 5],
                  fill=(BLUE_TEXT[0], BLUE_TEXT[1], BLUE_TEXT[2], 255),
                  outline=(BG[0], BG[1], BG[2], 255), width=2)
    img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))

    d = ImageDraw.Draw(img)
    for i, ((lbl, _, p), (x, y)) in enumerate(zip(axes, radar_points(cx, cy, r, [100] * n))):
        a = -math.pi / 2 + 2 * math.pi * i / n
        dx, dy = math.cos(a), math.sin(a)
        lx, ly = x + dx * 34, y + dy * 26
        anchor = "lm" if dx > 0.3 else ("rm" if dx < -0.3 else "mm")
        _text(d, (lx, ly - 11), lbl, f_lbl, MUTED, anchor=anchor)
        _text(d, (lx, ly + 9), f"{tier(p)}  {p}", f_word, _pole(p), anchor=anchor)
    return pts

# Shown to the right of each bar, so a reader sees the raw rate, not only a rank.
def _fmt(metric: str, v: float) -> str:
    if metric == "savepct":
        return f"{v:.3f}".lstrip("0")
    if metric in ("gaa",):
        return f"{v:.2f}"
    if metric == "shutouts":
        return f"{v * 100:.0f}%"
    return f"{v:.2f}"


# Where the live pool lives. On Railway this is a file on a persistent volume
# that the bot rebuilds itself (see bot.py); the pool.json in the repo is the
# fallback for a fresh volume and for local runs.
REPO_POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool.json")
POOL_PATH = os.getenv("POOL_PATH") or REPO_POOL


def pool() -> dict:
    global _pool
    if _pool is None:
        for path in (POOL_PATH, REPO_POOL):
            try:
                with open(path) as f:
                    _pool = json.load(f)
                break
            except Exception:
                continue
        else:
            _pool = {"breakpoints": {}, "counts": {}}
    return _pool


def reload_pool() -> dict:
    """Drop the cached pool so the next card reads the file again."""
    global _pool
    _pool = None
    return pool()


_logo = None


def logo(height: int):
    """The official dark-background lockup, scaled to a given cap height.

    Drawn as the real asset rather than typed out in Roboto -- the wordmark has
    its own letterforms and the mark cannot be reproduced with text at all.
    Falls back to None so a missing asset degrades to the typed wordmark
    instead of failing the whole render.
    """
    global _logo
    if _logo is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "chelscout-dark.png")
        try:
            _logo = Image.open(path).convert("RGBA")
        except Exception:
            _logo = False
    if not _logo:
        return None
    w = round(_logo.width * height / _logo.height)
    return _logo.resize((w, height), Image.LANCZOS)


def _font(name: str, size: int):
    # The wheel exposes each weight as a path constant; plain regular is
    # "Roboto", not "RobotoRegular".
    from font_roboto import Roboto, RobotoBlack, RobotoBold, RobotoMedium
    paths = {"black": RobotoBlack, "bold": RobotoBold,
             "medium": RobotoMedium, "regular": Roboto}
    return ImageFont.truetype(paths[name], size)


MIN_POOL_N = 150
FORWARDS = ("C", "LW", "RW")
# Under this many games at his position the grades are one good night away
# from moving 30 percentiles, and the card says so.
EARLY_GP = 20


def pool_info(ref: str) -> tuple[int, int, str | None]:
    """(pool size, game floor, season label) for the position being ranked
    against. build_pool.py stamps these per position; a pool.json without
    the stamp is the pre-launch file and gets the old fixed floors."""
    p = pool()
    n = max(p.get("counts", {}).get(ref, {}).values() or [0])
    info = p.get("meta", {}).get("positions", {}).get(ref, {})
    floor = info.get("floor") or p.get("min_pool_glgp" if ref == "G" else "min_pool_gp", 50)
    return n, floor, info.get("source")


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


# The tier ladder, in the vernacular the audience actually uses. A rank like
# "62nd" takes a beat to interpret; "SOLID" does not, and the ordinal is still
# printed beside it for anyone who wants the precision.
TIERS = [
    (90, "ELITE"),
    (78, "STUD"),
    (62, "SOLID"),
    (45, "MID"),
    (30, "WEAK"),
    (15, "BAD"),
    (0,  "SHITTER"),
]


def tier(p: int) -> str:
    for floor, word in TIERS:
        if p >= floor:
            return word
    return TIERS[-1][1]


def _pole(p: int) -> tuple:
    """Bar colour by tier. Redundant with bar length on purpose -- the point is
    that a glance at colour answers 'good or bad' before length is read."""
    if p >= 62:
        return GREEN
    if p >= 45:
        return AMBER
    return RED


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
        return f"{tier(best[1])} {primary} ACROSS THE BOARD"
    if worst[1] >= 60:
        return f"{tier(worst[1])} {primary} EVERYWHERE"
    return f"{tier(best[1])} {best[0]}  ·  {tier(worst[1])} {worst[0]}"


def _bar_row(d, y, lbl, metric, value, p, f_lbl, f_val, f_small, small=False):
    """One rating row: label, raw rate, bar, tier word.

    The bar runs the full width left to right -- 0 at the left, best at the
    right -- rather than diverging from a midpoint. Colour and length both
    encode the same rank on purpose: colour answers "good or bad" at a glance,
    length gives the degree, and the printed tier word means the reading never
    depends on colour alone.
    """
    h = 12 if small else 16
    x0 = PAD + (172 if small else 230)
    x1 = W - PAD - (132 if small else 168)
    _text(d, (PAD + (24 if small else 0), y + 2), lbl, f_lbl, MUTED if small else TEXT)
    d.rounded_rectangle([x0, y + 6, x1, y + 6 + h], radius=h // 2, fill=(30, 34, 43))
    if p is not None:
        col = _pole(p)
        w = max((x1 - x0) * p / 100, h)
        d.rounded_rectangle([x0, y + 6, x0 + w, y + 6 + h], radius=h // 2, fill=col)
        _text(d, (W - PAD, y - 2), tier(p), f_val, TEXT, anchor="ra")
        if not small:
            _text(d, (W - PAD, y + 21), str(p), f_small, DIM, anchor="ra")
    else:
        _text(d, (W - PAD, y + 2), "n/a", f_val, DIM, anchor="ra")
    _text(d, (x0 - 16, y + 2), _fmt(metric, value), f_val, MUTED, anchor="ra")
    return y + (36 if small else 50)


def render(m: dict, read: str | None = None, _debug: dict | None = None) -> bytes:
    """Draw the card for one player. `read` is optional prose under the stats.

    `_debug`, if given, receives the radar's geometry (centre, radius, axes,
    vertices) so a test can check the drawn pixels against the numbers."""
    name = str(m.get("name") or "unknown")
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    goals, assists = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
    points = goals + assists
    plusmin = ea._num(m.get("skplusmin"))
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
    f_read = _font("medium", 27)
    f_foot = _font("bold", 17)

    metrics = ROWS_BY_POS.get(primary, ROWS_BY_POS["C"])
    rows = []
    for key in metrics:
        if key not in rates:
            continue
        # A goalie with games but no recorded saves is missing data, not a
        # player who faced nothing -- ranking that as 0th would be a lie, and
        # the read would then describe him as never seeing the puck.
        rows.append((LABELS[key], key, rates[key], percentile(primary, key, rates[key])))

    # The other role he plays, ranked in ITS pool -- a goalie's skater numbers
    # are compared to skaters, never to goalies.
    sec_rows, sec_header = [], ""
    if is_goalie and skater_gp >= 10:
        sec_pos = next((pos for pos, _ in posns if pos != "G"), None)
        if sec_pos:
            # skater_gp is his total out-of-net games; sec_pos is only where
            # he played most of them, and the pool he is ranked against.
            sec_header = f"ALSO SKATES  ·  {skater_gp:.0f} GP  ·  RANKED VS {sec_pos}"
            for key in ("scoring", "playmaking", "impact"):
                if key in rates:
                    sec_rows.append((LABELS[key], key, rates[key],
                                     percentile(sec_pos, key, rates[key])))
    elif not is_goalie and glgp >= 10:
        sec_header = f"ALSO PLAYS NET  ·  {glgp:.0f} GP  ·  RANKED VS G"
        for key in ("savepct", "gaa"):
            if key in rates:
                sec_rows.append((LABELS[key], key, rates[key],
                                 percentile("G", key, rates[key])))

    verdict = _verdict(primary, rows, is_goalie)

    # Fewer than three axes is a line, not a shape -- skip the radar then.
    axes = radar_axes(primary, rates, is_goalie)
    show_radar = len(axes) >= 3
    radar_h = 2 * RADAR_R + 2 * RADAR_LABEL_ROOM + 16

    read_lines: list[str] = []
    if read:
        tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        maxw = W - 2 * PAD - 8
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
        read_lines = read_lines[:4]

    H = (150            # brand
         + 78           # name
         + (len(read_lines) * 36 + 22 if read_lines else 44)   # the read, or the computed verdict
         + 62           # position strip
         + 132          # stat tiles
         + (92 if not is_goalie and points else 0)   # play-style axis
         + (54 + len(sec_rows) * 36 + 22 if sec_rows else 0)
         + (radar_h if show_radar else 0)
         + 62           # bars header + legend
         + len(rows) * 52
         + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)

    # ---- brand
    y = 44
    mark = logo(54)
    if mark:
        img.paste(mark, (PAD, y - 12), mark)
    else:
        _text(d, (PAD, y), "Chel", f_brand, TEXT)
        bw = d.textlength("Chel", font=f_brand)
        _text(d, (PAD + bw, y), "Scout", f_brand, BLUE_TEXT)
        bw += d.textlength("Scout", font=f_brand)
        _text(d, (PAD + bw + 4, y + 14), ".net", f_brandsub, MUTED)
    _text(d, (W - PAD, y + 9), "PUBS SCOUTING REPORT", f_kicker, DIM, anchor="ra")

    # ---- name
    y += 62
    _text(d, (PAD, y), name[:17], f_name, TEXT)
    _text(d, (W - PAD, y + 30), f"{gp:.0f} GAMES", f_sub, MUTED, anchor="ra")
    # Two days into a season a 9-game player carries a 100th-percentile
    # discipline grade on zero penalty minutes. The number is real; the
    # sample isn't, and the card should say which before anyone argues.
    if (glgp if is_goalie else skater_gp) < EARLY_GP:
        _text(d, (W - PAD, y + 58), "EARLY READ · SMALL SAMPLE", f_statlbl, AMBER, anchor="ra")

    # ---- the read leads the card. It is the fastest path to "what is this
    # guy", so it goes above every number rather than under them. The computed
    # verdict is the fallback for when the model call failed.
    y += 78
    if read_lines:
        for ln in read_lines:
            _text(d, (PAD, y), ln, f_read, TEXT)
            y += 36
        y += 22
    else:
        _text(d, (PAD, y), verdict[:46], f_verdict, BLUE)
        y += 44

    # ---- positions as chips. The old proportional strip turned every
    # secondary role into an unlabelled sliver -- a 515-game goalie's 40 games
    # at centre became 3 pixels. Chips give every position the same legible
    # box and put the games played right next to it.
    _text(d, (PAD, y), "GAMES BY POSITION", f_statlbl, DIM)
    y += 30
    x = PAD
    for i, (pos, n) in enumerate(posns):
        label = f"{pos} {n}"
        cw = d.textlength(label, font=f_pos) + 34
        if x + cw > W - PAD:
            break
        d.rounded_rectangle([x, y, x + cw, y + 42], radius=8,
                            fill=BLUE if i == 0 else PANEL)
        _text(d, (x + cw / 2, y + 21), label, f_pos,
              TEXT if i == 0 else MUTED, anchor="mm")
        x += cw + 10
    y += 60

    # ---- headline tiles
    tiles = ([("SV%", _fmt("savepct", rates.get("savepct", 0))),
              ("GAA", _fmt("gaa", rates.get("gaa", 0))),
              # Shots faced is CONTEXT, not a grade -- it says how busy the
              # team in front of him keeps him, so it sits with the raw
              # numbers instead of on the graded good/bad scale.
              ("SHOTS/GM", f"{(ea._num(m.get('glsaves')) + ea._num(m.get('glga'))) / glgp:.1f}"
               if glgp else "--"),
              ("GP", f"{glgp:.0f}"), ("SO", f"{ea._num(m.get('glso')):.0f}")]
             if is_goalie else
             [("PTS", f"{points:.0f}"), ("G", f"{goals:.0f}"), ("A", f"{assists:.0f}"),
              ("P/GP", f"{points / skater_gp:.2f}" if skater_gp else "-"),
              # The bar below already grades his +/- as a per-game rate, but
              # the raw season number is what people actually quote at each
              # other, and it was nowhere on the card.
              ("+/-", f"{plusmin:+.0f}" if skater_gp else "-")])
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

    # ---- shooter <-> playmaker axis
    # For a forward this is the single thing people ask after "is he good" --
    # does he finish or does he set up. It is a balance, not a ranking, so it
    # gets a marker on an axis rather than a bar with a good end and a bad end.
    if not is_goalie and points:
        goal_share = goals / points
        _text(d, (PAD, y), "PLAY STYLE", f_statlbl, DIM)
        y += 28
        ax0, ax1 = PAD + 132, W - PAD - 132
        d.rounded_rectangle([ax0, y + 8, ax1, y + 14], radius=3, fill=(34, 38, 48))
        mx = ax0 + (ax1 - ax0) * (1 - goal_share)
        d.ellipse([mx - 9, y + 2, mx + 9, y + 20], fill=BLUE)
        _text(d, (PAD, y + 1), "SHOOTER", f_bar, TEXT if goal_share >= 0.5 else MUTED)
        _text(d, (W - PAD, y + 1), "PLAYMAKER", f_bar,
              TEXT if goal_share < 0.5 else MUTED, anchor="ra")
        y += 30
        _text(d, ((ax0 + ax1) / 2, y), f"{goal_share * 100:.0f}% of his points are goals",
              f_note, DIM, anchor="ma")
        y += 34

    # ---- rating bars
    ref = _breakpoints(primary, rows[0][1])[1] if rows else primary
    n_pool, floor, source = pool_info(ref)
    ref_name = "FORWARDS" if ref == "F" else primary
    # The floor and the season come from the pool file, not a constant: in
    # the first weeks of a new season the floor ramps up from 15 and some
    # positions are still ranked on last season's curve. Print what it is.
    header = f"RANKED VS {n_pool} {ref_name} WITH {floor}+ GAMES"
    if source:
        header += f"  ·  {source.upper()} POOL"
    _text(d, (PAD, y), header, f_statlbl, DIM)
    y += 24

    # ---- the shape, then the bars. Same percentiles twice on purpose: the
    # radar is the glance ("wide up top, dented at physicality"), the bars are
    # the precision, and having both on one card lets anyone check one
    # against the other.
    if show_radar:
        _text(d, (W - PAD, y - 24), "grey ring = a typical player", f_note, DIM, anchor="ra")
        cx, cy = W / 2, y + RADAR_LABEL_ROOM + RADAR_R
        pts = _radar(img, cx, cy, RADAR_R, axes, f_statlbl, f_val)
        d = ImageDraw.Draw(img)
        if _debug is not None:
            _debug["radar"] = {"cx": cx, "cy": cy, "r": RADAR_R, "axes": axes, "points": pts}
        y += radar_h

    _text(d, (PAD, y), "longer and greener is better · 50th is a typical player", f_note, DIM)
    y += 30

    for lbl, metric, value, p in rows:
        y = _bar_row(d, y, lbl, metric, value, p, f_bar, f_val, f_note)

    # ---- his other job, if he has one.
    # Plenty of pubs players split time between net and out. Which set of stats
    # someone wants depends on why they are scouting, and there is no way to
    # know that from a gamertag -- so the card leads with the position he
    # actually plays most and appends the other role underneath at a smaller
    # size, rather than picking one and hiding the rest.
    if sec_rows:
        y += 8
        d.rounded_rectangle([PAD, y, W - PAD, y + 46 + len(sec_rows) * 36], radius=12,
                            fill=(18, 21, 27))
        _text(d, (PAD + 24, y + 15), sec_header, f_statlbl, MUTED)
        y += 46
        for lbl, metric, value, p in sec_rows:
            y = _bar_row(d, y, SHORT_LABELS.get(metric, lbl), metric, value, p,
                         f_note, f_note, f_note, small=True)
        y += 14

    # ---- footer
    y = H - 56
    d.line([PAD, y - 20, W - PAD, y - 20], fill=LINE)
    _text(d, (PAD, y), "chelscout.net", f_foot, BLUE_TEXT)
    _text(d, (W - PAD, y), "SCOUT SMARTER. CHIRP RESPONSIBLY.", f_foot, DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ club
def _wrap(d, text: str, font, maxw: int, max_lines: int = 3) -> list[str]:
    lines, line = [], ""
    for wd in text.split():
        trial = f"{line} {wd}".strip()
        if d.textlength(trial, font=font) <= maxw:
            line = trial
        else:
            lines.append(line)
            line = wd
    if line:
        lines.append(line)
    return lines[:max_lines]


def render_club(s: dict, read: str | None = None) -> bytes:
    """The club card: record and goals up top, the roster-shape radar and
    recent form side by side, the roster underneath. Every block is optional
    except the header and roster, because EA doesn't always hand over the
    season stats or the match list."""
    f_kicker = _font("bold", 17)
    f_name = _font("black", 60)
    f_rec = _font("black", 40)
    f_sub = _font("medium", 19)
    f_read = _font("medium", 27)
    f_stat = _font("black", 42)
    f_statlbl = _font("bold", 16)
    f_note = _font("medium", 16)
    f_word = _font("bold", 17)
    f_cell = _font("medium", 19)
    f_row = _font("bold", 20)
    f_chip = _font("black", 18)
    f_foot = _font("bold", 17)

    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    read_lines = _wrap(tmp, read, f_read, W - 2 * PAD - 8, max_lines=4) if read else []

    tiles = []
    if s.get("gf") is not None and s.get("ga") is not None:
        tiles += [("GF", f"{s['gf']:.0f}"), ("GA", f"{s['ga']:.0f}"),
                  ("DIFF", f"{s['gf'] - s['ga']:+.0f}")]
        if s.get("gp"):
            tiles += [("GF/GP", f"{s['gf'] / s['gp']:.1f}"), ("GA/GP", f"{s['ga'] / s['gp']:.1f}")]
    elif s.get("w") is not None:
        tiles += [("W", f"{s['w']:.0f}"), ("L", f"{s['l']:.0f}"), ("OTL", f"{s['otl'] or 0:.0f}")]

    axes = s.get("shape") or []
    show_radar = len(axes) >= 3
    formv = (s.get("form") or [])[:10]
    show_mid = show_radar or formv
    rows = s["skaters"][:7] + s["goalies"][:1]

    H = (150 + 72 + 34
         + (len(read_lines) * 36 + 22 if read_lines else 12)
         + (146 if tiles else 0)
         + (2 * 118 + 2 * 44 + 40 if show_mid else 0)
         + (30 + len(axes) * 50 + 14 if show_radar else 0)
         + 34 + 30 + len(rows) * 42 + 30
         + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)

    y = 44
    mark = logo(54)
    if mark:
        img.paste(mark, (PAD, y - 12), mark)
    _text(d, (W - PAD, y + 9), "PUBS CLUB REPORT", f_kicker, DIM, anchor="ra")

    y += 62
    # Club names run long ("Bar Down Ur Sister"): step the size down before
    # resorting to an ellipsis, and only clip once 40px still doesn't fit.
    name = s["name"]
    budget = W - 2 * PAD - 230
    for size in (60, 52, 46, 40):
        f_name = _font("black", size)
        if d.textlength(name, font=f_name) <= budget:
            break
    while d.textlength(name, font=f_name) > budget and len(name) > 6:
        name = name[:-2].rstrip() + "…"
    _text(d, (PAD, y + (60 - size) // 2), name, f_name, TEXT)
    if s.get("w") is not None:
        _text(d, (W - PAD, y + 8), f"{s['w']:.0f}-{s['l']:.0f}-{s['otl'] or 0:.0f}", f_rec, TEXT, anchor="ra")
    y += 72
    # Under the name, not beside it -- a long club name and a right-aligned
    # subtitle share the same line otherwise.
    sub = "  ·  ".join(b for b in (
        f"DIVISION {s['division']}" if s.get("division") not in (None, "") else "",
        f"{s['gp']:.0f} GP" if s.get("gp") else "",
        f"{s['n_members']} MEMBERS",
        f"{s['platform'].upper()}" if s.get("platform") else "") if b)
    _text(d, (PAD, y), sub, f_sub, MUTED)
    y += 34
    if read_lines:
        for ln in read_lines:
            _text(d, (PAD, y), ln, f_read, TEXT)
            y += 36
        y += 22
    else:
        y += 12

    if tiles:
        tw = (W - 2 * PAD) / len(tiles)
        d.rounded_rectangle([PAD, y, W - PAD, y + 110], radius=14, fill=PANEL)
        for i, (lbl, val) in enumerate(tiles):
            cx = PAD + tw * i + tw / 2
            if i:
                d.line([PAD + tw * i, y + 20, PAD + tw * i, y + 90], fill=LINE)
            _text(d, (cx, y + 42), val, f_stat, TEXT, anchor="mm")
            _text(d, (cx, y + 84), lbl, f_statlbl, MUTED, anchor="mm")
        y += 146

    if show_mid:
        if show_radar:
            _text(d, (PAD, y), "ROSTER SHAPE  ·  TOP 6 SKATERS + G, RANKED VS THEIR POSITIONS", f_statlbl, DIM)
        top = y + 24
        if show_radar:
            r = 118
            _radar(img, PAD + 205, top + 44 + r, r, axes, _font("bold", 14), _font("bold", 15))
            d = ImageDraw.Draw(img)
        if formv:
            fx = W - PAD - 270
            fy = top + 30
            _text(d, (fx, fy), f"LAST {len(formv)}", f_statlbl, DIM)
            for i, res in enumerate(formv):
                col = GREEN if res == "W" else (AMBER if res == "OTL" else RED)
                x = fx + (i % 5) * 56
                yy = fy + 30 + (i // 5) * 60
                d.rounded_rectangle([x, yy, x + 50, yy + 50], radius=10, fill=col)
                _text(d, (x + 25, yy + 25), res, _font("black", 18 if res != "OTL" else 13), TEXT, anchor="mm")
            wl = (sum(r == "W" for r in formv), sum(r == "L" for r in formv), sum(r == "OTL" for r in formv))
            _text(d, (fx, fy + 30 + 2 * 60 + 8), f"{wl[0]}-{wl[1]}-{wl[2]} in the last {len(formv)}", f_note, MUTED)
            streak_res = formv[0]
            n = 0
            for r_ in formv:
                if r_ == streak_res:
                    n += 1
                else:
                    break
            _text(d, (fx, fy + 30 + 2 * 60 + 44), "STREAK", f_statlbl, DIM)
            _text(d, (fx, fy + 30 + 2 * 60 + 68), f"{'W' if streak_res == 'W' else 'L'}{n}", _font("black", 34),
                  GREEN if streak_res == "W" else RED)
        y += 2 * 118 + 2 * 44 + 40

    # The same grades as bars, under the radar -- the radar is the glance,
    # the bars are the precision, same as the player card.
    if show_radar:
        _text(d, (PAD, y), "longer and greener is better · 50th is a typical player", f_note, DIM)
        y += 30
        for lbl, key, p in axes:
            h = 16
            x0, x1 = PAD + 230, W - PAD - 168
            _text(d, (PAD, y + 2), lbl, _font("bold", 20), TEXT)
            d.rounded_rectangle([x0, y + 6, x1, y + 6 + h], radius=h // 2, fill=(30, 34, 43))
            wbar = max((x1 - x0) * p / 100, h)
            d.rounded_rectangle([x0, y + 6, x0 + wbar, y + 6 + h], radius=h // 2, fill=_pole(p))
            _text(d, (W - PAD, y - 2), tier(p), _font("bold", 19), TEXT, anchor="ra")
            _text(d, (W - PAD, y + 21), str(p), f_note, DIM, anchor="ra")
            y += 50
        y += 14

    _text(d, (PAD, y), "ROSTER  ·  GRADE IS HIS POSITION'S PERCENTILE", f_statlbl, DIM)
    y += 34
    cols = [("PLAYER", PAD, "la"), ("POS", 330, "ma"), ("GP", 400, "ra"), ("G", 460, "ra"), ("A", 520, "ra"),
            ("PTS", 590, "ra"), ("+/-", 660, "ra"), ("GRADE", W - PAD, "ra")]
    for lbl, x, a in cols:
        _text(d, (x, y), lbl, _font("bold", 15), DIM, anchor=a)
    y += 30
    for i, r in enumerate(rows):
        if i % 2 == 0:
            d.rounded_rectangle([PAD - 12, y - 6, W - PAD + 12, y + 34], radius=8, fill=(18, 21, 27))
        _text(d, (PAD, y + 2), r["name"][:17], f_row, TEXT)
        is_g = r["primary"] == "G"
        d.rounded_rectangle([312, y + 2, 348, y + 26], radius=6, fill=PANEL if is_g else BLUE)
        _text(d, (330, y + 14), r["primary"], _font("bold", 14), TEXT, anchor="mm")
        if is_g:
            cells = [(400, f"{r['glgp']:.0f}"), (460, "-"), (520, "-"),
                     (590, _fmt("savepct", r["rates"].get("savepct", 0))),
                     (660, f"{r['rates'].get('gaa', 0):.2f}")]
        else:
            cells = [(400, f"{r['gp']:.0f}"), (460, f"{r['g']:.0f}"), (520, f"{r['a']:.0f}"),
                     (590, f"{r['pts']:.0f}"), (660, f"{r['pm']:+.0f}")]
        for x, v in cells:
            _text(d, (x, y + 2), v, f_cell, MUTED if v == "-" else TEXT, anchor="ra")
        p = r.get("grade")
        if p is not None:
            _text(d, (W - PAD, y + 2), tier(p), _font("bold", 18), _pole(p), anchor="ra")
            _text(d, (W - PAD - 92, y + 6), str(p), _font("medium", 14), DIM, anchor="ra")
        else:
            _text(d, (W - PAD, y + 2), "n/a", _font("bold", 18), DIM, anchor="ra")
        y += 42
    _text(d, (PAD, y + 4), "G row: SV% and GAA in place of PTS and +/-", f_note, DIM)

    y = H - 56
    d.line([PAD, y - 20, W - PAD, y - 20], fill=LINE)
    _text(d, (PAD, y), "chelscout.net", f_foot, BLUE_TEXT)
    _text(d, (W - PAD, y), "SCOUT SMARTER. CHIRP RESPONSIBLY.", f_foot, DIM, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------- matchup
def _radar_multi(img, cx, cy, r, series, labels=None, f_lbl=None, ring_axis=None):
    """Rings plus one or more shapes: series = [(values, rgb, rgba_fill)].
    ring_axis draws a red ring on that vertex of the LAST series -- the
    axis to attack. Small radars (r < 80) drop the vertex dots.

    Every fill gets its OWN composited layer. PIL's draw REPLACES pixels
    instead of blending them, so both shapes on one overlay meant the
    second fill erased the first wherever they overlapped -- which is most
    of the pentagon. Lowering the alpha only made both paler; it could
    never let one read through the other. One layer per fill does.
    """
    n = len(series[0][0])
    base = img.convert("RGBA")

    def layer(paint):
        """Draw on a fresh transparent layer, then alpha-composite it."""
        nonlocal base
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        paint(ImageDraw.Draw(ov))
        base = Image.alpha_composite(base, ov)

    def grid(d):
        for ring in (25, 50, 75, 100):
            col = (70, 76, 92, 255) if ring == 50 else (*LINE, 255)
            d.polygon(radar_points(cx, cy, r, [ring] * n), outline=col)
        for x, y in radar_points(cx, cy, r, [100] * n):
            d.line([(cx, cy), (x, y)], fill=(*LINE, 255), width=1)

    shapes = [(radar_points(cx, cy, r, [max(v or 0, 3) for v in vals]), col, fill)
              for vals, col, fill in series]
    layer(grid)
    for pts, _col, fill in shapes:                     # translucent fills, blended
        layer(lambda d, p=pts, f=fill: d.polygon(p, fill=f))

    def marks(d):                                      # opaque edges on top
        for pts, col, _fill in shapes:
            d.line(pts + [pts[0]], fill=(*col, 255), width=3 if r >= 80 else 2, joint="curve")
            if r >= 80:
                for x, y in pts:
                    d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(*col, 255), outline=(*BG, 255), width=2)
        if ring_axis is not None:
            vx, vy = shapes[-1][0][ring_axis]
            d.ellipse([vx - 8, vy - 8, vx + 8, vy + 8], outline=(*RED, 255), width=3)

    layer(marks)
    img.paste(base.convert("RGB"))
    if labels:
        d = ImageDraw.Draw(img)
        for i, (lbl, (x, y)) in enumerate(zip(labels, radar_points(cx, cy, r, [100] * n))):
            a = -math.pi / 2 + 2 * math.pi * i / n
            dx, dy = math.cos(a), math.sin(a)
            anchor = "lm" if dx > 0.3 else ("rm" if dx < -0.3 else "mm")
            _text(d, (x + dx * 30, y + dy * 22), lbl, f_lbl, MUTED, anchor=anchor)


THEM = (196, 132, 31)             # the other club: amber outline, never red (red means "attack here")
# Semi-transparent fills: the two shapes overlap almost everywhere, so both
# must stay readable through each other. These blend for real now (each fill
# is its own composited layer in _radar_multi), so the overlap is a third
# colour carrying both hues rather than whichever was drawn last.
THEM_FILL = (196, 132, 31, 92)
US_FILL = (0, 105, 250, 96)


# Around the pentagon the way the ice reads: C at the top, wings either
# side of him, the D pair underneath.
POS_ORDER = ["C", "RW", "RD", "LD", "LW"]


def render_matchup(a: dict, b: dict, pairs: list, read: str | None = None,
                   custom: bool = False, note: str | None = None,
                   fwds: dict | None = None) -> bytes:
    """Numbers only, no prescriptions. Main radar is the PLAY-STYLE overlay
    (five-man mean percentile per skill); below it, one block per 5v5
    pairing showing both men's full axis lines as paired bars -- the
    player-vs-player matchup drawn, not described. Their weakest graded
    skill (physicality excluded -- noise at this level) prints red; that is
    the only emphasis. `read` is accepted for compatibility and ignored."""
    import club as clubmod
    f_kicker = _font("bold", 17); f_sub = _font("medium", 18)
    f_h = _font("bold", 16); f_nm = _font("bold", 22); f_ax = _font("bold", 15)
    f_val = _font("black", 16); f_edge = _font("black", 18); f_note = _font("medium", 16)

    by_slot = {p["slot"]: p for p in pairs}
    order = [s for s in POS_ORDER if s in by_slot]
    R = 118
    PAIR_H = 92
    H = (150 + 70 + (34 if note else 0)          # header
         + 46 + 2 * R + 132                       # two radars side by side
         + 56                                     # goalie line
         + 44 + 40 + len(order) * PAIR_H          # gap ladder
         + (34 + 232 + 26 if fwds and (fwds.get("us") or fwds.get("them")) else 0)
         + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)
    mark = logo(54)
    if mark:
        img.paste(mark, (PAD, 32), mark)
    _text(d, (W - PAD, 53), "PUBS MATCHUP REPORT" + ("  ·  CUSTOM LINEUP" if custom else ""), f_kicker, DIM, anchor="ra")

    def rec(s):
        return f"{s['w']:.0f}-{s['l']:.0f}-{s['otl'] or 0:.0f}" if s.get("w") is not None else ""

    y = 104
    half = (W - 2 * PAD) / 2 - 30
    for s, x, anchor, col, tag in ((a, PAD, "la", BLUE_TEXT, "YOU"), (b, W - PAD, "ra", THEM, "THEM")):
        name = s["name"]
        for size in (36, 30, 26, 22):
            f = _font("black", size)
            if d.textlength(name, font=f) <= half:
                break
        _text(d, (x, y), name, f, TEXT, anchor=anchor)
        sub = "  ·  ".join(t for t in (tag, rec(s), f"DIV {s['division']}" if s.get("division") not in (None, "") else "") if t)
        _text(d, (x, y + 46), sub, f_sub, col, anchor=anchor)
    _text(d, (W / 2, y + 22), "VS", _font("black", 24), DIM, anchor="mm")
    y += 70
    if note:
        _text(d, (PAD, y + 4), note, f_note, DIM)
        y += 34

    # ---- forwards: twin PUCK TIME strips under the names. Rows sorted by
    # possession -- the order IS "who holds the puck most". One shared bar
    # scale across both panels so the eye compares across teams. Style is
    # the player card's shooter/playmaker slider, already-learned idiom.
    if fwds and (fwds.get("us") or fwds.get("them")):
        f_ph = _font("bold", 14); f_pn = _font("bold", 19)
        f_hero = _font("black", 26); f_sty = _font("medium", 13); f_end = _font("bold", 11)
        all_poss = [f["poss"] for f in fwds.get("us", []) + fwds.get("them", [])]
        top = max(all_poss) if all_poss else 1
        _text(d, (PAD, y), "FORWARDS  ·  PUCK TIME  ·  FROM BANKED MATCHES", f_h, DIM)
        y += 34
        pw = (W - 2 * PAD - 20) / 2
        for side, x0, col in (("us", PAD, BLUE_TEXT), ("them", PAD + pw + 20, THEM)):
            rows = sorted(fwds.get(side, []), key=lambda f: -f["poss"])
            d.rounded_rectangle([x0, y, x0 + pw, y + 232], radius=12, fill=(18, 21, 27))
            _text(d, (x0 + 18, y + 12), "PUCK TIME · S/GM", f_ph, DIM)
            ix0, ix1 = x0 + 18, x0 + pw - 18
            for i, f in enumerate(rows[:3]):
                ry = y + 40 + i * 62
                _text(d, (ix0, ry), f["name"], f_pn, col)
                _text(d, (ix1, ry - 4), f"{f['poss']}s", f_hero, TEXT, anchor="ra")
                # possession bar, shared scale
                d.rounded_rectangle([ix0, ry + 28, ix1, ry + 34], radius=3, fill=(30, 34, 43))
                if f["poss"] > 0:
                    d.rounded_rectangle([ix0, ry + 28, ix0 + (ix1 - ix0) * f["poss"] / top, ry + 34],
                                        radius=3, fill=col)
                # style: goal-share text left, mini spectrum right
                _text(d, (ix0, ry + 40), f"{f['gs']}% OF PTS = GOALS · {f['gp']} GM", f_sty, MUTED)
                sx1, sx0 = ix1, ix1 - 110
                sy = ry + 48
                d.line([sx0, sy, sx1, sy], fill=(30, 34, 43), width=4)
                d.line([(sx0 + sx1) / 2, sy - 5, (sx0 + sx1) / 2, sy + 5], fill=(70, 76, 92), width=2)
                dotx = sx0 + (sx1 - sx0) * (100 - f["gs"]) / 100
                d.ellipse([dotx - 5, sy - 5, dotx + 5, sy + 5], fill=col, outline=(*BG, 255), width=2)
                if i == len(rows[:3]) - 1:
                    _text(d, (sx0, sy + 9), "SHO", f_end, DIM)
                    _text(d, (sx1, sy + 9), "PLY", f_end, DIM, anchor="ra")
        y += 232 + 26


    # ---- two radars, side by side: team play style, and the five
    # position matchups (each axis = our man at that slot vs the man he
    # lines up against, both as overall percentile).
    _text(d, (PAD, y), "TEAM PLAY STYLE  ·  FIVE-MAN MEAN", f_h, DIM)
    _text(d, (W / 2 + 40, y), "BY POSITION  ·  YOUR MAN vs HIS MAN", f_h, DIM)
    ly0 = y + 24
    d.rounded_rectangle([PAD, ly0 + 2, PAD + 22, ly0 + 14], radius=3, fill=BLUE_TEXT)
    _text(d, (PAD + 30, ly0 + 8), "YOU", _font("bold", 14), MUTED, anchor="lm")
    d.rounded_rectangle([PAD + 90, ly0 + 2, PAD + 112, ly0 + 14], radius=3, fill=THEM)
    _text(d, (PAD + 120, ly0 + 8), "THEM", _font("bold", 14), MUTED, anchor="lm")
    y += 46
    cy = y + 86 + R
    f_axlbl = _font("bold", 14)

    def draw_radar(cx, axes_labels, vals_us, vals_them, label_dist=30, val_dist=26):
        _radar_multi(img, cx, cy, R, [(vals_them, THEM, THEM_FILL), (vals_us, BLUE_TEXT, US_FILL)])
        dd = ImageDraw.Draw(img)
        n = len(axes_labels)
        for i, lbl in enumerate(axes_labels):
            ang = -math.pi / 2 + 2 * math.pi * i / n
            dx, dy = math.cos(ang), math.sin(ang)
            x, yv = cx + dx * (R + label_dist), cy + dy * (R + val_dist)
            _text(dd, (x, yv - 10), lbl, f_axlbl, DIM, anchor="ma")
            vu, vt = vals_us[i], vals_them[i]
            w_us = dd.textlength(str(vu), font=f_val)
            w_mid = dd.textlength(" · ", font=f_val)
            total = w_us + w_mid + dd.textlength(str(vt), font=f_val)
            x0 = x - total / 2
            _text(dd, (x0, yv + 9), str(vu), f_val, BLUE_TEXT)
            _text(dd, (x0 + w_us, yv + 9), " · ", f_val, DIM)
            _text(dd, (x0 + w_us + w_mid, yv + 9), str(vt), f_val, THEM)

    def axis_mean(side):
        out = []
        for i in range(len(clubmod.AXES)):
            vs = [p[side][i] for p in pairs if p[side][i] is not None]
            out.append(round(sum(vs) / len(vs)) if vs else 0)
        return out

    draw_radar(PAD + 92 + R, [lbl for lbl, _ in clubmod.AXES], axis_mean("ax_us"), axis_mean("ax_them"))
    draw_radar(W - PAD - 92 - R, order,
               [by_slot[s2]["ov_us"] or 0 for s2 in order],
               [by_slot[s2]["ov_them"] or 0 for s2 in order])
    d = ImageDraw.Draw(img)
    y = cy + R + 96

    # ---- goalies, numbers only
    def gline(s):
        g = next((r for r in s.get("goalies", []) if r["rates"].get("savepct")), None)
        if not g:
            return "no goalie sample"
        pct = g.get("grade")
        return (f"{g['name']}  {g['rates']['savepct']:.3f} sv%  {g['rates'].get('gaa', 0):.2f} GAA"
                + (f"  ·  {pct}" if pct is not None else ""))
    _text(d, (PAD, y), gline(a), f_note, BLUE_TEXT)
    _text(d, (W - PAD, y), gline(b), f_note, THEM, anchor="ra")
    _text(d, (W / 2, y), "IN NET", f_h, DIM, anchor="ma")
    y += 56

    # ---- the gap ladder: one row per pairing, one diverging bar per row.
    # The bar grows from the centre line toward whoever holds the edge; its
    # length is the gap. Five bars total -- readable in seconds. The per-axis
    # detail lives on the interactive board, not here.
    _text(d, (PAD, y), "5V5  ·  OVERALL PERCENTILE GAP AT EACH POSITION  ·  bar grows toward the better man", f_h, DIM)
    y += 30
    gaps = [(s2, by_slot[s2]) for s2 in order
            if by_slot[s2]["ov_us"] is not None and by_slot[s2]["ov_them"] is not None]
    if gaps:
        top_slot, top = max(gaps, key=lambda t: abs(t[1]["ov_us"] - t[1]["ov_them"]))
        tdiff = top["ov_us"] - top["ov_them"]
        if abs(tdiff) >= 10:
            side = "YOU" if tdiff > 0 else "THEM"
            col = BLUE_TEXT if tdiff > 0 else THEM
            _text(d, (PAD, y), f"Biggest gap: {top_slot} -- {top['us']['name']} {top['ov_us']} vs "
                               f"{top['them']['name']} {top['ov_them']}  ({side} +{abs(tdiff)})", f_note, col)
        else:
            _text(d, (PAD, y), "No position gap over 10 points -- even lineups.", f_note, DIM)
        y += 40
    cxm = W / 2
    half_w = 190          # bar span each side of centre; 30+ points = full
    f_gap = _font("black", 20); f_small = _font("bold", 14)
    for s2 in order:
        p = by_slot[s2]
        vu, vt = p["ov_us"], p["ov_them"]
        d.rounded_rectangle([PAD, y, W - PAD, y + PAIR_H - 10], radius=12, fill=(18, 21, 27))
        cyr = y + (PAIR_H - 10) / 2
        _text(d, (PAD + 20, y + 10), f"{s2} v {p['vs']}", f_small, DIM)
        def fit(nm, maxw):
            while d.textlength(nm, font=f_nm) > maxw and len(nm) > 4:
                nm = nm[:-2].rstrip() + "\u2026"
            return nm
        name_w = (cxm - half_w) - (PAD + 20) - 52
        _text(d, (PAD + 20, y + 32), fit(p["us"]["name"], name_w), f_nm, BLUE_TEXT)
        # tiny sample line: the games behind each man's number
        f_tiny = _font("medium", 12)
        def statline(r):
            gp = r.get("gp", 0) or 0
            pts = r.get("pts", 0) or 0
            return f"{pts / gp:.2f} p/gp · {gp:.0f} gp" if gp else "no games"
        _text(d, (PAD + 20, y + 60), statline(p["us"]), f_tiny, DIM)
        _text(d, (W - PAD - 20, y + 60), statline(p["them"]), f_tiny, DIM, anchor="ra")
        _text(d, (W - PAD - 20, y + 10), "" , f_small, DIM, anchor="ra")
        _text(d, (W - PAD - 20, y + 32), fit(p["them"]["name"], name_w), f_nm, THEM, anchor="ra")
        # centre track
        tx0, tx1 = cxm - half_w, cxm + half_w
        ty = cyr + 10
        d.line([tx0, ty, tx1, ty], fill=(30, 34, 43), width=6)
        d.line([cxm, ty - 9, cxm, ty + 9], fill=(70, 76, 92), width=2)
        if vu is not None and vt is not None:
            diff = vu - vt
            wpx = min(abs(diff), 30) / 30 * half_w
            if diff > 0:
                d.rounded_rectangle([cxm - wpx, ty - 5, cxm, ty + 5], radius=4, fill=BLUE)
            elif diff < 0:
                d.rounded_rectangle([cxm, ty - 5, cxm + wpx, ty + 5], radius=4, fill=THEM)
            lab = "EVEN" if diff == 0 else f"{'YOU' if diff > 0 else 'THEM'} +{abs(diff)}"
            col = DIM if diff == 0 else (BLUE_TEXT if diff > 0 else THEM)
            _text(d, (cxm, y + 8), lab, f_gap, col, anchor="ma")
            _text(d, (tx0 - 14, ty - 10), str(vu), f_val, BLUE_TEXT, anchor="ra")
            _text(d, (tx1 + 14, ty - 10), str(vt), f_val, THEM)
        y += PAIR_H

    fy = H - 56
    d.line([PAD, fy - 20, W - PAD, fy - 20], fill=LINE)
    _text(d, (PAD, fy), "chelscout.net", _font("bold", 17), BLUE_TEXT)
    _text(d, (W - PAD, fy), "SCOUT SMARTER. CHIRP RESPONSIBLY.", _font("bold", 17), DIM, anchor="ra")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------- compare
# /pubcompare: two players, one card, the matchup card's two-column idiom.
# Left man blue, right man amber.
#
# "Better" means BETTER AT HIS OWN JOB: each man's average grade on the
# skills his own position is judged on (ROWS_BY_POS, the rows his /pubscout
# card leads with), graded against his own position. That is the only
# definition that survives a C against a D -- a defenceman is not graded on
# goals -- and a goalie against a skater, who share no scale at all.
#
# Two kinds of card fall out of that:
#   same role  (skater v skater, goalie v goalie): one overlaid radar and a
#              skill-by-skill ladder, both ranked at each man's own position.
#   cross role (goalie v skater): no shared scale, so each man gets his own
#              "at his own job" panel side by side, and wherever the two DO
#              overlap -- the goalie also skates, or the skater also plays
#              net -- a secondary head-to-head in that role.

# Shutouts swing 60 points on a handful of games: on the card, not in the
# overall.
JOB_KEYS = {pos: [k for k in keys if k != "shutouts"] for pos, keys in ROWS_BY_POS.items()}
# A side role needs this many games before it earns a head-to-head.
SIDE_ROLE_GP = 10
_SHORT = {"scoring": "GOALS", "playmaking": "ASSISTS", "impact": "+/-",
          "physicality": "HITS", "discipline": "PIM", "savepct": "SV%",
          "gaa": "GAA", "shutouts": "SO"}
_POS_WORD = {"C": "CENTRE", "LW": "LEFT WING", "RW": "RIGHT WING", "D": "D-MAN", "G": "GOALIE"}


def _cmp_side(m: dict) -> dict:
    posns = _positions(m)
    primary = posns[0][0] if posns else "?"
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    rates = _rates(m)
    skater_pos = primary if primary != "G" else next((p for p, _ in posns if p != "G"), None)
    is_g = primary == "G"
    job = []
    for key in ROWS_BY_POS.get(primary, ROWS_BY_POS["C"]):
        if key in rates:
            job.append({"key": key, "label": LABELS[key], "v": rates[key],
                        "p": percentile(primary, key, rates[key])})
    ps = [r["p"] for r in job if r["p"] is not None and r["key"] in JOB_KEYS.get(primary, [])]
    return {"m": m, "name": str(m.get("name") or "unknown"), "primary": primary,
            "is_g": is_g, "posns": posns, "rates": rates, "gp": gp, "glgp": glgp,
            "skater_gp": max(gp - glgp, 0), "skater_pos": skater_pos,
            "role_gp": glgp if is_g else max(gp - glgp, 0),
            "job": job, "overall": round(sum(ps) / len(ps)) if ps else None,
            "radar": radar_axes(primary, rates, is_g)}


def _h2h(a: dict, b: dict, keys: list, pos_a: str, pos_b: str) -> list:
    out = []
    for key in keys:
        va, vb = a["rates"].get(key), b["rates"].get(key)
        out.append({"key": key, "label": LABELS[key], "va": va, "vb": vb,
                    "pa": percentile(pos_a, key, va) if va is not None else None,
                    "pb": percentile(pos_b, key, vb) if vb is not None else None})
    return out


def compare_data(ma: dict, mb: dict) -> dict:
    """Everything the compare card and the voice need, computed once."""
    a, b = _cmp_side(ma), _cmp_side(mb)
    cross = a["is_g"] != b["is_g"]
    axes, extras = [], []
    if not cross:
        keys = RADAR_AXES["G" if a["is_g"] else "skater"]
        axes = _h2h(a, b, keys, a["primary"], b["primary"])
    else:
        g, sk = (a, b) if a["is_g"] else (b, a)
        if g["skater_gp"] >= SIDE_ROLE_GP and g["skater_pos"]:
            pos = {id(g): g["skater_pos"], id(sk): sk["primary"]}
            extras.append({"kind": "skates", "who": g["name"], "gp": g["skater_gp"],
                           "title": f"ON SKATES  ·  {g['name']}'S SIDE ROLE, {g['skater_gp']:.0f} GAMES AT {g['skater_pos']}",
                           "axes": _h2h(a, b, ["scoring", "playmaking", "impact"], pos[id(a)], pos[id(b)])})
        if sk["glgp"] >= SIDE_ROLE_GP:
            extras.append({"kind": "net", "who": sk["name"], "gp": sk["glgp"],
                           "title": f"IN NET  ·  {sk['name']}'S SIDE ROLE, {sk['glgp']:.0f} GAMES",
                           "axes": _h2h(a, b, ["savepct", "gaa"], "G", "G")})

    diff = (a["overall"] or 0) - (b["overall"] or 0)
    # Ties go to the bigger sample: the same grade on more games is the
    # surer thing, and "who's better" always gets a name.
    a_wins = diff > 0 or (diff == 0 and a["role_gp"] >= b["role_gp"])
    win, lose = (a, b) if a_wins else (b, a)
    edges = [ax for ax in axes if ax["pa"] is not None and ax["pb"] is not None]
    return {"a": a, "b": b, "cross": cross, "goalies": a["is_g"] and b["is_g"],
            "axes": axes, "extras": extras,
            "winner": win, "loser": lose, "margin": abs(diff),
            "a_skills": sum(1 for ax in edges if ax["pa"] > ax["pb"]),
            "b_skills": sum(1 for ax in edges if ax["pb"] > ax["pa"]),
            "small_sample": [s["name"] for s in (a, b) if s["role_gp"] < EARLY_GP]}


def compare_headline(c: dict) -> str:
    """The deterministic one-liner: who's better and how clearly. The card's
    fallback read, and the line the voice clip is held to."""
    w, l, mg = c["winner"], c["loser"], c["margin"]
    how = ("by a hair" if mg < 3 else "but it's close" if mg < 10
           else "clearly" if mg < 20 else "and it isn't close")
    job = " at his own job" if c["cross"] or w["primary"] != l["primary"] else ""
    return f"{w['name']} is better than {l['name']}{job}, {how}."


def format_compare(c: dict) -> str:
    """The head-to-head for the model: verdict first, each man's job grades
    with the card's tier words, then whatever overlaps. The model never
    decides who wins -- the code already has."""
    a, b, w, l = c["a"], c["b"], c["winner"], c["loser"]
    lines = [f"VERDICT (decided by code -- do not change it): {compare_headline(c)}",
             f"Better means a higher average grade at HIS OWN JOB: the skills his own "
             f"position is judged on, each graded against players at his own position. "
             f"{w['name']} {_ordinal(w['overall'] or 0)}, {l['name']} {_ordinal(l['overall'] or 0)} "
             f"(margin {c['margin']}).", ""]
    if c["cross"]:
        g, sk = (a, b) if a["is_g"] else (b, a)
        lines += [f"GOALIE vs SKATER: {g['name']} is a goalie, {sk['name']} is a {sk['primary']}. "
                  "They do different jobs, so this is who is better at his own job. Judge the "
                  "goalie ONLY on his goalie grades and the skater ONLY on his skater grades -- "
                  "never grade a goalie on scoring or a skater on save percentage.", ""]
    elif a["primary"] != b["primary"]:
        lines += [f"DIFFERENT POSITIONS: {a['name']} is a {a['primary']}, {b['name']} is a "
                  f"{b['primary']}. Each is graded on his own position's job -- a D-man is not "
                  "judged on goals. Say that; don't compare raw scoring as if they played the same spot.", ""]
    for s in (a, b):
        role = "goalie" if s["is_g"] else s["primary"]
        lines.append(f"{s['name']} AT HIS OWN JOB ({role}, {s['role_gp']:.0f} games there):")
        for r in s["job"]:
            if r["p"] is not None:
                lines.append(f"  {r['label']}: {tier(r['p']).lower()} ({_ordinal(r['p'])} percentile "
                             f"among {s['primary']}), rate {_fmt(r['key'], r['v'])}")
        lines.append("")
    if c["axes"]:
        lines.append("SKILL BY SKILL, HEAD TO HEAD (each ranked at his own position):")
        for ax in c["axes"]:
            if ax["pa"] is None or ax["pb"] is None:
                continue
            g_ = ax["pa"] - ax["pb"]
            who = "even" if g_ == 0 else f"{a['name'] if g_ > 0 else b['name']} +{abs(g_)}"
            lines.append(f"  {ax['label']}: {ax['pa']} vs {ax['pb']} -> {who}")
    for ex in c["extras"]:
        lines.append(f"SIDE NOTE, NOT PART OF THE VERDICT -- {ex['who']} also plays a side role "
                     f"({ex['gp']:.0f} games, {'on skates' if ex['kind'] == 'skates' else 'in net'}). "
                     "Head to head in that role:")
        for ax in ex["axes"]:
            if ax["pa"] is not None and ax["pb"] is not None:
                lines.append(f"  {ax['label']}: {a['name']} {tier(ax['pa']).lower()} vs "
                             f"{b['name']} {tier(ax['pb']).lower()}")
    if c["small_sample"]:
        lines.append(f"SMALL SAMPLE (under {EARLY_GP} games at his job): {', '.join(c['small_sample'])}.")
    return "\n".join(lines)


_RATE_UNIT = {"scoring": "goals/gm", "playmaking": "assists/gm", "impact": "+/- per gm",
              "physicality": "hits/gm", "discipline": "PIM/gm", "savepct": "sv%",
              "gaa": "GAA", "shutouts": "of games"}


def _rate(metric: str, v) -> str:
    """The raw rate under a percentile, with its unit, so 0.52 isn't a riddle."""
    return "" if v is None else f"{_fmt(metric, v)} {_RATE_UNIT.get(metric, '')}".strip()


def _per_gm(total: float, gp: float, signed: bool = False) -> tuple[str, float | None]:
    if not gp:
        return "-", None
    r = total / gp
    return (f"{r:+.2f}" if signed else f"{r:.2f}"), r


def _tape(s: dict, goalie: bool) -> list[dict]:
    """Tale-of-the-tape rows: (label, shown value, number to compare, small
    raw total, higher-is-better / lower / None). Every counting stat is a
    per-game rate -- a total is mostly a measure of games played, so 31
    shutouts in 168 games must not out-shout 10 in 74. The raw total rides
    along in small print for anyone who wants it."""
    m = s["m"]
    if goalie:
        gp = s["glgp"]
        so = ea._num(m.get("glso"))
        shots = (ea._num(m.get("glsaves")) + ea._num(m.get("glga"))) / gp if gp else 0
        sv, gaa = s["rates"].get("savepct", 0), s["rates"].get("gaa", 0)
        return [
            {"lbl": "GAMES IN NET", "val": f"{gp:.0f}", "num": None, "sub": "", "hi": None},
            {"lbl": "SAVE %", "val": _fmt("savepct", sv), "num": sv, "sub": "", "hi": True},
            {"lbl": "GAA", "val": _fmt("gaa", gaa), "num": gaa, "sub": "", "hi": False},
            {"lbl": "SHUTOUT RATE", "val": f"{so / gp * 100:.0f}%" if gp else "-",
             "num": so / gp if gp else None, "sub": f"{so:.0f} SO", "hi": True},
            {"lbl": "SHOTS FACED / GM", "val": f"{shots:.1f}", "num": None, "sub": "", "hi": None},
        ]
    gp = s["skater_gp"]
    g, a_ = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
    pm = ea._num(m.get("skplusmin"))
    rows = [{"lbl": "SKATER GAMES", "val": f"{gp:.0f}", "num": None, "sub": "", "hi": None}]
    for lbl, tot, signed, unit in (("POINTS / GAME", g + a_, False, "PTS"), ("GOALS / GAME", g, False, "G"),
                                   ("ASSISTS / GAME", a_, False, "A"), ("+/- PER GAME", pm, True, "+/-")):
        val, num = _per_gm(tot, gp, signed)
        sub = f"{tot:+.0f} {unit}" if signed else f"{tot:.0f} {unit}"
        rows.append({"lbl": lbl, "val": val, "num": num, "sub": sub, "hi": True})
    return rows


def _key_stats(s: dict) -> list[tuple[str, str]]:
    """The four numbers in a cross-role panel -- rates, never raw totals."""
    m = s["m"]
    if s["is_g"]:
        gp = s["glgp"]
        so = ea._num(m.get("glso"))
        return [("SV%", _fmt("savepct", s["rates"].get("savepct", 0))),
                ("GAA", _fmt("gaa", s["rates"].get("gaa", 0))),
                ("SO RATE", f"{so / gp * 100:.0f}%" if gp else "-"), ("GP", f"{gp:.0f}")]
    sg = s["skater_gp"]
    g, a_ = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
    return [("P/GP", _per_gm(g + a_, sg)[0]), ("G/GP", _per_gm(g, sg)[0]),
            ("A/GP", _per_gm(a_, sg)[0]), ("+/- /GP", _per_gm(ea._num(m.get("skplusmin")), sg, True)[0])]


# Panel geometry for the cross-role card.
_PR = 78          # mini radar radius
_PROW = 46        # one job row


def _job_panel_h(s: dict) -> int:
    return 48 + 2 * _PR + 70 + 72 + len(s["job"]) * _PROW + 12


def render_compare(c: dict, read: str | None = None) -> bytes:
    """Two players side by side. Header and the written take on top, then
    either the shared shape (same role) or two own-job panels (cross role),
    then one diverging gap bar per comparable skill."""
    a, b = c["a"], c["b"]
    f_kicker = _font("bold", 17); f_sub = _font("medium", 18)
    f_h = _font("bold", 16); f_val = _font("black", 16)
    f_read = _font("medium", 27); f_tape = _font("black", 30); f_tlbl = _font("bold", 15)
    f_gap = _font("black", 20); f_small = _font("bold", 14); f_tiny = _font("medium", 13)

    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    read_lines = _wrap(tmp, read or compare_headline(c), f_read, W - 2 * PAD - 24, max_lines=5)

    def ladder_rows(axes):
        return [ax for ax in axes if ax["pa"] is not None and ax["pb"] is not None]

    overall_row = ([{"key": "overall", "label": "OVERALL  ·  AT HIS OWN JOB", "va": None, "vb": None,
                     "pa": a["overall"], "pb": b["overall"]}]
                   if a["overall"] is not None and b["overall"] is not None else [])
    R, ROW_H = 132, 96
    tape_lbls = []
    if not c["cross"]:
        ta, tb = _tape(a, c["goalies"]), _tape(b, c["goalies"])
        tape_lbls = [r["lbl"] for r in ta]
        main_rows = ladder_rows(c["axes"]) + overall_row
        middle_h = (40 + len(tape_lbls) * 46 + 30) + (40 + 2 * R + 120) + (40 + len(main_rows) * ROW_H)
    else:
        main_rows = overall_row
        panel_h = max(_job_panel_h(a), _job_panel_h(b))
        middle_h = (40 + panel_h + 24) + len(main_rows) * ROW_H
    extras = [(ex, ladder_rows(ex["axes"])) for ex in c["extras"]]
    extras = [(ex, rows) for ex, rows in extras if rows]
    extra_h = sum(56 + len(rows) * ROW_H for _, rows in extras)

    H = (150 + 92 + len(read_lines) * 36 + 40 + middle_h + extra_h + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)
    mark = logo(54)
    if mark:
        img.paste(mark, (PAD, 32), mark)
    _text(d, (W - PAD, 53), "PUBS PLAYER COMPARE", f_kicker, DIM, anchor="ra")

    # ---- names, two columns
    y = 104
    half = (W - 2 * PAD) / 2 - 40
    for s, x, anchor, col in ((a, PAD, "la", BLUE_TEXT), (b, W - PAD, "ra", THEM)):
        for size in (40, 34, 28, 24, 20):
            f = _font("black", size)
            if d.textlength(s["name"], font=f) <= half:
                break
        _text(d, (x, y), s["name"], f, TEXT, anchor=anchor)
        sub = f"{s['primary']}  ·  {s['role_gp']:.0f} GP  ·  RANKED VS {s['primary']}"
        _text(d, (x, y + 48), sub, f_sub, col, anchor=anchor)
    _text(d, (W / 2, y + 22), "VS", _font("black", 24), DIM, anchor="mm")
    y += 92

    # ---- the take leads the card, with a rule in the winner's colour
    win_col = BLUE_TEXT if c["winner"] is a else THEM
    ry0 = y
    for ln in read_lines:
        _text(d, (PAD + 24, y), ln, f_read, TEXT)
        y += 36
    d.rounded_rectangle([PAD, ry0 + 2, PAD + 5, y - 6], radius=2, fill=win_col)
    if c["small_sample"]:
        _text(d, (W - PAD, y + 2), "SMALL SAMPLE: " + ", ".join(c["small_sample"]),
              f_small, AMBER, anchor="ra")
    y += 40

    def ladder(d, y, rows):
        cxm, half_w = W / 2, 200
        for ax in rows:
            pa, pb = ax["pa"], ax["pb"]
            d.rounded_rectangle([PAD, y, W - PAD, y + ROW_H - 10], radius=12, fill=(18, 21, 27))
            _text(d, (PAD + 20, y + 10), ax["label"], f_small, DIM)
            _text(d, (PAD + 20, y + 32), str(pa), f_gap, BLUE_TEXT)
            _text(d, (PAD + 20, y + 60), _rate(ax["key"], ax["va"]), f_tiny, DIM)
            _text(d, (W - PAD - 20, y + 32), str(pb), f_gap, THEM, anchor="ra")
            _text(d, (W - PAD - 20, y + 60), _rate(ax["key"], ax["vb"]), f_tiny, DIM, anchor="ra")
            ty = y + 52
            d.line([cxm - half_w, ty, cxm + half_w, ty], fill=(30, 34, 43), width=6)
            d.line([cxm, ty - 9, cxm, ty + 9], fill=(70, 76, 92), width=2)
            diff = pa - pb
            wpx = min(abs(diff), 40) / 40 * half_w
            if diff > 0:
                d.rounded_rectangle([cxm - wpx, ty - 5, cxm, ty + 5], radius=4, fill=BLUE)
            elif diff < 0:
                d.rounded_rectangle([cxm, ty - 5, cxm + wpx, ty + 5], radius=4, fill=THEM)
            if diff == 0:
                lab, col = "EVEN", DIM
            else:
                who = a if diff > 0 else b
                nm = who["name"] if len(who["name"]) <= 14 else who["name"][:13] + "…"
                lab, col = f"{nm} +{abs(diff)}", (BLUE_TEXT if diff > 0 else THEM)
            _text(d, (cxm, y + 16), lab, f_gap, col, anchor="ma")
            y += ROW_H
        return y

    if not c["cross"]:
        # ---- tale of the tape: label down the middle, his rate either side,
        # the better rate in his colour, the raw total in small print.
        f_sub_t = _font("medium", 13)
        _text(d, (PAD, y), "TALE OF THE TAPE  ·  PER GAME", f_h, DIM)
        y += 34
        d.rounded_rectangle([PAD, y - 6, W - PAD, y + len(tape_lbls) * 46 + 2], radius=12, fill=(18, 21, 27))
        for i, (ra, rb) in enumerate(zip(ta, tb)):
            ry = y + i * 46
            if i:
                d.line([PAD + 20, ry - 2, W - PAD - 20, ry - 2], fill=LINE)
            ca, cb = TEXT, TEXT
            if ra["hi"] is not None and ra["num"] is not None and rb["num"] is not None \
                    and round(ra["num"], 4) != round(rb["num"], 4):
                a_better = (ra["num"] > rb["num"]) == ra["hi"]
                ca, cb = (BLUE_TEXT, MUTED) if a_better else (MUTED, THEM)
            _text(d, (PAD + 28, ry + 21), ra["val"], f_tape, ca, anchor="lm")
            _text(d, (W - PAD - 28, ry + 21), rb["val"], f_tape, cb, anchor="rm")
            if ra["sub"]:
                wa = d.textlength(ra["val"], font=f_tape)
                _text(d, (PAD + 28 + wa + 12, ry + 26), ra["sub"], f_sub_t, DIM, anchor="lm")
                wb = d.textlength(rb["val"], font=f_tape)
                _text(d, (W - PAD - 28 - wb - 12, ry + 26), rb["sub"], f_sub_t, DIM, anchor="rm")
            _text(d, (W / 2, ry + 21), ra["lbl"], f_tlbl, MUTED, anchor="mm")
        y += len(tape_lbls) * 46 + 30

        # ---- the shape: both men overlaid
        axes = c["axes"]
        _text(d, (PAD, y), "SKILL SHAPE  ·  PERCENTILE AT HIS OWN POSITION", f_h, DIM)
        f14 = _font("bold", 14)
        nb, na = b["name"][:16], a["name"][:16]
        _text(d, (W - PAD, y + 10), nb, f14, MUTED, anchor="rm")
        bx = W - PAD - d.textlength(nb, font=f14) - 30
        d.rounded_rectangle([bx, y + 4, bx + 22, y + 16], radius=3, fill=THEM)
        _text(d, (bx - 14, y + 10), na, f14, MUTED, anchor="rm")
        ax_ = bx - 14 - d.textlength(na, font=f14) - 30
        d.rounded_rectangle([ax_, y + 4, ax_ + 22, y + 16], radius=3, fill=BLUE_TEXT)
        y += 40
        cx, cy = W / 2, y + 40 + R
        _radar_multi(img, cx, cy, R, [([ax["pb"] or 0 for ax in axes], THEM, THEM_FILL),
                                      ([ax["pa"] or 0 for ax in axes], BLUE_TEXT, US_FILL)])
        d = ImageDraw.Draw(img)
        n = len(axes)
        for i, ax in enumerate(axes):
            ang = -math.pi / 2 + 2 * math.pi * i / n
            dx, dy = math.cos(ang), math.sin(ang)
            x, yv = cx + dx * (R + 44), cy + dy * (R + 30)
            _text(d, (x, yv - 12), ax["label"], f14, DIM, anchor="ma")
            sa = "-" if ax["pa"] is None else str(ax["pa"])
            sb = "-" if ax["pb"] is None else str(ax["pb"])
            wa, wm = d.textlength(sa, font=f_val), d.textlength(" · ", font=f_val)
            x0 = x - (wa + wm + d.textlength(sb, font=f_val)) / 2
            _text(d, (x0, yv + 7), sa, f_val, BLUE_TEXT)
            _text(d, (x0 + wa, yv + 7), " · ", f_val, DIM)
            _text(d, (x0 + wa + wm, yv + 7), sb, f_val, THEM)
        y = cy + R + 80
        _text(d, (PAD, y), "SKILL BY SKILL  ·  bar grows toward the better man", f_h, DIM)
        y += 40
        y = ladder(d, y, main_rows)
    else:
        # ---- cross role: no shared scale, so each man gets his own panel --
        # his own shape, his key numbers, his job's grades -- side by side.
        _text(d, (PAD, y), "AT HIS OWN JOB  ·  EACH GRADED AGAINST HIS OWN POSITION", f_h, DIM)
        y += 40
        pw = (W - 2 * PAD - 20) / 2
        f_pt = _font("black", 18); f_kv = _font("black", 24); f_kl = _font("bold", 12)
        f_tw = _font("black", 15); f_rl = _font("bold", 14)
        for s, x0, col, fill in ((a, PAD, BLUE_TEXT, US_FILL), (b, PAD + pw + 20, THEM, THEM_FILL)):
            d.rounded_rectangle([x0, y, x0 + pw, y + panel_h], radius=14, fill=(18, 21, 27),
                                outline=col if c["winner"] is s else None, width=2)
            title = f"AS A {_POS_WORD.get(s['primary'], s['primary'])}"
            _text(d, (x0 + 20, y + 16), title, f_pt, col)
            _text(d, (x0 + pw - 20, y + 20), f"{s['role_gp']:.0f} GP", f_small, DIM, anchor="ra")
            ry = y + 48
            if len(s["radar"]) >= 3:
                pcx, pcy = x0 + pw / 2, ry + 34 + _PR
                _radar_multi(img, pcx, pcy, _PR, [([p for _, _, p in s["radar"]], col, fill)],
                             labels=[_SHORT[k] for _, k, _ in s["radar"]], f_lbl=f_kl)
                d = ImageDraw.Draw(img)
            ry += 2 * _PR + 70
            # key numbers, four across
            tw = (pw - 40) / 4
            for i, (lbl, val) in enumerate(_key_stats(s)):
                tx = x0 + 20 + tw * i + tw / 2
                _text(d, (tx, ry + 4), val, f_kv, TEXT, anchor="ma")
                _text(d, (tx, ry + 36), lbl, f_kl, DIM, anchor="ma")
            ry += 72
            for r in s["job"]:
                p = r["p"]
                _text(d, (x0 + 20, ry), r["label"], f_rl, MUTED)
                if p is not None:
                    _text(d, (x0 + pw - 20, ry - 1), f"{tier(p)}  {str(p)}", f_tw, _pole(p), anchor="ra")
                    bx0, bx1 = x0 + 20, x0 + pw - 20
                    d.rounded_rectangle([bx0, ry + 22, bx1, ry + 28], radius=3, fill=(30, 34, 43))
                    d.rounded_rectangle([bx0, ry + 22, bx0 + max((bx1 - bx0) * p / 100, 6), ry + 28],
                                        radius=3, fill=_pole(p))
                else:
                    _text(d, (x0 + pw - 20, ry - 1), "n/a", f_tw, DIM, anchor="ra")
                ry += _PROW
        y += panel_h + 24
        y = ladder(d, y, main_rows)

    # ---- where they overlap: a goalie who skates, a skater who plays net
    for ex, rows in extras:
        y += 6
        _text(d, (PAD, y), ex["title"].upper(), f_h, DIM)
        _text(d, (PAD, y + 22), "side note -- not part of the verdict", f_tiny, DIM)
        y += 50
        y = ladder(d, y, rows)

    fy = H - 56
    d.line([PAD, fy - 20, W - PAD, fy - 20], fill=LINE)
    _text(d, (PAD, fy), "chelscout.net", _font("bold", 17), BLUE_TEXT)
    _text(d, (W - PAD, fy), "SCOUT SMARTER. CHIRP RESPONSIBLY.", _font("bold", 17), DIM, anchor="ra")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
