"""Build the percentile pool used by the minicard.

The bot runs this itself, on Railway: weekly, and at boot when the pool on
its volume is older than a week (bot.py, pool_rebuild_loop). EA's Akamai
front blocks datacenter IPs like GitHub's runners, but it answers Railway --
that is the IP the bot already talks to it from. From a shell:

    python build_pool.py            # writes pool.json (reuses players_raw.json)
    python build_pool.py --refresh  # re-samples EA first

HOW A NEW SEASON CHANGES OVER. On launch day nobody has 50 games, so a fixed
floor would produce an empty pool. Instead every position tries the floors
in FLOORS from strictest to loosest and keeps the strictest one that still
yields MIN_POOL_N players. A position that clears nothing keeps the bands it
had in the previous pool.json -- last season's curve -- and is labelled as
such in pool["meta"], which the card prints. As the weeks pass and people
accumulate games the chosen floor climbs back to 50 on its own, and the
previous-season fallback disappears position by position. Nobody has to
remember to flip anything.

The pool deliberately EXCLUDES low-game accounts. Pubs is full of abandoned
profiles with a handful of games, and leaving them in would drag every
percentile down until a mediocre regular looked elite. Filtering to players
with real minutes makes the scale mean "good among people who actually play",
which is a harder and more honest bar.
"""
import json
import os
import sys
import time

import ea

# Game floors to enter the pool, strictest first. 50 is the standard: below it
# the pool fills with abandoned accounts and a mediocre regular looks elite.
# The looser floors exist ONLY for the first weeks of a new season, and each
# position moves back up the list automatically as soon as it can.
FLOORS = [50, 40, 30, 20, 15]
GOALIE_FLOORS = [20, 16, 12, 10, 8]  # goalies play fewer games: their own ladder, same length
# A position needs this many players at a floor before that floor counts. The
# same number card.py uses to decide a pool is too thin to trust.
MIN_POOL_N = 150

# A player also has to have real time AT the position he's ranked in. Someone
# with 300 games who took 4 shifts on D should not be sitting in the D pool
# dragging it around. Scales with the floor: 80% of it.
def primary_floor(gp_floor: int) -> int:
    return max(6, round(gp_floor * 0.8))

# Kept for the pool.json keys older code reads; the per-position truth is in
# pool["meta"]["positions"].
MIN_POOL_GP = FLOORS[0]
MIN_POOL_GLGP = GOALIE_FLOORS[0]
MIN_PRIMARY_GP = primary_floor(FLOORS[0])

# Labels for the card. The season being sampled, and what to call whatever
# the previous pool.json was if it carries no label of its own.
SEASON = os.getenv("POOL_SEASON", "NHL 27")
PREV_SEASON = os.getenv("POOL_PREV_SEASON", "NHL 26")

# A sample smaller than this is not a population, it is EA blocking us (a
# rate limit, an endpoint change). Abort without touching the pool rather
# than write a curve built from 40 people.
MIN_SAMPLE = int(os.getenv("POOL_MIN_SAMPLE", "500"))

# One request at a time, ~0.75s apart: 1,136 stems x 2 calls is ~30-40
# minutes. Slow is the point -- see collect(). Env-overridable for tuning
# from Railway without a deploy.
PAUSE = float(os.getenv("POOL_PAUSE", "0.75"))
# On a 403, sleep this long and try the stem once more; a second 403 in a
# row means the IP is throttled and the run stops to protect it.
BACKOFF_403 = float(os.getenv("POOL_BACKOFF_403", "180"))

# Percentiles are computed per position, because the same rate means different
# things at different spots -- 1.4 points a game is ordinary for a centre and
# excellent for a defenceman. Scoring in the EA data is COMBINED across a
# player's skater positions, so a player is placed in exactly one pool: the
# position he actually plays most.
SKATER_POS = ["C", "LW", "RW", "D"]

