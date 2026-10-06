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
D_B = {"name": "Hoods003", "gamesplayed": "40", "glgp": "0", "skgoals": "4", "skassists": "17",
       "skplusmin": "-3", "skhits": "150", "skpim": "44", "dgp": "40"}
G_SKATES = {"name": "Benzy", "gamesplayed": "325", "glgp": "300", "glsaves": "7000", "glga": "700",
            "glgaa": "2.33", "glsavepct": "90.9", "glso": "30", "skgoals": "12", "skassists": "28",
            "skplusmin": "-13", "skhits": "87", "skpim": "32", "dgp": "25"}
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
        for r in s["job"]:
            assert r["p"] == card.percentile(s["primary"], r["key"], s["rates"][r["key"]])


def test_winner_is_named_and_consistent():
    for x, y in ((FWD_A, FWD_B), (FWD_B, FWD_A), (G_A, G_B), (FWD_A, G_SKATES)):
        c = card.compare_data(x, y)
        w, l = c["winner"], c["loser"]
        assert (w["overall"] or 0) >= (l["overall"] or 0)
        assert card.compare_headline(c).startswith(f"{w['name']} is better than {l['name']}")
        assert card.compare_headline(c) in card.format_compare(c)


def test_overall_is_his_own_job():
    """A D is graded on the D rows, a forward on the forward rows, a goalie
    on save % and GAA -- never shutouts, never the other man's skills."""
    c = card.compare_data(FWD_A, D_B)
    for s in (c["a"], c["b"]):
        keys = card.JOB_KEYS[s["primary"]]
        ps = [card.percentile(s["primary"], k, s["rates"][k]) for k in keys]
        assert s["overall"] == round(sum(ps) / len(ps))
    g = card.compare_data(G_A, G_B)["a"]
    assert [r["key"] for r in g["job"]] == ["savepct", "gaa", "shutouts"]
    assert g["overall"] == round((card.percentile("G", "savepct", g["rates"]["savepct"])
                                  + card.percentile("G", "gaa", g["rates"]["gaa"])) / 2)


def test_goalie_vs_skater_is_graded_on_each_job():
    """The goalie is judged as a goalie, not on his side-role skating; his
    skating shows up only as a side note head-to-head."""
    c = card.compare_data(FWD_A, G_SKATES)
    assert c["cross"] and not c["axes"]
    assert c["b"]["primary"] == "G" and [r["key"] for r in c["b"]["job"]][0] == "savepct"
    assert [ex["kind"] for ex in c["extras"]] == ["skates"]
    assert "GOALIE vs SKATER" in card.format_compare(c)
    # a pure goalie against a pure skater: no side note, still a verdict
    c = card.compare_data(G_A, FWD_A)
    assert c["cross"] and not c["extras"] and c["winner"]["name"] in ("Wall", "Williamson20")


def test_renders():
    for x, y in ((FWD_A, FWD_B), (G_A, G_B), (FWD_A, D_B), (FWD_A, G_SKATES), (G_A, FWD_A)):
        png = card.render_compare(card.compare_data(x, y), "A test read.")
        assert Image.open(io.BytesIO(png)).size[0] == card.W


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
