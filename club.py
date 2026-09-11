"""Shape an EA club (roster + season stats + recent matches) for the card
and the model. Everything here tolerates missing pieces: only the roster is
guaranteed, so record/goals/form are None or [] when EA didn't give them,
and the renderer skips the block.
"""
import re

import card
import ea

# EA's clubs/stats field names have moved between releases. Try each name in
# turn; the first that exists wins.
_W = ("wins", "w", "seasonWins", "totalWins")
_L = ("losses", "l", "seasonLosses", "totalLosses")
_OTL = ("otl", "ot", "otLosses", "seasonOtl", "ties")
_GF = ("goals", "goalsFor", "gf", "seasonGoals")
_GA = ("goalsAgainst", "ga", "seasonGoalsAgainst")


def _pick(d: dict | None, keys) -> float | None:
    if not d:
        return None
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return ea._num(d[k])
    return None


def _record_from_string(s) -> tuple[int, int, int] | None:
    """'14-6-2' -> (14, 6, 2). clubs/search hands the record over as text."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", str(s or ""))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _our_side(match: dict, club_id: str) -> tuple[dict | None, dict | None]:
    """(our entry, their entry) in one match record, whatever EA nested them under."""
    clubs = match.get("clubs")
    if not isinstance(clubs, dict):
        return None, None
    ours = clubs.get(str(club_id))
    theirs = next((v for k, v in clubs.items() if str(k) != str(club_id)), None)
    return (ours if isinstance(ours, dict) else None,
            theirs if isinstance(theirs, dict) else None)


def form(matches: list, club_id: str) -> list[str]:
    """Most recent first: 'W' / 'L' / 'OTL' per match, best effort.

    Score fields differ by release ('score', 'goals'); a result code isn't
    trusted because its meaning has changed too. Goals decide, and a loss
    where EA flags overtime/shootout counts as OTL.
    """
    out = []
    for m in matches:
        us, them = _our_side(m, club_id)
        if not us:
            continue
        gf = _pick(us, ("score", "goals", "gf"))
        # EA usually lists only the requesting club under "clubs" and puts
        # the other side's score on OUR entry as opponentScore.
        ga = _pick(us, ("opponentScore", "oppScore"))
        if ga is None and them:
            ga = _pick(them, ("score", "goals", "gf"))
        if gf is None or ga is None:
            continue
        if gf > ga:
            out.append("W")
        elif str(us.get("result", "")) in ("5", "6", "10") or ea._num(m.get("ot") or us.get("ot")) > 0:
            out.append("OTL")
        else:
            out.append("L")
    return out


def roster(members: list) -> tuple[list[dict], list[dict]]:
    """(skaters by points desc, goalies by games in net desc), each row
    carrying the same rates/primary/percentile the player card uses."""
    sk, gl = [], []
    for m in members:
        posns = card._positions(m)
        if not posns:
            continue
        primary = posns[0][0]
        rates = card._rates(m)
        gp = ea._num(m.get("gamesplayed"))
        glgp = ea._num(m.get("glgp"))
        row = {"m": m, "name": str(m.get("name") or "?"), "primary": primary,
               "gp": gp, "glgp": glgp, "rates": rates}
        if primary == "G":
            row["grade"] = card.percentile("G", "savepct", rates.get("savepct", 0)) if rates.get("savepct") else None
            gl.append(row)
        else:
            row["g"] = ea._num(m.get("skgoals"))
            row["a"] = ea._num(m.get("skassists"))
            row["pts"] = row["g"] + row["a"]
            row["pm"] = ea._num(m.get("skplusmin"))
            key = "impact" if primary == "D" else "production"
            row["grade"] = card.percentile(primary, key, rates[key]) if key in rates else None
            sk.append(row)
    sk.sort(key=lambda r: (-r["pts"], -r["gp"]))
    gl.sort(key=lambda r: -r["glgp"])
    return sk, gl


SHAPE_AXES = [("SCORING", "scoring"), ("PLAYMAKING", "playmaking"), ("PLUS/MINUS", "impact"),
              ("PHYSICALITY", "physicality"), ("DISCIPLINE", "discipline")]


def shape(skaters: list, goalies: list, top: int = 6, min_gp: int = 5) -> list[tuple[str, str, int]]:
    """Roster-shape radar axes: for each skill, the mean percentile of the
    top-N skaters by games played (each ranked at HIS position), plus the
    starting goalie's save% percentile. Same percentile() the player card
    uses, so a club's shape is made of the same grades its players carry."""
    core = [r for r in sorted(skaters, key=lambda r: -r["gp"]) if r["gp"] >= min_gp][:top]
    axes = []
    for label, key in SHAPE_AXES:
        ps = [card.percentile(r["primary"], key, r["rates"][key]) for r in core if key in r["rates"]]
        ps = [p for p in ps if p is not None]
        if ps:
            axes.append((label, key, round(sum(ps) / len(ps))))
    starter = next((g for g in goalies if g.get("grade") is not None and g["glgp"] >= min_gp), None)
    if starter:
        axes.append(("GOALTENDING", "savepct", starter["grade"]))
    return axes


