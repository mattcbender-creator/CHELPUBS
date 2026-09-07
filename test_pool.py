"""The new-season changeover in build_pool.py, on synthetic players.

Run with `python test_pool.py` or pytest. No network: the sampler is never
called, only the floor-picking and fallback logic.
"""
import random

import build_pool as bp


def _player(pos_key, gp, seed):
    rng = random.Random(seed)
    m = {"name": f"p{seed}", "gamesplayed": str(gp), "glgp": "0", pos_key: str(gp),
         "skgoals": str(round(gp * rng.uniform(0.2, 1.5))),
         "skassists": str(round(gp * rng.uniform(0.2, 1.5))),
         "skhits": str(round(gp * rng.uniform(0.5, 4))), "skpim": str(round(gp * rng.uniform(0, 1))),
         "skplusmin": str(round(gp * rng.uniform(-0.5, 0.5)))}
    if pos_key == "glgp":
        m.update(gamesplayed=str(gp), glsaves=str(gp * 25), glga=str(gp * 2), glgaa="2.5",
                 glso="1", glsavepct="0.91")
    return m


def _season(c=0, lw=0, d=0, g=0, gp=30):
    seed = 0
    out = []
    for key, n in (("cgp", c), ("lwgp", lw), ("dgp", d), ("glgp", g)):
        for _ in range(n):
            seed += 1
            out.append(_player(key, gp, seed))
    return out


PREV = {"min_pool_gp": 50, "min_pool_glgp": 20, "min_primary_gp": 40,
        "counts": {"C": {"production": 1318}, "LW": {"production": 1176}, "RW": {"production": 143},
                   "D": {"production": 1337}, "F": {"production": 2637}, "G": {"savepct": 139}},
        "breakpoints": {p: {"production": list(range(101))} for p in ("C", "LW", "RW", "D", "F")}
                       | {"G": {"savepct": list(range(101))}},
        "meta": {"built": "2026-08-14", "season": "NHL 26",
                 "positions": {p: {"floor": 50, "source": "NHL 26", "n": 1} for p in ("C", "LW", "RW", "D", "F")}
                              | {"G": {"floor": 20, "source": "NHL 26", "n": 139}}}}


def test_launch_week_keeps_last_season():
    """Two days in: nobody has 15 games, every position stays on NHL 26."""
    pool = bp.assemble(_season(c=400, lw=300, d=300, g=100, gp=9), PREV)
    for pos, info in pool["meta"]["positions"].items():
        assert info["source"] == "NHL 26", (pos, info)
        assert pool["breakpoints"][pos] == PREV["breakpoints"][pos]
    assert pool["meta"]["season"] == "NHL 27"


def test_week_two_ramps_in_at_the_loosest_floor():
    """Everyone at 18 games: C/LW/D clear 150 at the 15 floor, RW and G don't."""
    pool = bp.assemble(_season(c=400, lw=300, d=300, g=100, gp=18), PREV)
    pos = pool["meta"]["positions"]
    for p in ("C", "LW", "D", "F"):
        assert pos[p]["source"] == "NHL 27" and pos[p]["floor"] == 15, (p, pos[p])
    assert pos["RW"]["source"] == "NHL 26"        # nobody sampled at RW -> fallback
    assert pos["G"]["source"] == "NHL 26"         # 100 goalies < 150 -> fallback
    assert pool["counts"]["C"]["production"] == 400
    assert pool["breakpoints"]["RW"] == PREV["breakpoints"]["RW"]


def test_floor_climbs_to_the_strictest_that_clears():
    """Mixed games: the strictest floor with 150 players wins, per position."""
    players = _season(c=200, gp=55) + _season(lw=200, gp=35) + _season(d=200, gp=22)
    pool = bp.assemble(players, PREV)
    pos = pool["meta"]["positions"]
    assert pos["C"]["floor"] == 50
    assert pos["LW"]["floor"] == 30
    assert pos["D"]["floor"] == 20
    # F follows C's floor: only the 55-game centres qualify at 50
    assert pos["F"]["floor"] == 50 and pool["counts"]["F"]["production"] == 200


def test_summary_reads_like_a_commit_line():
    pool = bp.assemble(_season(c=400, lw=300, d=300, g=100, gp=18), PREV)
    s = bp.summary(pool)
    assert "C 15+ (400)" in s and "RW NHL 26" in s and "G NHL 26" in s


def test_fallback_chains_labels_and_floor():
    """A position that never cleared keeps its ORIGINAL label, not 'last season'."""
    older = dict(PREV)
    older["meta"] = {"season": "NHL 27", "positions": dict(PREV["meta"]["positions"])}
    older["meta"]["positions"]["RW"] = {"floor": 50, "source": "NHL 25", "n": 143}
    pool = bp.assemble(_season(c=400, gp=9), older)
    assert pool["meta"]["positions"]["RW"] == {"floor": 50, "source": "NHL 25", "n": 143}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
