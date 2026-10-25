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
    spoken = spoken_name(s['name'])
    say = f' -- say the name "{spoken}"' if spoken.lower() != s['name'].lower() else ""
    lines = [f"Club: {s['name']}{say}  (platform {s['platform']}, division {s['division'] or 'unknown'})"]
    if s["w"] is not None:
        # Written out, not "50-4-1": a text-to-speech voice reads dashed
        # numbers as a date, and the model tends to echo whatever form it sees.
        lines.append(f"Record: {s['w']:.0f} wins, {s['l']:.0f} losses, {s['otl'] or 0:.0f} overtime losses "
                     f"({s['gp']:.0f} GP) -- say it like a broadcaster: "
                     f"\"{s['w']:.0f}, {s['l']:.0f} and {s['otl'] or 0:.0f}\"")
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
            lines.append(f"  {label}: {ordinal(p)} ({card.tier(p)})")
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


def ordinal(n: int) -> str:
    """92 -> '92nd', 22 -> '22nd', 11 -> '11th'. A voice reads '22th' aloud."""
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def story(s: dict) -> dict:
    """What the numbers MEAN, decided in code so a voice has a take to
    perform instead of a table to read. Everything here is derived from the
    same percentiles the card prints, so it can't disagree with the card."""
    shape = {key: p for _, key, p in s["shape"]}
    skills = {k: v for k, v in shape.items() if k != "savepct"}
    out = {}
    if skills:
        best_k = max(skills, key=skills.get)
        worst_k = min(skills, key=skills.get)
        best, worst = skills[best_k], skills[worst_k]
        hi = [k for k, v in skills.items() if v >= 78]
        lo = [k for k, v in skills.items() if v < 45]
        names = {"scoring": "finishing", "playmaking": "passing and setting up goals",
                 "impact": "outscoring teams while they're on the ice",
                 "physicality": "hitting and physical play", "discipline": "staying out of the box"}
        if len(hi) >= 3 and not lo:
            identity = "a complete team -- good at everything the card measures"
        elif "scoring" in hi or "playmaking" in hi:
            identity = "an offence-first team" + (", soft to play against" if "physicality" in lo else "")
        elif "impact" in hi and best_k == "impact":
            identity = "a team that wins the goals-for battle without flash"
        elif "physicality" in hi:
            identity = "a heavy, physical team" + (" that can't finish" if "scoring" in lo else "")
        elif best < 45:
            identity = "a below-average team at everything the card measures"
        else:
            identity = "a middle-of-the-pack team"
        out["identity"] = identity
        out["strength"] = f"{names[best_k]} ({card.tier(best).lower()}, {ordinal(best)} percentile)"
        out["weakness"] = (f"{names[worst_k]} ({card.tier(worst).lower()}, {ordinal(worst)} percentile)"
                           if worst < 62 else f"nothing on the card -- their lowest skill is still "
                                              f"{card.tier(worst).lower()}")
        out["game_plan"] = {
            "physicality": "lean on them, finish checks, make it a grind",
            "discipline": "draw them into penalties, they take them",
            "scoring": "make them beat you from the outside, they don't finish",
            "playmaking": "take away the pass, they can't create",
            "impact": "they bleed goals against -- trade chances with them",
        }[worst_k] if worst < 45 else "no soft spot on the card; play your game"
    if shape.get("savepct") is not None:
        g = shape["savepct"]
        out["goalie"] = f"{card.tier(g).lower()} ({ordinal(g)} percentile among goalies)"
    sk = [r for r in s["skaters"] if r.get("grade") is not None]
    if sk:
        top = max(sk, key=lambda r: r["grade"])
        role = {"C": "centre", "LW": "left winger", "RW": "right winger", "D": "defenceman"}.get(top["primary"], top["primary"])
        what = ("a goal scorer" if top["g"] > top["a"] * 1.2 else
                "a setup man" if top["a"] > top["g"] * 1.2 else "a producer both ways")
        out["best_player"] = (f"{spoken_name(top['name'])}, {role}, {what} "
                              f"({card.tier(top['grade']).lower()}, {ordinal(top['grade'])} at his position)")
        weak = min(sk, key=lambda r: r["grade"])
        if weak["grade"] < 45 and weak is not top:
            out["weak_link"] = (f"{spoken_name(weak['name'])}, {weak['primary']} "
                                f"({card.tier(weak['grade']).lower()}, {ordinal(weak['grade'])} at his position)")
    if s["w"] is not None and s["gp"]:
        pct = s["w"] / s["gp"]
        out["record"] = (f"{s['w']:.0f}, {s['l']:.0f} and {s['otl'] or 0:.0f} -- "
                         + ("they almost never lose" if pct >= 0.8 else
                            "a winning team" if pct >= 0.6 else
                            "around five hundred" if pct >= 0.45 else "a losing team"))
    if s["form"]:
        w = s["form"].count("W")
        out["form"] = f"{w} wins in the last {len(s['form'])}" + (", hot" if w >= len(s["form"]) - 1 else
                                                                    ", cold" if w <= 1 else "")
    return out


