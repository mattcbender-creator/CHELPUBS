"""Checks that the radar on the player card says the same thing as the bars.

Run with `python test_radar.py` (no pytest needed) or `pytest test_radar.py`.
Uses the bundled percentile pool, so a card rendered here is graded exactly
as production grades it.
"""
import io
import math

from PIL import Image, ImageDraw

import card

OUT = None  # set to a directory to also write the rendered PNGs


def _close(a, b, tol=1.0):
    return abs(a - b) <= tol


def test_geometry():
    """100 sits on the outer ring, 50 halfway, 0 at the centre; axis 0 is up."""
    pts = card.radar_points(100, 100, 40, [100, 50, 0, 100, 100])
    assert _close(pts[0][0], 100) and _close(pts[0][1], 60)          # straight up, r away
    assert _close(math.dist(pts[1], (100, 100)), 20)                  # 50 -> r/2
    assert _close(math.dist(pts[2], (100, 100)), 0)                   # 0 -> centre
    for p in pts[3:]:
        assert _close(math.dist(p, (100, 100)), 40)
    # clamps: nothing can leave the ring or go negative
    over = card.radar_points(0, 0, 40, [150, -20])
    assert _close(math.dist(over[0], (0, 0)), 40) and _close(math.dist(over[1], (0, 0)), 0)


def _rates_and_axes(m):
    gp, glgp = card.ea._num(m.get("gamesplayed")), card.ea._num(m.get("glgp"))
    posns = card._positions(m)
    primary = posns[0][0] if posns else "?"
    rates = card._rates(m)
    return primary, rates, card.radar_axes(primary, rates, primary == "G")


FWD = {"name": "xBobby92-", "gamesplayed": "9", "glgp": "0", "skgoals": "15", "skassists": "18",
       "skplusmin": "4", "skhits": "20", "skpim": "0", "lwgp": "6", "rwgp": "3"}
DEF = {"name": "Hoods003", "gamesplayed": "40", "glgp": "0", "skgoals": "4", "skassists": "17",
       "skplusmin": "-3", "skhits": "150", "skpim": "44", "dgp": "40"}
GOALIE = {"name": "Wall", "gamesplayed": "40", "glgp": "40", "glsaves": "900", "glga": "80",
          "glgaa": "2.10", "glso": "5"}


def test_axes_match_bars():
    """Every axis the bars also show carries the identical percentile."""
    for m in (FWD, DEF, GOALIE):
        primary, rates, axes = _rates_and_axes(m)
        by_key = {k: p for _, k, p in axes}
        for key in card.ROWS_BY_POS[primary]:
            if key not in rates:
                continue
            bar_p = card.percentile(primary, key, rates[key])
            assert by_key[key] == bar_p, (m["name"], key, by_key[key], bar_p)
        # fixed order, and every axis is a real graded skill
        expect = card.RADAR_AXES["G" if primary == "G" else "skater"]
        assert [k for _, k, _ in axes] == [k for k in expect if k in by_key]
        assert all(0 <= p <= 100 for p in by_key.values())


def _render(m, debug):
    png = card.render(m, "A test read.", _debug=debug)
    return Image.open(io.BytesIO(png)).convert("RGB")


def test_drawn_where_the_numbers_say():
    """The vertex dot is drawn at exactly radius * percentile / 100."""
    for m in (FWD, DEF, GOALIE):
        dbg = {}
        img = _render(m, dbg)
        if OUT:
            img.save(f"{OUT}/radar_{m['name']}.png")
        r = dbg["radar"]
        assert len(r["axes"]) >= 3
        for (lbl, key, p), (x, y) in zip(r["axes"], r["points"]):
            want = r["r"] * max(p, 3) / 100
            assert _close(math.dist((x, y), (r["cx"], r["cy"])), want), (lbl, p)
            # the pixel under the vertex is the dot colour, not background
            assert img.getpixel((round(x), round(y))) == card.BLUE_TEXT, (lbl, p, img.getpixel((round(x), round(y))))


def test_labels_fit_the_card():
    """The longest possible label on every axis stays inside the margins."""
    f_lbl, f_word = card._font("bold", 16), card._font("bold", 19)
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    worst_word = max((f"{w}  {card._ordinal(100)}" for _, w in card.TIERS),
                     key=lambda s: d.textlength(s, font=f_word))
    for kind, keys in card.RADAR_AXES.items():
        n = len(keys)
        cx, cy = card.W / 2, 0
        for i, key in enumerate(keys):
            a = -math.pi / 2 + 2 * math.pi * i / n
            dx = math.cos(a)
            x = cx + card.RADAR_R * dx + dx * 34
            wid = max(d.textlength(card.LABELS[key], font=f_lbl), d.textlength(worst_word, font=f_word))
            if dx > 0.3:
                assert x + wid <= card.W - card.PAD, (kind, key, x + wid)
            elif dx < -0.3:
                assert x - wid >= card.PAD, (kind, key, x - wid)
            else:
                assert x - wid / 2 >= card.PAD and x + wid / 2 <= card.W - card.PAD, (kind, key)


if __name__ == "__main__":
    import sys
    OUT = sys.argv[1] if len(sys.argv) > 1 else None
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