# EA's search is a prefix match needing 4+ characters, so the population is
# sampled by querying many stems. These are chosen to spread across how people
# actually name themselves -- given names, hockey words, gamer-tag filler --
# rather than to be exhaustive.
STEMS = """
aaro adam alex andr anth aust bake barn bear beau beck bell benz bill blad blak
bobb bond boom brad brady brer brew broc brok brow bruh bryc buck budd burn bush
butc byfu cade cain cale camp capt carl cart case cash chad chan chas chel chip
chri clar clay coch cole conn cook coop corb cory crai cros crus cuck culy curt
cyde dale dalt dang dani dark dave davi dean deke delt demo denn dent derr dest
devi dice dill dirt dobb doge dolo domi donn doug drag drak dram drew drop duke
dunc dust dyla eagl earl east eddy edge elit elli emer erik evan ever fade fain
falc fang farm fast feed fell finn fire fish flam flas flip flow foco forb fost
foxx fran fred frog fros full funk fury gabe gage gale game gard garr gary gate
gavi gear geno geor ghos gibb gift gilb glen glid goal godd gold golf gone gonz
good goon gord gore grab grac graf gran gray greg grey grif grim grin grit gron
guns gunn hack hail hale hall hami hamm hand hank hard harl harp harr hart hawk
haze heat heck hell henr herb hero hers hidd high hill hitt hock hodg hogg holl
holm hond hook hoop hopp horn hose hous howe hube hugh hulk hunt huss hutc iceb
icem inju iron isaa jace jack jaco jade jake jame jaso jayd jean jeff jenk jenn
jerr jess jimm jock joel john joke jona jord jose josh juan judd juic jump junk
just kane karl kase keeg keit kell kemp kend kenn kent kerr kevi kick kidd kill
king kirb kirk kiss kite klei knig knox koch kris kyle lace lain lake lamb lamp
land lane lang lars lash last laug lawr lazy lead leaf lear ledg lega legi lemi
leon leve lewi liam libe lift ligh lily lima lind line lion litt live lloy loca
lock logi lone long loop lord lose loud love lowe luca luck luke lump lund lynn
lyon mack macl madd mags main majo make mali malo mann mans mapl marc mark marl
mars mart marv mase mass mast matt maul maus maxx mayb mayo mcca mcco mcda mcdo
mcgr mcka mead meat medi mega melo memp mend menz merc mere merr mess meta mice
mick midd migh mike mill milo mine mini mink mint mira mitc mode mogu moha moli
monk mont moon moor mora more morg morr mose moss moto moun move mrpu much muel
mull munn murp murr muse musk myer myst nail nash nate nava neal neil nels neon
nest neve newm nick nigh nike nils nino nitr noah noel nolan nord norm nort nova
nuke nurs nutt oakl oats obri ocho odel odon ogre ohar oill oldm oliv olse omar
onec onei only onyx open oran orca orio orla orta osca otis otto outl oval over
owen oxfo ozzy pace pack padd page pain pale palm pand pant pape pari park parr
part pasc pass past pate patr patt paul pave pawn payn peac peak pear peck pedr
peel pele penn pepp perc pere perk perr pete petr phan phil phoe pick pier pike
pill pilo pine pink pipe pist pitt pizz plan play pleb plow plum poch pods poin
poke pola poli poll polo pond pool poor pope pork port pose post pott powe prat
pray pred prem pres pric prid prim prin prio prob prod prof prog proj prom prop
pros prot prou prov prow prox pryo puck pudd pugh pull pump punk pure purp push
putt pyle pyth quad quak qual quan quar quee quen ques quic quie quig quil quin
quit race rack radi rage rail rain rake rall ralp rams ranc rand rang rank rans
rapt rash rasm rath rats rave rawl ray razo read real reap rebe reck redd reed
reef reev refl rega regg regi reid reil rein remi remo rena rend renn reno rent
repo requ resc rese resi reso resp rest retr reub reve revo reyn rhin rhod rhys
rice rich rick ride ridg riff rift rigg righ rile ring riot ripp rise risk rite
rive road roar robb robe robi robl rock rode rodg rodr roge rogu roha roll roma
romo rona rond rook room roon roos root rope rosa rose ross roth roun rous rowe
roya rubb rube ruby rudd rude rudy ruff ruiz rule rumb rune runn rush russ ruth
ryan ryde sabe sabr sack sain sale salm salt samm samp sanc sand sank sant sanz
sask sauc saul sava save sawy saxo scal scam scan scar scha sche schm schn scho
schu scoo scop scor scot scou scra scre scru scud sculp seab seal sean sear seas
seat seav sebe seco sect secu seda sega sege seid seif seis sela self sell selv
semi send seng seni sens sept sequ sere serg seri serr serv seth sett seve sewe
shad shaf shak shal sham shan shap shar shaw shay shea shed shee shel shep sher
shie shif shil shim shin ship shir shiv shoc shoe shoo shor shot shou show shre
shri shro shru shuf shul shun shut sick side sieg sier sift sigh sign sike sila
silv simm simo simp sinc sing sink sipe sire sisk sitt sixe size skat skee skel
skid skil skim skin skip skul skyl slab slac slam slap slas slat slav sled slee
slic slid slim slip slit sloa slob slop slot slow slug slum slur smac smal smar
smas smel smit smok smoo smug snac snag snak snap snar snat snea snel snip snob
snow snug soar sobe socc sock soda soft sola sold sole soli solo solv soma somm
sona song sonn sony soon soot sore sorr sort soul soun sour sout sove sowe soyb
spac spad span spar spat spau spaw spea spec sped spee spel spen sper sphe spic
spid spie spik spil spin spir spit spla sple spli spoi spok spon spoo spor spot
spra spre spri spro spru spud spun spur spyd squa sque squi stab stac staf stag
stah stai stak stal stam stan star stat stau stav stay stea sted stee stef stei
stel stem step ster stev stew stic stif stig stil stim stin stip stir stoc stod
stok stol stom ston stoo stop stor stou stov stow stra stre stri stro stru stua
stub stuc stud stue stuf stum stun stur styl suar subb subl subm subs succ such
suck sudd sudo suff suga sugg suit sull sulu summ sump sund sung sunk supe supp
supr surf surg surp surr surv susa susp sust suth sutt suzu svob swag swai swal
swam swan swap swar swat sway swea swed swee swel swep swer swet swif swim swin
swip swir swis swit swiv swol swoo swop swor sydn sykes sylv symb symo sync synd
""".split()


