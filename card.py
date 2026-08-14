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
    "G":  ["savepct", "gaa", "workload", "shutouts"],
}

# Used in the compact secondary block, where a long label runs into its value.
SHORT_LABELS = {"savepct": "SV%", "gaa": "GAA", "scoring": "GOALS",
                "playmaking": "ASSISTS", "impact": "+/-", "workload": "SAVES"}

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
            _text(d, (W - PAD, y + 21), _ordinal(p), f_small, DIM, anchor="ra")
    else:
        _text(d, (W - PAD, y + 2), "n/a", f_val, DIM, anchor="ra")
    _text(d, (x0 - 16, y + 2), _fmt(metric, value), f_val, MUTED, anchor="ra")
    return y + (36 if small else 50)


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
        if key == "workload" and not rates[key]:
            continue
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
         + 62           # bars header + legend
         + len(rows) * 52
         + 74)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 5], fill=BLUE)

    # ---- brand
    y = 44
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
    n_pool = max(pool().get("counts", {}).get(ref, {}).values() or [0])
    ref_name = "FORWARDS" if ref == "F" else primary
    _text(d, (PAD, y), f"RANKED VS {n_pool} {ref_name} WITH 50+ GAMES", f_statlbl, DIM)
    y += 24
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
