"""Checks for /pubcompare's data and card. Run with `python test_compare.py`."""
import io

from PIL import Image

import card

FWD_A = {"name": "Williamson20", "gamesplayed": "115", "glgp": "0", "skgoals": "250",
         "skassists": "190", "skplusmin": "90", "skhits": "200", "skpim": "60",
         "lwgp": "100", "cgp": "15"}
FWD_B = {"name": "x Fisher 81", "gamesplayed": "38", "glgp": "0", "skgoals": "33",
         "skassists": "66", "skplusmin": "20", "skhits": "150", "skpim": "30",
         "rwgp": "30", "dgp": "8"}
G_A = {"name": "Wall", "gamesplayed": "40", "glgp": "40", "glsaves": "900", "glga": "80",
       "glgaa": "2.10", "glsavepct": "91.8", "glso": "5"}
G_B = {"name": "jtrim94", "gamesplayed": "55", "glgp": "55", "glsaves": "1100", "glga": "160",
       "glgaa": "3.00", "glsavepct": "87.3", "glso": "2"}


def test_grades_match_each_players_own_card():
    """Every percentile on the compare card is the one his /pubscout card shows."""
    c = card.compare_data(FWD_A, FWD_B)
    for s, side in ((c["a"], "pa"), (c["b"], "pb")):
        for ax in c["axes"]:
            assert ax[side] == card.percentile(s["primary"], ax["key"], s["rates"][ax["key"]])


def test_winner_is_named_and_consistent():
    for x, y in ((FWD_A, FWD_B), (FWD_B, FWD_A), (G_A, G_B)):
        c = card.compare_data(x, y)
        w, l = c["winner"], c["loser"]
        assert (w["overall"] or 0) >= (l["overall"] or 0)
        assert card.compare_headline(c).startswith(f"{w['name']} is better than {l['name']}")
        assert f"{w['name']} is the better player" in card.format_compare(c)


def test_overall_ignores_physicality():
    c = card.compare_data(FWD_A, FWD_B)
    ps = [ax["pa"] for ax in c["axes"] if ax["key"] != "physicality"]
    assert c["a"]["overall"] == round(sum(ps) / len(ps))


def test_goalie_vs_pure_skater_is_refused():
    assert "error" in card.compare_data(G_A, FWD_A)


def test_renders():
    for x, y in ((FWD_A, FWD_B), (G_A, G_B)):
        png = card.render_compare(card.compare_data(x, y), "A test read.")
        assert Image.open(io.BytesIO(png)).size[0] == card.W


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