# If this many stems in a row fail every call, EA is not answering this
# machine at all. Stop, instead of retrying a thousand stems to learn the
# same thing.
DEAD_STEMS = int(os.getenv("POOL_DEAD_STEMS", "10"))


class Blocked(Exception):
    pass


def collect(pause: float = PAUSE) -> list[dict]:
    """Sample the population, one request at a time.

    The first version of this scrape ran 4 workers x 4 concurrent calls and
    EA's rate limit answered 403 to everything after ~70 requests -- and
    since the bot shares the IP, that took /pubscout down too. The August
    run that completed used one call per stem at low concurrency. So:
    serial, paced, and the moment EA says 403 we wait BACKOFF_403 and try
    once more; a second 403 ends the run. The run takes 30-40 minutes and
    the bot keeps answering commands the whole time (it's a worker thread).
    """
    seen, out = set(), []
    dead_run = 0
    for i, stem in enumerate(STEMS, 1):
        hits = None
        for attempt in range(2):
            try:
                hits = ea.sample_hits(stem, pause=pause)
                break
            except ea.RateLimited as e:
                if attempt:
                    raise Blocked(f"EA rate-limited this IP twice in a row ({e}) after "
                                  f"{i} stems, {len(out)} players -- stopping to protect it")
                print(f"[pool] {e}; backing off {BACKOFF_403:.0f}s", flush=True)
                time.sleep(BACKOFF_403)
            except Exception as e:
                print(f"[pool] stem {stem!r} failed: {type(e).__name__}: {e}", flush=True)
                time.sleep(pause * 4)
                break
        if hits is None:
            dead_run += 1
            if dead_run >= DEAD_STEMS:
                raise Blocked(f"{DEAD_STEMS} stems in a row failed every EA call "
                              f"(after {i} stems, {len(out)} players) -- EA is not "
                              "answering this machine")
            continue
        dead_run = 0
        for m in hits:
            name = str(m.get("name") or "").lower()
            if name and name not in seen:
                seen.add(name)
                out.append(m)
        if i % 50 == 0:
            print(f"  {i}/{len(STEMS)} stems -> {len(out)} unique players", flush=True)
        time.sleep(pause)
    return out


def primary_position(m: dict) -> tuple[str | None, float]:
    """Where he actually plays most, and how many games he has there."""
    best, best_gp = None, 0.0
    for key, pos in ea.POSITIONS:
        gp = ea._num(m.get(key))
        if gp > best_gp:
            best, best_gp = pos, gp
    return best, best_gp