def summarize(club: dict, detail: dict) -> dict:
    stats = detail.get("stats") or {}
    members = detail.get("members") or []
    matches = detail.get("matches") or []
    cid = str(club.get("clubId"))

    rec = _record_from_string(club.get("record"))
    w = _pick(stats, _W); l = _pick(stats, _L); otl = _pick(stats, _OTL)
    if rec and None in (w, l, otl):
        w, l, otl = rec
    gf, ga = _pick(stats, _GF), _pick(stats, _GA)
    gp = (w or 0) + (l or 0) + (otl or 0) if None not in (w, l) else None
    sk, gl = roster(members)
    return {
        "club_id": cid, "name": str(club.get("name") or "?"), "platform": club.get("_platform"),
        "division": club.get("currentDivision"),
        "w": w, "l": l, "otl": otl, "gp": gp, "gf": gf, "ga": ga,
        "form": form(matches, cid),
        "skaters": sk, "goalies": gl,
        "shape": shape(sk, gl),
        "n_members": len(members),
    }


def format_block(s: dict) -> str:
    """The club, flattened for the model. Raw facts only; the model's job is
    to say what they mean, not to recompute them."""
    lines = [f"Club: {s['name']}  (platform {s['platform']}, division {s['division'] or 'unknown'})"]
    if s["w"] is not None:
        lines.append(f"Record: {s['w']:.0f}-{s['l']:.0f}-{s['otl'] or 0:.0f} ({s['gp']:.0f} GP)")
    if s["gf"] is not None and s["ga"] is not None and s["gp"]:
        lines.append(f"Goals for {s['gf']:.0f} ({s['gf'] / s['gp']:.2f}/GP), against {s['ga']:.0f} "
                     f"({s['ga'] / s['gp']:.2f}/GP), diff {s['gf'] - s['ga']:+.0f}")
    if s["form"]:
        lines.append(f"Last {len(s['form'])}: {' '.join(s['form'])}")
    if s["shape"]:
        vocab = ", ".join(f"{floor}+ {word.lower()}" for floor, word in card.TIERS)
        lines.append("ROSTER SHAPE (mean percentile of the top-6 skaters at each skill, ranked vs their "
                     f"positions; goaltending = starter's save% percentile). Use the card's words: {vocab}.")
        for label, _, p in s["shape"]:
            lines.append(f"  {label}: {p}th ({card.tier(p)})")
    lines.append(f"ROSTER ({s['n_members']} members; skaters by points):")
    for r in s["skaters"][:8]:
        g = f" [{card.tier(r['grade'])} {r['grade']}th]" if r.get("grade") is not None else ""
        lines.append(f"  {r['name']} {r['primary']}: {r['gp']:.0f} GP, {r['g']:.0f}G {r['a']:.0f}A "
                     f"{r['pts']:.0f}P, {r['pm']:+.0f}{g}")
    for r in s["goalies"][:2]:
        sv = r["rates"].get("savepct", 0)
        g = f" [{card.tier(r['grade'])} {r['grade']}th]" if r.get("grade") is not None else ""
        lines.append(f"  {r['name']} G: {r['glgp']:.0f} GP, {sv:.3f} SV%, {r['rates'].get('gaa', 0):.2f} GAA{g}")
    return "\n".join(lines)


