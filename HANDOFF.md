# Handoff: ChelScout Pubs bot → the Claude session on Matt's Mac

Written 2026-09-11 by the cloud session (peer name `chelpubs-7f`) that built
most of what is in this repo over the last week. You are on Matt's Mac and
can see files that never leave it. This tells you what exists, what is off
limits, and what is needed from you.

## What this repo is

`CHELPUBS` is **ChelScout Pubs**, a Python Discord bot for the EA NHL
"Chel" pubs community. Railway auto-deploys every push to `main` (project
`big-byf`, service `bot`). There is no staging. A push is a production
deploy within about two minutes.

Files that matter:

| file | what |
|---|---|
| `bot.py` | all slash commands, the pool-rebuild scheduler, the matchup lineup buttons |
| `voice.py` | every TTS prompt and the Fish Audio plumbing |
| `ea.py` | EA Pro Clubs API client (members search, club search, roster, stats, matches) |
| `card.py` | PIL renderers: player card, club card, matchup card, radar |
| `club.py` | club/matchup data shaping: lineups, pairings, spoken-text cleanup |
| `build_pool.py` | percentile pool builder (the grading curve) |
| `pool.json` | fallback pool in the repo; the live one is on a Railway volume at `/data/pool.json` |
| `test_radar.py`, `test_pool.py` | run with plain `python`, no pytest needed |

Commands live right now: `/ask-buddy /ask-trump /ask-cherry /ask-torts
/ask-gilbert /ask-narrator` (one question in, a voice clip back),
`/pubscout <gamertag> [voice]`, `/clubscout <club> [voice]`,
`/matchup <your club> <their club>` (with lineup buttons), `/help`,
`/rebuild-pool` (admin).

## Hard rules. Do not break these.

1. **The `eastats` folder on the Mac is OFF LIMITS for writing.** Read it,
   summarize it, never modify it, never `git add` it, never push it, never
   copy it into any repo or any container. Matt was explicit. If it sits
   inside `~/CHELPUBS`, make sure it is in `.gitignore` there before any
   commit on that machine.
2. **Do not touch the Trump voice.** Not `/ask-trump` in `bot.py`, not
   `TRUMP_VOICE_PROMPT`, not `TRUMP_VOICE_ID`, not the shared TTS plumbing
   in `voice.py` that Trump runs through (`_tts_sync`, `speak`). A previous
   "small" change to shared plumbing broke it in production and Matt had it
   reverted. Everything Trump is frozen unless Matt asks by name.
3. **Do not scrape EA from anywhere but the bot's own scheduler.** EA's
   Akamai front 403-bans an IP after a burst of about 70 requests, and the
   bot shares that IP, so a careless scrape takes `/pubscout` down for
   everyone. GitHub Actions runners are blocked outright. The bot rebuilds
   its pool weekly, one request at a time, between 06:00 and 11:00 UTC,
   and backs off on the first 403. Leave that alone.
4. **Pull before you edit.** This Mac clone was 30 commits behind when it
   was opened. `git pull` on `main` first, always. Never force-push.
5. Commit messages end with the attribution footer this session uses
   (see any recent commit on `main`).

## State of things

- The percentile pool switched itself from NHL 26 to NHL 27 on Monday
  2026-09-08 (46-minute serial scrape, no ban). Floors are 30 games for
  C/LW/F, 40 for D, 15 for RW, 12 for G, and climb toward 50 on their own.
  The card header prints whichever pool and floor it is ranking against.
- `/clubscout` works. EA's `clubs/stats` endpoint returns HTTP 500 on
  every call (an EA-side breakage reported on their forums since NHL 25),
  so the club card shows W/L/OTL from the record and cannot show goals
  for/against. The match feed works; the last-10 chips fill from it.
- `/matchup` renders one five-position radar (LW/C/RW/LD/RD) with your man
  in blue and the man he lines up against in amber, and buttons to swap
  any of the ten. It has rendered only on synthetic rosters so far. The
  first real run is the test.
- Every `/clubscout` now logs each recent match's `cNhlOnlineGameType`,
  time and score. That is to learn whether PIN-lobby (private) games
  appear in EA's feed at all. Nobody has run it since that landed.
- Discord on mobile sometimes hands back a dropdown's display label as the
  value. Club dropdowns are plain names now and `club.clean_name()` strips
  decoration defensively. Keep that pattern for any new autocomplete.
- Trump scout clips run 40+ seconds because that path has no length rule
  (`VOICE_WPS` has no `trump` entry). Known. Frozen by rule 2.

## What is needed from you

Matt says `eastats` is "gold" and that it is the EA data collection for the
LeagueGaming (LG) side of ChelScout. It has never been described to the
cloud session. Please read it and report, in this order:

1. **Inventory.** What is in the folder: scripts, data files, formats,
   sizes, and how fresh the data is (newest timestamp).
2. **Provenance.** Which EA endpoints or pages it pulls from, how it
   paces requests (this matters given rule 3), and whether it has ever
   been rate-limited.
3. **Match data.** Does it hold match-level records? If so: are PIN-lobby
   / private games in there, what distinguishes them (a game-type code, a
   flag, a match type string), and what per-player stats a match carries.
4. **Positions.** Anything that distinguishes LD from RD. EA's public
   member stats give only "D", and the matchup card currently guesses by
   games played.
5. **Identity.** How it maps EA gamertags to LG players or Discord users,
   if it does. The pubs bot's name matching is fuzzy and this would fix it.
6. **Anything else Matt calls gold** that the list above missed.

Report to Matt in chat. If cross-session messaging reaches this session,
also send the same report to peer `chelpubs-7f`. Describe, do not paste
whole files. Do not attach or transmit raw data that Matt has called off
limits; a schema and a few example rows with names removed is enough.

## How to reach the cloud session

`ListAgents` on the Mac should show `chelpubs-7f` once Remote Control is
connected for both sessions. `SendMessage` to that name delivers into its
conversation. If it does not show up, everything goes through Matt.