def metrics(players: list[dict], min_gp: int = MIN_POOL_GP,
            min_glgp: int = MIN_POOL_GLGP) -> dict[str, dict[str, list[float]]]:
    """Per-game rates bucketed by primary position, at the given floors.

    Returns {position: {metric: [values]}}. A player lands in exactly one
    bucket, so the pools stay independent and a percentile always means
    "among players who mainly play this position".
    """
    cols: dict[str, dict[str, list[float]]] = {}
    min_primary = primary_floor(min_gp)
    min_primary_g = primary_floor(min_glgp)

    METRICS = ("production", "scoring", "playmaking", "physicality", "discipline",
               "impact", "savepct", "gaa", "workload", "shutouts")

    def bucket(pos: str) -> dict[str, list[float]]:
        return cols.setdefault(pos, {k: [] for k in METRICS})

    for m in players:
        pos, pos_gp = primary_position(m)
        if not pos or pos_gp < (min_primary_g if pos == "G" else min_primary):
            continue
        gp = ea._num(m.get("gamesplayed"))
        glgp = ea._num(m.get("glgp"))
        skater_gp = max(gp - glgp, 0)

        if pos == "G":
            if glgp < min_glgp:
                continue
            b = bucket("G")
            sv = ea._savepct(m)
            if sv > 0:
                b["savepct"].append(sv)
            gaa = ea._num(m.get("glgaa"))
            if gaa > 0:
                b["gaa"].append(gaa)
            # How busy he is, and how often he shuts the door completely.
            b["workload"].append(ea._num(m.get("glsaves")) / glgp)
            b["shutouts"].append(ea._num(m.get("glso")) / glgp)
            continue

        if skater_gp < min_gp:
            continue
        goals = ea._num(m.get("skgoals"))
        assists = ea._num(m.get("skassists"))
        pts = goals + assists
        # Scoring and playmaking are split out: "can he finish" and "can he set
        # up" are different scouting questions, and a combined points rate hides
        # which one a player actually is.
        vals = {
            "production": pts / skater_gp,
            "scoring": goals / skater_gp,
            "playmaking": assists / skater_gp,
            "physicality": ea._num(m.get("skhits")) / skater_gp,
            "discipline": ea._num(m.get("skpim")) / skater_gp,
            "impact": ea._num(m.get("skplusmin")) / skater_gp,
        }
        # Every forward also joins a combined "F" pool. Right wing is a rare
        # primary position, so its own pool stays small no matter how hard we
        # sample -- ranking a RW against all forwards is coarser than ranking
        # him against right wings, but far better than a 46-player scale.
        targets = [bucket(pos)] + ([bucket("F")] if pos in ("C", "LW", "RW") else [])
        for b in targets:
            for k, v in vals.items():
                b[k].append(v)
    return cols


def breakpoints(values: list[float]) -> list[float]:
    """101 values, one per percentile. Lookup is a bisect at runtime."""
    if not values:
        return []
    s = sorted(values)
    out = []
    for p in range(101):
        idx = min(int(round(p / 100 * (len(s) - 1))), len(s) - 1)
        out.append(round(s[idx], 4))
    return out


RAW = "players_raw.json"
POOL = "pool.json"
# The metric whose count decides whether a position's pool is big enough.
GATE = {"G": "savepct"}


