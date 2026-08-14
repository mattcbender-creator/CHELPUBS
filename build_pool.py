"""Build the percentile pool used by the minicard.

Run locally, commit the result. Railway containers are ephemeral, so sampling
at boot would repeat this work on every deploy for no benefit -- the shape of
the population barely moves week to week.

    python build_pool.py            # writes pool.json

The pool deliberately EXCLUDES low-game accounts. Pubs is full of abandoned
profiles with a handful of games, and leaving them in would drag every
percentile down until a mediocre regular looked elite. Filtering to players
with real minutes makes the scale mean "good among people who actually play",
which is a harder and more honest bar.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import ea

# Minimum skater games to enter the pool. Everything below this is noise.
MIN_POOL_GP = 50
MIN_POOL_GLGP = 20  # goalies play fewer games, so they get their own floor

# A player also has to have real time AT the position he's ranked in. Someone
# with 300 games who took 4 shifts on D should not be sitting in the D pool
# dragging it around.
MIN_PRIMARY_GP = 40

# Kept deliberately low; see fetch() for what happens otherwise.
WORKERS = 4
THROTTLE = 0.15  # seconds between requests per worker

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


def collect() -> list[dict]:
    seen, out = set(), []

    def fetch(stem):
        # EA rate-limits hard: a 12-worker run over this stem list earned a
        # 403 for the whole machine partway through, and every request after
        # it silently returned nothing. Keep the concurrency low and pause
        # between requests -- the run takes longer but actually completes.
        for attempt in range(3):
            try:
                return ea._all_hits(stem, fast=True)
            except Exception:
                time.sleep(2 * (attempt + 1))
        return []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, hits in enumerate(pool.map(fetch, STEMS), 1):
            time.sleep(THROTTLE)
            for m in hits:
                name = str(m.get("name") or "").lower()
                if name and name not in seen:
                    seen.add(name)
                    out.append(m)
            if i % 50 == 0:
                print(f"  {i}/{len(STEMS)} stems -> {len(out)} unique players", flush=True)
    return out


def primary_position(m: dict) -> tuple[str | None, float]:
    """Where he actually plays most, and how many games he has there."""
    best, best_gp = None, 0.0
    for key, pos in ea.POSITIONS:
        gp = ea._num(m.get(key))
        if gp > best_gp:
            best, best_gp = pos, gp
    return best, best_gp


def metrics(players: list[dict]) -> dict[str, dict[str, list[float]]]:
    """Per-game rates bucketed by primary position.

    Returns {position: {metric: [values]}}. A player lands in exactly one
    bucket, so the pools stay independent and a percentile always means
    "among players who mainly play this position".
    """
    cols: dict[str, dict[str, list[float]]] = {}

    def bucket(pos: str) -> dict[str, list[float]]:
        return cols.setdefault(pos, {k: [] for k in
                                     ("production", "physicality", "discipline", "impact", "savepct", "gaa")})

    for m in players:
        pos, pos_gp = primary_position(m)
        if not pos or pos_gp < MIN_PRIMARY_GP:
            continue
        gp = ea._num(m.get("gamesplayed"))
        glgp = ea._num(m.get("glgp"))
        skater_gp = max(gp - glgp, 0)

        if pos == "G":
            if glgp < MIN_POOL_GLGP:
                continue
            b = bucket("G")
            sv = ea._savepct(m)
            if sv > 0:
                b["savepct"].append(sv)
            gaa = ea._num(m.get("glgaa"))
            if gaa > 0:
                b["gaa"].append(gaa)
            continue

        if skater_gp < MIN_POOL_GP:
            continue
        b = bucket(pos)
        pts = ea._num(m.get("skgoals")) + ea._num(m.get("skassists"))
        b["production"].append(pts / skater_gp)
        b["physicality"].append(ea._num(m.get("skhits")) / skater_gp)
        b["discipline"].append(ea._num(m.get("skpim")) / skater_gp)
        b["impact"].append(ea._num(m.get("skplusmin")) / skater_gp)
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


def main():
    print(f"sampling {len(STEMS)} stems...", flush=True)
    players = collect()
    print(f"collected {len(players)} unique players", flush=True)

    cols = metrics(players)
    pool = {
        "min_pool_gp": MIN_POOL_GP,
        "min_pool_glgp": MIN_POOL_GLGP,
        "min_primary_gp": MIN_PRIMARY_GP,
        "counts": {pos: {k: len(v) for k, v in mets.items() if v} for pos, mets in cols.items()},
        "breakpoints": {pos: {k: breakpoints(v) for k, v in mets.items() if v}
                        for pos, mets in cols.items()},
    }
    thin = []
    for pos in sorted(cols):
        for k, v in cols[pos].items():
            if not v:
                continue
            s = sorted(v)
            print(f"  {pos:<3} {k:<12} n={len(v):<5} p10={s[len(s)//10]:.2f} "
                  f"p50={s[len(s)//2]:.2f} p90={s[len(s)*9//10]:.2f}")
            if len(v) < 150:
                thin.append(f"{pos}/{k} (n={len(v)})")
    if thin:
        print(f"WARNING: thin pools, percentiles will be coarse: {', '.join(thin)}", file=sys.stderr)

    with open("pool.json", "w") as f:
        json.dump(pool, f)
    print("wrote pool.json")


if __name__ == "__main__":
    main()
