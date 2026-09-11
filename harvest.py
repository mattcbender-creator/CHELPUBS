"""Match-history harvester for the scout board.

EA's clubs/matches feed only holds the last ~5 games per mode, so anything
we want history for has to be polled and accumulated before the window
slides. Every poll also snapshots each tracked club's member stats (the
310-field records the board's grades come from) and fetches a one-shot
career record for any player name we haven't seen before.

Serial and slow on purpose, same reason as build_pool.py: a burst of
concurrent requests from this IP got the whole machine 403-banned in
production. One request at a time, a pause between, stop dead on a 403.
"""

import json
import os
import threading
import time
from urllib.parse import quote

import ea

# One process, many callers (the 4-hourly loop, plus every /clubscout and
# /matchup absorbing what it already fetched) -- serialize the read/write.
_LOCK = threading.Lock()

# Volume first (survives deploys), repo file as the seed/fallback --
# the same pattern pool.json uses.
REPO_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest.json")
STORE_PATH = os.getenv("HARVEST_PATH") or REPO_STORE

PAUSE = 2.5
MATCH_TYPES = ("club_private", "gameType5")


def load_store() -> dict:
    for path in (STORE_PATH, REPO_STORE):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {"matches": {}, "members": {}, "careers": {}, "meta": {}}


def save_store(store: dict) -> None:
    store["meta"]["saved"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, STORE_PATH)


def _get(url: str):
    time.sleep(PAUSE)
    try:
        return ea._get(url, timeout=20)
    except Exception as e:
        if "403" in str(e):
            raise ea.RateLimited(str(e)) from e
        raise


def absorb(club_id: str, matches: list | None, members: list | None,
           pub_matches: list | None = None) -> int:
    """Bank data another command already fetched -- no network. Every
    /clubscout and /matchup call feeds the store this way, so history
    accumulates from normal use, not just the scheduled poll. Private and
    public feeds are tagged separately and never blended."""
    if not matches and not members and not pub_matches:
        return 0
    added = 0
    with _LOCK:
        store = load_store()
        for mt, ms in (("club_private", matches), ("gameType5", pub_matches)):
            for m in ms or []:
                mid = str(m.get("matchId"))
                if mid and mid not in store["matches"]:
                    m = dict(m)
                    m["_matchType"] = mt
                    m["_club"] = str(club_id)
                    store["matches"][mid] = m
                    added += 1
        if members:
            store["members"][str(club_id)] = {"at": time.time(), "members": members}
        if added or members:
            save_store(store)
    if added:
        print(f"[harvest] absorbed {added} new matches from a scout of club {club_id}", flush=True)
    return added


def add_careers(careers: dict) -> None:
    """Bank career records fetched elsewhere (guest lookups)."""
    careers = {n: m for n, m in careers.items() if m}
    if not careers:
        return
    with _LOCK:
        store = load_store()
        store["careers"].update(careers)
        save_store(store)


def harvest(club_ids: list[str], platform: str = "common-gen5") -> dict:
    """One polite pass over the tracked clubs. Returns a small report.

    On RateLimited the partial store is saved and the exception re-raised
    so the caller can back off instead of hammering.
    """
    store = load_store()
    new_matches = 0
    try:
        for cid in club_ids:
            data = _get(f"{ea.BASE}/members/stats?platform={platform}&clubId={quote(str(cid))}")
            members = data.get("members", data) if isinstance(data, dict) else data
            if isinstance(members, list) and members:
                store["members"][str(cid)] = {"at": time.time(), "members": members}

            for mt in MATCH_TYPES:
                ms = _get(f"{ea.BASE}/clubs/matches?platform={platform}"
                          f"&clubIds={quote(str(cid))}&matchType={mt}&maxResultCount=10")
                if not isinstance(ms, list):
                    continue
                for m in ms:
                    mid = str(m.get("matchId"))
                    if mid and mid not in store["matches"]:
                        m["_matchType"] = mt
                        m["_club"] = str(cid)
                        store["matches"][mid] = m
                        new_matches += 1

        # one-shot career record for names we haven't met yet
        names = set()
        for m in store["matches"].values():
            for plist in (m.get("players") or {}).values():
                for p in plist.values():
                    nm = p.get("playername")
                    if nm and nm not in store["careers"]:
                        names.add(nm)
        for nm in sorted(names):
            d = _get(f"{ea.BASE}/members/search?platform={platform}&memberName={quote(nm)}")
            hits = d.get("members", []) or []
            exact = next((h for h in hits if str(h.get("name", "")).lower() == nm.lower()), None)
            store["careers"][nm] = exact or (hits[0] if hits else None)
    finally:
        # Merge-write under the lock: an absorb() that landed while this
        # pass was on the network must not be clobbered by our stale copy.
        with _LOCK:
            fresh = load_store()
            fresh["matches"].update(store["matches"])
            fresh["members"].update(store["members"])
            fresh["careers"].update(store["careers"])
            save_store(fresh)

    report = {"clubs": len(club_ids), "new_matches": new_matches,
              "total_matches": len(store["matches"]), "careers": len(store["careers"])}
    print(f"[harvest] {report}", flush=True)
    return report


if __name__ == "__main__":
    import sys
    clubs = sys.argv[1:] or ["12521", "22423"]
    harvest(clubs)