def load_previous(path: str = POOL) -> dict:
    """The pool being replaced -- the fallback for any position that
    can't clear MIN_POOL_N yet this season."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"breakpoints": {}, "counts": {}, "meta": {"positions": {}}}


def assemble(players: list[dict], prev: dict) -> dict:
    """Pick a floor per position, falling back to the previous pool.

    Each position takes the STRICTEST floor that still yields MIN_POOL_N
    players. "F" (all forwards) is built at the same floor as C, since it
    exists to back up the thin forward positions. A position that clears no
    floor keeps its previous bands, counts and floor, and its source label
    stays whatever it was (last season's, or the season before that if last
    season never cleared either -- it can chain, and the label stays honest).
    """
    ladder = list(zip(FLOORS, GOALIE_FLOORS))
    by_floor = {f: metrics(players, min_gp=f, min_glgp=g) for f, g in ladder}
    prev_pos = prev.get("meta", {}).get("positions", {})
    out = {"min_pool_gp": MIN_POOL_GP, "min_pool_glgp": MIN_POOL_GLGP,
           "min_primary_gp": MIN_PRIMARY_GP, "counts": {}, "breakpoints": {},
           "meta": {"built": time.strftime("%Y-%m-%d"), "season": SEASON, "positions": {}}}

    def clears(pos, f):
        cols = by_floor[f].get(pos, {})
        return cols if len(cols.get(GATE.get(pos, "production"), [])) >= MIN_POOL_N else None

    positions = sorted(set(prev.get("breakpoints", {})) | {p for c in by_floor.values() for p in c})
    # C first so F can follow its floor
    positions.sort(key=lambda p: (p != "C", p))
    chosen_c = None
    for pos in positions:
        pick = None
        if pos == "F" and chosen_c is not None:
            cols = clears("F", chosen_c)
            if cols:
                pick = (chosen_c, cols)
        if pick is None:
            for f, g in ladder:
                cols = clears(pos, f)
                if cols:
                    pick = (g if pos == "G" else f, cols)
                    break
        if pick:
            floor, cols = pick
            if pos == "C":
                chosen_c = floor
            out["counts"][pos] = {k: len(v) for k, v in cols.items() if v}
            out["breakpoints"][pos] = {k: breakpoints(v) for k, v in cols.items() if v}
            out["meta"]["positions"][pos] = {
                "floor": floor, "source": SEASON,
                "n": len(cols.get(GATE.get(pos, "production"), []))}
        elif pos in prev.get("breakpoints", {}):
            out["counts"][pos] = prev.get("counts", {}).get(pos, {})
            out["breakpoints"][pos] = prev["breakpoints"][pos]
            old = prev_pos.get(pos, {})
            default_floor = prev.get("min_pool_glgp" if pos == "G" else "min_pool_gp", 50)
            out["meta"]["positions"][pos] = {
                "floor": old.get("floor", default_floor),
                "source": old.get("source", PREV_SEASON),
                "n": old.get("n", max(out["counts"][pos].values() or [0])),
            }
    return out


def summary(pool: dict) -> str:
    """One line for a commit message: 'C 30+ (412) · RW NHL 26 · G NHL 26'."""
    bits = []
    for pos, info in sorted(pool["meta"]["positions"].items()):
        if info["source"] == pool["meta"]["season"]:
            bits.append(f"{pos} {info['floor']}+ ({info['n']})")
        else:
            bits.append(f"{pos} {info['source']}")
    return " · ".join(bits)


class TooSmall(Exception):
    pass


def rebuild(prev_path: str = POOL, out_path: str = POOL, players: list | None = None) -> dict:
    """Sample EA (unless players are given), assemble against the previous
    pool at prev_path, write out_path. Raises Blocked / TooSmall instead of
    writing anything when the sample isn't a population. This is what the
    bot calls on its weekly timer; main() is the same thing from a shell."""
    if players is None:
        players = collect()
        if len(players) < MIN_SAMPLE:
            raise TooSmall(f"only {len(players)} players sampled (need {MIN_SAMPLE}) -- "
                           "EA is blocking or rate-limiting this machine")
    pool = assemble(players, load_previous(prev_path))
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pool, f)
    os.replace(tmp, out_path)   # never leave a half-written pool for the card to read
    return pool


def main():
    # Sampling is the expensive, rate-limited part, so the raw pull is cached.
    # Re-deriving pools from it (new buckets, new floors) then costs nothing.
    # Pass --refresh to force a new sample.
    players = None
    if "--refresh" not in sys.argv and os.path.exists(RAW):
        with open(RAW) as f:
            players = json.load(f)
        print(f"reusing {RAW}: {len(players)} players (--refresh to re-sample)", flush=True)
    else:
        print(f"sampling {len(STEMS)} stems...", flush=True)
        try:
            players = collect()
        except Blocked as e:
            print(f"ABORT: {e}; {POOL} untouched.", file=sys.stderr)
            sys.exit(2)
        if len(players) < MIN_SAMPLE:
            print(f"ABORT: only {len(players)} players sampled (need {MIN_SAMPLE}). "
                  f"EA is blocking or rate-limiting this machine; {POOL} untouched.",
                  file=sys.stderr)
            sys.exit(2)
        with open(RAW, "w") as f:
            json.dump(players, f)
        print(f"collected {len(players)} unique players -> {RAW}", flush=True)

    pool = rebuild(players=players)
    for pos in sorted(pool["meta"]["positions"]):
        info = pool["meta"]["positions"][pos]
        print(f"  {pos:<3} {info['source']:<7} floor={info['floor']:<3} n={info['n']}")
    print(f"wrote {POOL}: {summary(pool)}")


if __name__ == "__main__":
    main()