def voice_block(s: dict) -> str:
    """What a VOICE gets about a club. Leads with the story (code-written
    takes), then the facts as a reference it must not contradict. No tier
    vocabulary line: that was the card read's instruction, and every voice
    was obeying it -- "elite, elite, elite, bad" -- instead of talking."""
    spoken = spoken_name(s["name"])
    st = story(s)
    lines = [f"CLUB: {spoken}" + (f" (spelled {s['name']} -- always SAY \"{spoken}\")"
                                 if spoken.lower() != s["name"].lower() else ""),
             f"  division {s['division'] or 'unknown'}, {s['n_members']} members on EA's roster",
             "", "THE STORY (decided by the numbers -- this is your material):"]
    for k, label in (("identity", "What they are"), ("record", "Record"), ("form", "Form"),
                     ("strength", "What they do best"), ("weakness", "Where they're weakest"),
                     ("game_plan", "How you beat them"), ("goalie", "Their goalie"),
                     ("best_player", "Their best player"), ("weak_link", "Their weak link")):
        if k in st:
            lines.append(f"  {label}: {st[k]}")
    lines += ["", "FACTS (reference only -- never contradict these, never read them out):"]
    if s["gf"] is not None and s["ga"] is not None and s["gp"]:
        lines.append(f"  goals for {s['gf'] / s['gp']:.1f} a game, against {s['ga'] / s['gp']:.1f}")
    for label, _, p in s["shape"]:
        lines.append(f"  {label.lower()}: {card.tier(p).lower()} ({ordinal(p)})")
    for r in s["skaters"][:6]:
        g = f", {card.tier(r['grade']).lower()}" if r.get("grade") is not None else ""
        lines.append(f"  {spoken_name(r['name'])} ({r['primary']}): {r['gp']:.0f} games, "
                     f"{r['g']:.0f} goals {r['a']:.0f} assists{g}")
    for r in s["goalies"][:1]:
        g = f", {card.tier(r['grade']).lower()}" if r.get("grade") is not None else ""
        lines.append(f"  {spoken_name(r['name'])} (G): {r['glgp']:.0f} games{g}")
    return "\n".join(lines)


# ---------------------------------------------------------------- matchup
# EA doesn't split defence into left and right. LD is the D with more games,
# RD the next; the dropdowns on the card let anyone swap them.
SLOTS = ["LW", "C", "RW", "LD", "RD"]
# Who lines up against whom at 5v5: a winger drives at the far-side D.
PAIRS = [("LW", "RD"), ("C", "C"), ("RW", "LD"), ("LD", "RW"), ("RD", "LW")]
AXES = SHAPE_AXES  # scoring, playmaking, impact, physicality, discipline
# Matchup math ignores physicality: at the level using /matchup, hits are
# noise -- it stays on the radar as data but never drives an overall or a
# weakest-skill pick, and the card prescribes nothing (numbers only).
MATCH_KEYS = [i for i, (_, k) in enumerate(AXES) if k != "physicality"]


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
        want = "D" if slot in ("LD", "RD") else slot
        pick = next((r for r in pool if r["primary"] == want and id(r) not in used), None)
        if pick is None:
            pick = next((r for r in pool if id(r) not in used), None)
        if pick is None:
            continue
        used.add(id(pick))
        out[slot] = pick
    return out


def candidates(s: dict, limit: int = 25) -> list[dict]:
    """Everyone who could fill a slot, most games first -- the dropdowns."""
    return sorted(s["skaters"], key=lambda r: -r["gp"])[:limit]


