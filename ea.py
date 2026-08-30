"""EA NHL Pro Clubs data.

EA fronts this API with Akamai, which fingerprints the TLS handshake -- plain
requests/httpx get a 403 no matter what headers you send. curl_cffi impersonates
Chrome's TLS stack, which gets through.
"""

import asyncio
import difflib
from concurrent.futures import ThreadPoolExecutor
import re
import time
from urllib.parse import quote

from curl_cffi import requests

MIN_QUERY = 4  # EA's search ignores anything shorter


class EAUnavailable(Exception):
    """Every EA call for a query failed.

    Distinct from "EA answered, nobody matched" -- the old code caught both
    and returned an empty list, so an EA outage, a rate limit, a changed
    endpoint and a genuinely unknown gamertag all surfaced to the user as the
    same flat "No player found."
    """

# Small TTL cache -- Discord autocomplete fires on every keystroke.
_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 120

BASE = "https://proclubs.ea.com/api/nhl"
PLATFORMS = ["common-gen5", "common-gen4"]

# EA's field prefix -> what a human calls the position.
POSITIONS = [
    ("cgp", "C"),
    ("lwgp", "LW"),
    ("rwgp", "RW"),
    ("dgp", "D"),
    ("glgp", "G"),
]


def _get(url: str, timeout: int = 20):
    r = requests.get(url, impersonate="chrome", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _savepct(m) -> float:
    """EA reports save% inconsistently -- sometimes 0.912, sometimes 91.2. Normalize to 0-1."""
    v = _num(m.get("glsavepct"))
    while v > 1:
        v /= 100
    return v


def _search_platforms(query: str, timeout: int = 20) -> tuple[list, int, int]:
    """Query every platform CONCURRENTLY and merge. Returns (hits, ok, failed).

    Autocomplete used to ask common-gen5 only, so a player whose club is on
    gen4 could never appear in the picker no matter what you typed -- which is
    exactly what "I can't find any of these guys" looked like. Both platforms
    are queried now, and in PARALLEL: Discord kills an autocomplete that takes
    longer than 3s, and two sequential calls blow that budget on their own.
    """
    def one(platform):
        try:
            data = _get(f"{BASE}/members/search?platform={platform}"
                        f"&memberName={quote(query)}", timeout=timeout)
            return platform, data.get("members", []) or []
        except Exception as e:
            print(f"[ea] search FAILED q={query!r} platform={platform}: "
                  f"{type(e).__name__}: {e}")
            return platform, None

    out, seen, ok, failed = [], set(), 0, 0
    with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
        for platform, members in pool.map(one, PLATFORMS):
            if members is None:
                failed += 1
                continue
            ok += 1
            for m in members:
                ident = (str(m.get("name")), platform)
                if ident in seen:
                    continue
                seen.add(ident)
                m["_platform"] = platform
                out.append(m)
    return out, ok, failed


def _all_hits(gamertag: str, fast: bool = False) -> list:
    """Every member EA returns for this query, across platforms. Cached briefly.

    fast=True is for Discord autocomplete, which dies after 3 seconds: one query,
    one platform, short timeout. The full walk is for the actual command.
    """
    key = gamertag.strip().lower()
    now = time.time()

    if fast:
        # Autocomplete fires on every keystroke, so query only the first
        # MIN_QUERY characters and re-rank the same result set locally as the
        # user keeps typing. EA's search is a prefix match, so the stem returns
        # a superset of anything the longer string would -- which means one
        # network call per stem, and every keystroke after it is a cache hit.
        stem = key[:MIN_QUERY]
        if len(stem) < MIN_QUERY:
            return []
        # Namespaced separately: these are single-platform, no prefix-walk
        # results, and must never satisfy a full search_player() lookup.
        ckey = f"fast:{stem}"
        cached = _cache.get(ckey)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
        hits, ok, failed = _search_platforms(stem, timeout=2)
        print(f"[ea] autocomplete {stem!r} -> {len(hits)} hits "
              f"({ok}/{ok + failed} platforms ok)")
        if not ok:
            # Every platform failed -- don't cache an outage as "no results".
            return []
        _cache[ckey] = (now, hits)
        return hits

    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    # ONE search. EA's endpoint is a prefix match with a 4-character minimum,
    # so the string actually typed is the query. The old code walked the
    # prefix back a character at a time -- for a 15-character tag that was up
    # to a dozen queries, each hitting both platforms, so ~24 calls against a
    # flaky API before admitting it found nothing. It bought one rare case
    # (typing MORE than the real tag, "Byfuglien33" -> "Byfuglien") and paid
    # for it on every single lookup.
    q0 = gamertag.strip()
    if len(q0) < MIN_QUERY:
        return []
    hits, ok_n, fail_n = _search_platforms(q0)
    attempted, failed = ok_n + fail_n, fail_n

    # Nothing got through at all -- that is an outage, not an empty result,
    # and must not be cached or reported as "no such player".
    if attempted and failed == attempted:
        raise EAUnavailable(f"all {attempted} EA calls failed for {gamertag!r}")

    print(f"[ea] search {gamertag!r} -> {len(hits)} hits "
          f"({attempted - failed}/{attempted} calls ok)"
          + (f" e.g. {[h.get('name') for h in hits[:5]]}" if hits else ""))
    _cache[key] = (now, hits)
    return hits


def _score(m: dict, want: str) -> tuple:
    """Rank candidates: exact, then prefix, then substring, then fuzzy, then games played.

    The fuzzy ratio is rounded to two decimals on purpose. Raw ratios almost
    never tie, so games played -- the thing that actually separates a real
    player from an abandoned account with the same-ish name -- never got to
    break anything. Rounding puts near-identical matches in the same bucket and
    lets the guy with 1,700 games sort above the one with zero.
    """
    name = str(m.get("name", "")).lower()
    exact = name == want
    prefix = name.startswith(want)
    contains = want in name
    ratio = round(difflib.SequenceMatcher(None, name, want).ratio(), 2)
    return (exact, prefix, contains, ratio, _num(m.get("gamesplayed")))


def _search_sync(gamertag: str):
    hits = _all_hits(gamertag)
    if not hits:
        return None
    return max(hits, key=lambda m: _score(m, gamertag.strip().lower()))


def _suggest_sync(query: str, limit: int = 20, fast: bool = False) -> list:
    """Ranked candidates for autocomplete / 'did you mean'."""
    hits = _all_hits(query, fast=fast)
    want = query.strip().lower()

    # The stem query returns a superset in theory, but EA caps how many members
    # it hands back -- so on a crowded stem ("chel", "goon") a longer tag can be
    # missing from it entirely. If nothing in the stem set actually starts with
    # what the user has typed, ask EA for the full string too and merge. Costs a
    # second call only in the case where the first one was insufficient.
    if fast and len(want) > MIN_QUERY and not any(
        str(m.get("name", "")).lower().startswith(want) for m in hits
    ):
        now = time.time()
        ckey = f"fast:{want}"
        cached = _cache.get(ckey)
        if cached and now - cached[0] < _CACHE_TTL:
            extra = cached[1]
        else:
            # Both platforms here too -- this is the branch that rescues a tag
            # missing from the crowded 4-char stem, so restricting it to gen5
            # meant a gen4 player stayed invisible even on an exact typed tag.
            extra, ok, _f = _search_platforms(want, timeout=2)
            print(f"[ea] autocomplete exact {want!r} -> {len(extra)} hits")
            if ok:
                _cache[ckey] = (now, extra)
        hits = hits + extra

    ranked = sorted(hits, key=lambda m: _score(m, want), reverse=True)

    seen, out = set(), []
    for m in ranked:
        name = str(m.get("name", ""))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(m)
        if len(out) >= limit:
            break
    return out


def label(m: dict) -> str:
    """One-line description for a suggestion list."""
    gp = _num(m.get("gamesplayed"))
    pos = {"center": "C", "leftWing": "LW", "rightWing": "RW", "defenseMen": "D", "goalie": "G"}.get(
        str(m.get("favoritePosition")), "?"
    )
    return f"{m.get('name')} — {pos}, {gp:.0f} GP"


async def search_player(gamertag: str):
    return await asyncio.to_thread(_search_sync, gamertag)


async def suggest(query: str, limit: int = 20, fast: bool = False) -> list:
    return await asyncio.to_thread(_suggest_sync, query, limit, fast)


# Grading bands for EASHL club play. Deterministic so the model can't invent a rating.
# Format: (threshold, grade) -- first band the value meets or beats wins.
SKATER_PPG_BANDS = [
    (3.0, "unreal"),
    (2.0, "elite"),
    (1.4, "very good"),
    (1.0, "solid"),
    (0.6, "average"),
    (0.3, "weak"),
    (0.0, "bad"),
]
# EASHL save% is compressed and runs far below real hockey. Calibrated by a goalie:
# .820 is the practical ceiling, .800 is a good goalie. Nobody is at .900.
GOALIE_SVPCT_BANDS = [
    (0.820, "elite"),
    (0.800, "good"),
    (0.770, "above average"),
    (0.730, "average"),
    (0.680, "weak"),
    (0.0, "bad"),
]
GOALIE_GAA_BANDS = [(2.0, "elite"), (2.75, "good"), (3.5, "above average"), (4.5, "average"), (6.0, "weak")]

# Secondary trait bands, per skater game. Same idea as the scoring bands: fixed
# thresholds so a grade is never the model's opinion.
HITS_BANDS = [
    (9.0, "unhinged"),
    (6.0, "extremely physical"),
    (4.0, "heavy"),
    (2.5, "physical"),
    (1.2, "middling"),  # NOT "average" -- that word is reserved for the scoring bands
    (0.0, "soft"),
]
PIM_BANDS = [(2.0, "undisciplined"), (1.0, "chippy"), (0.4, "normal"), (0.0, "clean")]
PM_BANDS = [
    (3.0, "absurd"),  # NOT "unreal" -- that word is reserved for the scoring bands
    (2.0, "massive"),
    (1.0, "strong"),
    (0.25, "positive"),
    (-0.25, "neutral"),
    (-1.0, "negative"),
    (-99, "liability"),
]
GOALS_SHARE = 55  # % of points from goals -> shooter vs playmaker

MIN_GAMES = 10  # below this the sample is too small to call


def _band(value, bands):
    for threshold, grade in bands:
        if value >= threshold:
            return grade
    return bands[-1][1]


def _band_index(value, bands):
    for i, (threshold, grade) in enumerate(bands):
        if value >= threshold:
            return i
    return len(bands) - 1


def standout_trait(m: dict):
    """Which skater trait is the most extreme (best or worst) for this guy.

    Every scout otherwise surveys production/physicality/discipline/impact in
    the same order, which is a big part of why they all read the same. This
    picks the one thing that's actually statistically notable about THIS
    player, so the narration has a reason to be about him specifically.
    """
    gp = _num(m.get("gamesplayed"))
    glgp = _num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    if skater_gp < MIN_GAMES:
        return None

    points = _num(m.get("skgoals")) + _num(m.get("skassists"))
    ppg = points / skater_gp
    hpg = _num(m.get("skhits")) / skater_gp
    pimpg = _num(m.get("skpim")) / skater_gp
    pmpg = _num(m.get("skplusmin")) / skater_gp

    traits = {
        "production": (ppg, SKATER_PPG_BANDS, f"{ppg:.1f} points/game"),
        "physicality": (hpg, HITS_BANDS, f"{hpg:.1f} hits/game"),
        "discipline": (pimpg, PIM_BANDS, f"{pimpg:.1f} PIM/game"),
        "impact": (pmpg, PM_BANDS, f"{pmpg:+.1f} plus/minus per game"),
    }
    best = None
    for name, (val, bands, detail) in traits.items():
        idx = _band_index(val, bands)
        mid = (len(bands) - 1) / 2
        extremity = abs(idx - mid) / mid if mid else 0
        grade = bands[idx][1]
        if best is None or extremity > best[0]:
            best = (extremity, name, grade, detail)
    return {"trait": best[1], "grade": best[2], "detail": best[3]}


# The model reaches for hype words ("elite", "unreal") as generic
# enthusiasm regardless of the actual tier, which defeats the point of having
# graded tiers at all -- testing showed real, repeated instances of this even
# after explicit prompt instructions not to. Since accuracy matters more than
# trusting model compliance here, any tier word STRICTLY BETTER than the real
# grade gets corrected back to the real grade in code -- same principle as
# computing the bands in Python instead of asking the model to grade.
# Deliberately one-directional (only catches inflation, not deflation): tier
# words like "average" appear in more than one trait's band list, so blindly
# correcting every mismatched word -- including ones worse than reality --
# clobbers a different trait's legitimate, correct use of the same word
# elsewhere in the same report. Word boundaries matter too: without them,
# replacing "physical" mangles "physicality" into "softity".
TRAIT_BANDS = {
    "production": SKATER_PPG_BANDS,
    "physicality": HITS_BANDS,
    "discipline": PIM_BANDS,
    "impact": PM_BANDS,
}


def enforce_grade_word(text: str, standout: dict | None) -> str:
    if not standout:
        return text
    bands = TRAIT_BANDS.get(standout["trait"])
    if not bands:
        return text
    words = [g for _, g in bands]
    correct = standout["grade"]
    if correct not in words:
        return text
    better_words = sorted({w for w in words[: words.index(correct)] if w != correct}, key=len, reverse=True)
    for wrong in better_words:
        text = re.sub(rf"\b{re.escape(wrong)}\b", correct, text, flags=re.IGNORECASE)
    return text


def _gaa_band(value):
    for threshold, grade in GOALIE_GAA_BANDS:
        if value <= threshold:
            return grade
    return "bad"


def grade_positions(m: dict) -> list[str]:
    """Return a deterministic verdict line per position, computed from the stats."""
    gp = _num(m.get("gamesplayed"))
    glgp = _num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    points = _num(m.get("skgoals")) + _num(m.get("skassists"))
    ppg = points / skater_gp if skater_gp else 0.0
    out = []

    goals = _num(m.get("skgoals"))
    assists = _num(m.get("skassists"))
    hits = _num(m.get("skhits"))
    hpg = hits / skater_gp if skater_gp else 0.0
    plusmin = _num(m.get("skplusmin"))
    grade = _band(ppg, SKATER_PPG_BANDS).upper()

    for key, pos in POSITIONS:
        n = _num(m.get(key))
        if n == 0:
            out.append(f"{pos}: 0 games. NEVER PLAYED THIS POSITION — no data exists, do not rate him here.")
            continue

        if pos == "G":
            svpct = _savepct(m)
            gaa = _num(m.get("glgaa"))
            saves = _num(m.get("glsaves"))
            ga = _num(m.get("glga"))
            shots = saves + ga
            detail = (
                f"{n:.0f} games in net, {svpct:.3f} save%, {gaa:.2f} GAA, "
                f"{saves:.0f} saves on {shots:.0f} shots, {ga:.0f} goals against, "
                f"{_num(m.get('glso')):.0f} shutouts, {shots / n:.1f} shots faced/gm. "
                f"Save% scale: .820+ elite, .800+ good, .770+ above average, .730+ average, "
                f"under .680 bad. GAA scale: 2.00 elite, 2.75 good, 3.50 above average, 4.50 average"
            )
            if n < MIN_GAMES:
                out.append(
                    f"G: HE HAS PLAYED {n:.0f} GAMES in net — under the {MIN_GAMES}-game minimum, so it is "
                    f"too small a sample to grade. Do NOT say he has never played goalie. ({detail})"
                )
            else:
                out.append(
                    f"G: {_band(svpct, GOALIE_SVPCT_BANDS).upper()} on save%, "
                    f"{_gaa_band(gaa).upper()} on GAA. ({detail})"
                )
            continue

        # Per-position lines carry ONLY games played. EA does not split scoring by
        # position, so putting the combined stat line here invites the model to
        # present career totals as production at one position.
        share = n / skater_gp * 100 if skater_gp else 0
        if n < 5:
            role = "BARELY PLAYED — he HAS played here, just not enough to say anything. Do NOT call this 'never played'"
        elif share >= 40:
            role = "THIS IS HIS POSITION — where he actually plays"
        elif share >= 10:
            role = "secondary position — plays here sometimes"
        else:
            role = "fill-in only"
        out.append(f"{pos}: {n:.0f} games ({share:.0f}% of his skating). {role}.")

    if skater_gp >= MIN_GAMES:
        pim = _num(m.get("skpim"))
        pimpg = pim / skater_gp
        pmpg = plusmin / skater_gp
        gshare = goals / points * 100 if points else 0
        style = "shooter" if gshare >= GOALS_SHARE else ("playmaker" if gshare <= 100 - GOALS_SHARE else "balanced")

        out.append(
            f"OVERALL SKATER GRADE: {grade} ({ppg:.1f} P/GP over {skater_gp:.0f} skater games). "
            f"Scale: elite 2.00+, very good 1.40+, solid 1.00+, average 0.60+, weak 0.30+."
        )
        out.append(
            f"  Production: {goals:.0f}G {assists:.0f}A {points:.0f}P — "
            f"{goals / skater_gp:.1f} goals/gm, {assists / skater_gp:.1f} assists/gm. "
            f"{gshare:.0f}% of his points are goals, so he is a {style.upper()}."
        )
        out.append(
            f"  Physicality: {_band(hpg, HITS_BANDS).upper()} — {hpg:.1f} hits/gm ({hits:.0f} total). "
            f"Scale: 6.0+ extremely physical, 4.0+ heavy, 2.5+ physical, 1.2+ middling, under that soft."
        )
        out.append(
            f"  Discipline: {_band(pimpg, PIM_BANDS).upper()} — {pimpg:.1f} PIM/gm ({pim:.0f} total). "
            f"Scale: 2.0+ undisciplined, 1.0+ chippy, 0.4+ normal, under that clean."
        )
        out.append(
            f"  Impact: {_band(pmpg, PM_BANDS).upper()} — {plusmin:+.0f} plus/minus, {pmpg:+.1f} per game. "
            f"Scale: +2.0/gm massive, +1.0 strong, +0.25 positive, -0.25 neutral, below that negative."
        )
        out.append(
            "  IMPORTANT: every number in this OVERALL block is ONE combined line EA reports across ALL "
            "his skater positions. It describes HIM AS A SKATER. It is NOT his production at any single "
            "position. Never attach these to one position's game count."
        )
    if gp < MIN_GAMES:
        out.append(f"WARNING: only {gp:.0f} total games on record. Everything above is a small sample.")
    return out


def pos_line(m: dict) -> str:
    """Games played at each position, e.g. 'C 2 · LW 2 · D 191 · G 4'."""
    parts = [f"{pos} {_num(m.get(key)):.0f}" for key, pos in POSITIONS if _num(m.get(key)) > 0]
    return " · ".join(parts) if parts else "no position data"


def stat_footer(m: dict) -> str:
    """Raw numbers under the verdict, so anyone can check the work."""
    gp = _num(m.get("gamesplayed"))
    glgp = _num(m.get("glgp"))
    skater_gp = max(gp - glgp, 0)
    points = _num(m.get("skgoals")) + _num(m.get("skassists"))
    bits = [
        f"{gp:.0f} GP",
        f"{_num(m.get('skgoals')):.0f}G {_num(m.get('skassists')):.0f}A {points:.0f}P",
    ]
    if skater_gp:
        bits.append(f"{points / skater_gp:.1f} P/GP")
        bits.append(f"{_num(m.get('skhits')) / skater_gp:.1f} hits/gm")
    bits.append(f"{_num(m.get('skplusmin')):+.0f}")
    if glgp:
        bits.append(f"G: {_savepct(m):.3f} SV% / {_num(m.get('glgaa')):.2f} GAA in {glgp:.0f}")
    return "EA data: " + " · ".join(bits)


def format_stats(m: dict) -> str:
    """Flatten a member record into a compact block for the model to read."""
    gp = _num(m.get("gamesplayed"))
    goals = _num(m.get("skgoals"))
    assists = _num(m.get("skassists"))
    points = goals + assists
    hits = _num(m.get("skhits"))
    glgp = _num(m.get("glgp"))

    lines = [
        f"Gamertag: {m.get('name')}",
        # Free-text field the player sets themselves -- often a joke. Not a club,
        # not a real name, not a fact about him.
        f"EA in-game name (user-chosen text, frequently a joke — never state it as a "
        f"fact about him and never call it his club): {m.get('skplayername')}",
        f"Platform: {m.get('_platform')}",
        f"Favorite position (EA): {m.get('favoritePosition')}",
        f"Total games played: {gp:.0f}",
        "",
        "GAMES PLAYED AT EACH POSITION:",
    ]
    for key, label in POSITIONS:
        n = _num(m.get(key))
        share = f" ({n / gp * 100:.0f}% of games)" if gp else ""
        lines.append(f"  {label}: {n:.0f} games{share}")

    # Raw totals only. Every per-game rate is computed once, in grade_positions,
    # off skater games -- duplicating them here with a different denominator is
    # how the model ended up printing 3.67 P/GP next to a footer saying 3.69.
    lines += [
        "",
        "SKATER RAW TOTALS (all skater positions combined -- EA does not split these per position):",
        f"  Goals: {goals:.0f}",
        f"  Assists: {assists:.0f}",
        f"  Points: {points:.0f}",
        f"  Plus/minus: {_num(m.get('skplusmin')):+.0f}",
        f"  Hits: {hits:.0f}",
        f"  PIM: {_num(m.get('skpim')):.0f}",
        "  (Per-game rates are in the PRE-COMPUTED VERDICTS below. Use those, not your own division.)",
    ]

    if glgp > 0:
        lines += [
            "",
            "GOALIE STATS:",
            f"  Games in net: {glgp:.0f}",
            f"  Save %: {_savepct(m):.3f}",
            f"  GAA: {_num(m.get('glgaa')):.2f}",
            f"  Goals against: {_num(m.get('glga')):.0f}",
            f"  Saves: {_num(m.get('glsaves')):.0f}",
            f"  Shutouts: {_num(m.get('glso')):.0f}",
        ]
    else:
        lines += ["", "GOALIE STATS: never played goalie."]

    return "\n".join(lines)
