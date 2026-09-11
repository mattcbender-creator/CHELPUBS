"""Build the scout-board data pack from the harvest store, and render the
board HTML. The math mirrors club.py's pairing/percentile approach; every
number is computed here, the page only draws what it is handed.
"""

import collections
import json
import os
import time

import card
import club as clubmod
import ea
import harvest

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoutboard.html")
POSMAP = {"center": "C", "leftWing": "LW", "rightWing": "RW", "defenseMen": "D", "goalie": "G"}


def _rows(store: dict) -> dict:
    """name -> list of per-match stat rows, from every harvested match."""
    rows = collections.defaultdict(list)
    for m in store["matches"].values():
        mt = "private" if m.get("_matchType") == "club_private" else "public"
        for pclub, plist in (m.get("players") or {}).items():
            for p in plist.values():
                nm = p.get("playername")
                if not nm:
                    continue
                f = lambda k: ea._num(p.get(k, 0))
                rows[nm].append({
                    "mt": mt, "club": pclub, "pos": POSMAP.get(p.get("position"), "?"),
                    "g": f("skgoals"), "a": f("skassists"), "poss": f("skpossession"),
                    "passa": f("skpassattempts"), "passc": f("skpasses"),
                    "gva": f("skgiveaways"), "tka": f("sktakeaways"), "ints": f("skinterceptions"),
                    "fow": f("skfow"), "fol": f("skfol"), "pdr": f("skpenaltiesdrawn"),
                    "pim": f("skpim"), "hits": f("skhits"), "satt": f("skshotattempts"),
                    "sog": f("skshots"), "pm": f("skplusmin"),
                    "glsaves": f("glsaves"), "glshots": f("glshots"), "glga": f("glga"),
                })
    return rows


def _agg(rs: list) -> dict:
    sk = [r for r in rs if r["pos"] != "G"]
    out = {"gp": len(rs)}
    if sk:
        n = len(sk)
        tp = sum(r["poss"] for r in sk)
        out.update(
            gpg=round(sum(r["g"] for r in sk) / n, 2), apg=round(sum(r["a"] for r in sk) / n, 2),
            poss=round(tp / n),
            passpct=round(100 * sum(r["passc"] for r in sk) / max(1, sum(r["passa"] for r in sk)), 1),
            gva60=round(sum(r["gva"] for r in sk) / max(tp, 1) * 3600, 1),
            fow=int(sum(r["fow"] for r in sk)), fol=int(sum(r["fol"] for r in sk)),
        )
    gl = [r for r in rs if r["pos"] == "G"]
    if gl:
        sv = sum(r["glsaves"] for r in gl)
        sh = sum(r["glshots"] for r in gl)
        if sh < sv:  # some releases report saves, not shots
            sh = sv + sum(r["glga"] for r in gl)
        out.update(gl_gp=len(gl), gl_sv=int(sv), gl_sh=int(sh),
                   gl_svpct=round(sv / max(sh, 1), 3), gl_ga=int(sum(r["glga"] for r in gl)))
    return out


def _likely(store: dict, cid: str) -> dict:
    slots = collections.defaultdict(collections.Counter)
    games = 0
    for m in store["matches"].values():
        plist = (m.get("players") or {}).get(cid, {})
        if not plist:
            continue
        sk = [(p.get("playername"), POSMAP.get(p.get("position"), "?"))
              for p in plist.values() if p.get("position") != "goalie"]
        if len(sk) != 5:  # uneven game -- not a real lineup
            continue
        games += 1
        for n, pos in sk:
            slots[n][pos] += 1
        for p in plist.values():
            if p.get("position") == "goalie":
                slots[p.get("playername")]["G"] += 1
    return {"slots": {n: dict(c) for n, c in slots.items()}, "games_seen": games}


def _player(nm: str, m: dict, rows: dict) -> dict | None:
    if not m:
        return None
    posns = card._positions(m)
    rates = card._rates(m)
    primary = posns[0][0] if posns else "?"
    gp = ea._num(m.get("gamesplayed"))
    glgp = ea._num(m.get("glgp"))
    skgp = max(gp - glgp, 0)
    axes = {}
    for _, k in clubmod.AXES:
        if k in rates:
            p = card.percentile(primary if primary != "G" else "D", k, rates[k])
            if p is not None:
                axes[k] = p
    g_, a_ = ea._num(m.get("skgoals")), ea._num(m.get("skassists"))
    conf = "LOW" if gp < 5 else ("MODERATE" if gp < 10 else ("SOLID" if gp < 15 else "HIGH"))
    priv = _agg([r for r in rows.get(nm, []) if r["mt"] == "private"])
    alll = _agg(rows.get(nm, []))
    d = {"name": m.get("name"), "primary": primary, "gp": int(gp),
         "g": int(g_), "a": int(a_), "ppg": round((g_ + a_) / skgp, 2) if skgp else 0,
         "pm": int(ea._num(m.get("skplusmin"))),
         "hitspg": round(ea._num(m.get("skhits")) / skgp, 1) if skgp else 0,
         "pimpg": round(ea._num(m.get("skpim")) / skgp, 2) if skgp else 0,
         "axes": axes, "conf": conf,
         "gl": ({"gp": int(glgp), "svpct": rates.get("savepct"),
                 "gaa": ea._num(m.get("glgaa"))} if glgp else None),
         "form_priv": priv, "form_all": alll, "form_pub": _agg([r for r in rows.get(nm, []) if r["mt"] == "public"])}
    if d["gl"] and d["gl"]["svpct"]:
        d["gl"]["pct"] = card.percentile("G", "savepct", d["gl"]["svpct"])
    return d


def _defaults(likely: dict, players: dict) -> dict:
    slots = likely["slots"]

    def top(pos, used):
        cands = [(s.get(pos, 0), players.get(n, {}).get("gp", 0), n)
                 for n, s in slots.items() if s.get(pos, 0) > 0 and n in players and n not in used]
        return max(cands)[2] if cands else None

    used, out = set(), {}
    for sl in ("LW", "C", "RW"):
        p = top(sl, used)
        if p:
            out[sl] = p
            used.add(p)
    ds = sorted([(s.get("D", 0), players.get(n, {}).get("gp", 0), n)
                 for n, s in slots.items() if s.get("D", 0) > 0 and n in players and n not in used],
                reverse=True)
    if ds:
        out["LD"] = ds[0][2]
        used.add(ds[0][2])
    if len(ds) > 1:
        out["RD"] = ds[1][2]
        used.add(ds[1][2])
    g = top("G", used)
    if g:
        out["G"] = g
    return out


def build_pack(cid_a: str, cid_b: str) -> dict:
    store = harvest.load_store()
    rows = _rows(store)
    players = {}
    for nm, m in store["careers"].items():
        p = _player(nm, m, rows)
        if p:
            players[nm] = p
    pack = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "clubs": {}, "players": players}
    for cid in (cid_a, cid_b):
        lk = _likely(store, cid)
        pool = sorted(n for n in lk["slots"] if n in players)
        pack["clubs"][cid] = {"default": _defaults(lk, players), "pool": pool,
                              "games_seen": lk["games_seen"]}
    return pack


def render(cid_a: str, cid_b: str, title: str) -> str:
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    pack = build_pack(cid_a, cid_b)
    return html.replace("__TITLE__", title).replace("__DATA__", json.dumps(pack, separators=(",", ":")))


if __name__ == "__main__":
    out = render("12521", "22423", "Wildman vs Entourage")
    with open("board_test.html", "w", encoding="utf-8") as f:
        f.write(out)
    print(f"board_test.html written ({len(out)} bytes)")