def pairings(us: dict, them: dict) -> list[dict]:
    """Our man against theirs, with the overall edge and where to attack."""
    out = []
    for ours, theirs in PAIRS:
        a, b = us.get(ours), them.get(theirs)
        if not a or not b:
            continue
        ax_a, ax_b = player_axes(a), player_axes(b)
        oa = [ax_a[i] for i in MATCH_KEYS if ax_a[i] is not None]
        ob = [ax_b[i] for i in MATCH_KEYS if ax_b[i] is not None]
        weak = min((i for i in MATCH_KEYS if ax_b[i] is not None), key=lambda i: ax_b[i], default=None)
        strong = max((i for i in MATCH_KEYS if ax_a[i] is not None), key=lambda i: ax_a[i], default=None)
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
        atk = (f" His lowest graded skill: {AXES[p['attack']][0]} {p['ax_them'][p['attack']]}th percentile."
               if p["attack"] is not None else "")
        lines.append(f"  our {p['slot']} {p['us']['name']} ({an}) vs their {p['vs']} {p['them']['name']} ({bn}).{atk}")
    return "\n".join(lines)


_REC3 = re.compile(r"\b(\d{1,3})-(\d{1,3})-(\d{1,3})\b")
_REC2 = re.compile(r"\b(\d{1,3})-(\d{1,3})\b")


def clean_name(q: str) -> str:
    """A club name as the user meant it. Discord on mobile sometimes hands
    back the dropdown's display text instead of its value, so anything we
    ever decorated a label with (" — 50-4-1, div 2") is stripped here."""
    q = re.split(r"\s+[—–]\s+", q, maxsplit=1)[0]
    q = re.sub(r",\s*div\s+\S+$", "", q, flags=re.I)
    return q.strip()


_SLASHY = [(re.compile(r"\bplus\s*/\s*minus\b", re.I), "plus minus"),
           (re.compile(r"\+\s*/\s*-"), "plus minus"),
           (re.compile(r"\bP\s*/\s*GP\b"), "points per game"),
           (re.compile(r"\bSV\s*%"), "save percentage"),
           (re.compile(r"\bGAA\b"), "goals against average"),
           (re.compile(r"\bGP\b"), "games played")]


def spoken_name(name: str) -> str:
    """How a voice should SAY a club name. EA names are tags, not words:
    "6GANG7" gets spelled letter by letter by TTS. Split digits from
    letters ("6 GANG 7"), turn all-caps words of three or more letters into
    words ("Gang"; "LA" stays), and flatten _ . - to spaces. Returns the
    name unchanged when none of that applies."""
    n = re.sub(r"[_.]+", " ", str(name or ""))
    # leetspeak: a digit WRAPPED in letters inside one word is a letter
    # ("Pl0t" -> "Plot", "Sn1per" -> "Sniper"); a digit at the edge of a
    # word ("6GANG7", "PS5") is still a number
    n = re.sub(r"(?<=[A-Za-z])[013457](?=[A-Za-z])",
               lambda m: {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}[m[0]], n)
    n = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", n)
    n = re.sub(r"\b[A-Z]{3,}\b", lambda m: m[0].capitalize(), n)
    return re.sub(r"\s+", " ", n).strip()


def say_names(text: str, names) -> str:
    """say_name for several names at once (a club plus its roster, or both
    sides of a compare)."""
    for n in names:
        text = say_name(text, n)
    return text


def say_name(text: str, name: str) -> str:
    """Swap every mention of the raw club name in a script for the spoken
    form, so the voice reads "6 Gang 7" even if the model copied the tag."""
    spoken = spoken_name(name)
    if not name or spoken.lower() == str(name).lower():
        return text
    return re.sub(re.escape(str(name)), spoken, text, flags=re.I)


def speakable(text: str) -> str:
    """Make the script safe for a voice: '50-4-1' -> '50, 4 and 1' (dashed,
    a TTS voice reads it as a date); 'plus/minus' -> 'plus minus' (it was
    saying "plus slash minus"); the stat abbreviations spelled out."""
    for rx, rep_ in _SLASHY:
        text = rx.sub(rep_, text)
    text = _REC3.sub(lambda m: f"{m[1]}, {m[2]} and {m[3]}", text)
    # Two-part: a record ("41-9") is spoken "41 and 9"; a score ("3-2") is
    # left alone -- "three and two" is wrong and TTS reads "3-2" fine.
    text = _REC2.sub(lambda m: f"{m[1]} and {m[2]}" if int(m[1]) >= 10 else m[0], text)
    return text
