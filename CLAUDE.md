# ChelScout Pubs -- read this first

Python Discord bot for the EA NHL "Chel" pubs community. Railway auto-deploys
every push to `main` (project `big-byf`, service `bot`). No staging: a push to
`main` is a production deploy in ~2 minutes. `HANDOFF.md` has the longer
background (files, EA quirks, the `eastats` brief).

## Hard rules (from HANDOFF.md -- still in force)

1. The `eastats` folder on Matt's Mac is READ-ONLY. Never modify, `git add`,
   push or copy it anywhere. Keep it in `.gitignore`.
2. Don't touch the Trump voice: `/ask-trump`, `TRUMP_VOICE_PROMPT`,
   `TRUMP_SCOUT_PROMPT`, `TRUMP_VOICE_ID`, or the shared TTS plumbing in
   `voice.py` (`_tts_sync`, `speak`). Reusing them unchanged is fine.
3. Never scrape EA except from the bot's own scheduler. EA's Akamai front
   403-bans the IP after ~70 quick requests, and that takes the bot down.
4. `git pull` on `main` before editing. Never force-push.
5. Commit messages end with the attribution footer used on recent commits.

Tests run with plain `python test_radar.py`, `test_pool.py`, `test_compare.py`,
`test_scout.py` (no pytest needed). Importing `bot.py` starts the Discord
client -- patch `discord.Client.run` to a no-op to import it in a test.

## Session log: 2026-10-06 (cloud session, all live on main)

### New command: `/pubcompare <player1> <player2> [voice]`
- Two-column card in the `/matchup` style: left player blue, right amber.
  Code: `card.compare_data`, `card.format_compare`, `card.render_compare`;
  command and prompts in `bot.py` (`pubcompare`, `_compare_clip`,
  `COMPARE_READ_PROMPT`, `COMPARE_VOICE_RULE`, `_lead_with_winner`).
- **Who's better is decided by code, never the model**: each man's average
  percentile on HIS OWN JOB -- the `ROWS_BY_POS` skills for his primary
  position, ranked against his own position (`JOB_KEYS`; shutouts excluded,
  they swing too much). A D is not graded on goals; a goalie is graded as a
  goalie. Ties go to the bigger sample.
- Top of the card is COMMENTARY, no numbers (Matt's call). The read and the
  voice clip must open with who's better; `_lead_with_winner` prepends
  "X is better than Y." if the model buries it.
- Same role (skater v skater, G v G): tale of the tape, overlaid radar,
  skill-by-skill diverging bars, OVERALL row at the bottom.
- Goalie v skater ("cross"): two side-by-side "at his own job" panels, the
  overall gap, plus a side-note head-to-head where roles overlap (goalie
  with 10+ skater games, or skater with 10+ in net) -- not part of the verdict.
- Tale of the tape is PER GAME (shutout rate, G/A/P/+- per game) with the raw
  total in small print. Totals mostly measure games played (Matt's point:
  31 shutouts in 168 GP vs 10 in 74). Prompts forbid arguing from totals.
- Voices: each voice's normal `/pubscout` prompt + `COMPARE_VOICE_RULE`, and
  the model gets each player's full `scout_block()` (the same block
  `/pubscout` uses -- extracted tonight, output unchanged). That fixed Trump
  calling an elite save% "bad": the goalie numbers weren't in front of him.
- Speed: both EA lookups and the read + voice script run concurrently; the
  card posts as soon as it's drawn and the clip follows as a reply.

### Cards everywhere
- Percentiles print as plain numbers ("99", not "99th") on every card:
  player, club, matchup, compare. Model-facing text keeps "99th percentile".

### Other servers: members-only gate (BUILT, SWITCHED OFF)
- `PUBLIC_MODE=1` (Railway var) registers commands globally; `GatedTree` in
  `bot.py` lets anyone outside the home server (`DISCORD_GUILD_ID`) use them
  only if they're a member of it (REST lookup, cached 1h / 2min). Others get
  an ephemeral note with `JOIN_URL`. `/rebuild-pool` refuses outside home.
- Not enabled yet. To turn on: Developer Portal -> Public Bot + Guild Install
  (`bot`, `applications.commands`); make a never-expiring invite; set
  `PUBLIC_MODE=1` and `JOIN_URL` in Railway. Watch the EA rate limit after.

### Decisions / direction
- Compete with ChelHead on personality and shareable arguments, not stats
  breadth. ChelHead gates per server via subscription (new subs paused).
- Growth must not depend on Discord<->gamertag linking ("not everyone's gonna
  link"). Features key off gamertags and clubs.
- Parked for now: `/track <club>` + automatic post-game recaps (needs ~10-min
  polling per tracked club; EA's feed keeps only ~5 games), weekly awards.
- Next up when Matt wants it -- "fun" features, no linking needed:
  `/beef` (two voices argue a compare), `/tierlist` (up to 8 gamertags into
  S..bender tiers), `/roast`, `/draft`, `/whoami` archetype card.
  Recommended first: `/beef` and `/tierlist`.