# ---------------------------------------------------------------- matchup
SLOTS = ["C", "LW", "RW", "D", "D"]
# Who lines up against whom at 5v5: a winger drives at the far-side D.
PAIRS = [("C", "C"), ("LW", "D2"), ("RW", "D1"), ("D1", "RW"), ("D2", "LW")]
ATTACK = {
    "scoring": "give him the shot, take away his pass",
    "playmaking": "he's a shooter -- cheat to the shot lane",
    "impact": "he's on the ice for goals against: attack his side",
    "physicality": "finish every check, he loses the boards",
    "discipline": "bait him -- he takes the penalty",
}
AXES = SHAPE_AXES  # scoring, playmaking, impact, physicality, discipline


def player_axes(r: dict) -> list[int | None]:
    return [card.percentile(r["primary"], k, r["rates"][k]) if k in r["rates"] else None
            for _, k in AXES]


def lineup(s: dict, min_gp: int = 5) -> dict[str, dict]:
    """The club's five by games played: one C, one LW, one RW, two D (D1 has
    more games). A missing slot takes the next-most-played skater of any
    position, so a club of five wingers still gets a lineup."""
    pool = [r for r in sorted(s["skaters"], key=lambda r: -r["gp"]) if r["gp"] >= min_gp]
    out, used = {}, set()
    for slot in SLOTS:
        pick = next((r for r in pool if r["primary"] == slot and id(r) not in used), None)
        if pick is None:
            pick = next((r for r in pool if id(r) not in used), None)
        if pick is None:
            continue
        used.add(id(pick))
        key = slot if slot != "D" else ("D1" if "D1" not in out else "D2")
        out[key] = pick
    return out


def pairings(us: dict, them: dict) -> list[dict]:
    """Our man against theirs, with the overall edge and where to attack."""
    out = []
    for ours, theirs in PAIRS:
        a, b = us.get(ours), them.get(theirs)
        if not a or not b:
            continue
        ax_a, ax_b = player_axes(a), player_axes(b)
        oa = [p for p in ax_a if p is not None]
        ob = [p for p in ax_b if p is not None]
        weak = min((i for i, p in enumerate(ax_b) if p is not None), key=lambda i: ax_b[i], default=None)
        strong = max((i for i, p in enumerate(ax_a) if p is not None), key=lambda i: ax_a[i], default=None)
        out.append({
            "slot": ours, "vs": theirs, "us": a, "them": b,
            "ax_us": ax_a, "ax_them": ax_b,
            "ov_us": round(sum(oa) / len(oa)) if oa else None,
            "ov_them": round(sum(ob) / len(ob)) if ob else None,
            "attack": weak, "lean": strong,
        })
    return out


def format_matchup(a: dict, b: dict, pairs: list) -> str:
    """Both clubs and the five pairings, flattened for the model's game plan."""
    def shape_line(s):
        return ", ".join(f"{lbl} {p}th ({card.tier(p)})" for lbl, _, p in s["shape"]) or "no shape (thin roster)"
    lines = [f"YOU: {a['name']} -- record {a.get('w')}-{a.get('l')}-{a.get('otl')}, shape: {shape_line(a)}",
             f"THEM: {b['name']} -- record {b.get('w')}-{b.get('l')}-{b.get('otl')}, shape: {shape_line(b)}",
             "", "5v5 PAIRINGS (our man vs theirs; percentiles vs their own positions):"]
    for p in pairs:
        an = ", ".join(f"{lbl} {v}" for (lbl, _), v in zip(AXES, p["ax_us"]) if v is not None)
        bn = ", ".join(f"{lbl} {v}" for (lbl, _), v in zip(AXES, p["ax_them"]) if v is not None)
        atk = f" ATTACK {AXES[p['attack']][0]} {p['ax_them'][p['attack']]}th: {ATTACK[AXES[p['attack']][1]]}" if p["attack"] is not None else ""
        lines.append(f"  our {p['slot']} {p['us']['name']} ({an}) vs their {p['vs']} {p['them']['name']} ({bn}).{atk}")
    return "\n".join(lines)
