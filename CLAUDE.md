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
- Speed: both EA lookups and the read + voice clip run concurrently, but card
  and clip go out as ONE message (Matt: "it cannot show separate"). Don't
  split them into a card-then-reply again.

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

## Fix: 2026-10-07 -- voices trashing good goalies
- Trump (any voice) hated on Benzymcnasty, an ELITE goalie. Cause was data,
  not the prompt: `ea.standout_trait()` only knows skater stats, so a primary
  goalie with 10+ skater games got his side-role skating as "STANDOUT TRAIT to
  focus on" (Benzy: +/- "negative"), and `ea.enforce_grade_word()` then
  rewrote praise words in the script down to that grade ("a strong GAA" ->
  "a negative GAA").
- Fix: `bot.standout_for(m)` -- primary goalies get a "goaltending" trait in
  the card's percentile tier word; skaters still use `ea.standout_trait`.
  Used by `/pubscout`, `/pubcompare` (via `scout_block`) and @mention notes.
  "goaltending" isn't in `ea.TRAIT_BANDS`, so nothing is rewritten.
- Known leftover: FIXED 2026-10-23, see below.

## Change: 2026-10-07 -- compare clips argue, they don't recite
- Matt: the /pubcompare narration kept spelling out "plus slash minus" and
  every stat. `COMPARE_VOICE_RULE` now OVERRIDES the solo-report rules:
  winner first, then 2-3 deciding MATCHUPS in plain hockey words, no stat
  readout (max one number), no box-score labels, verdict again at the end.
- Compare scripts pass through `club.speakable()` before TTS ("+/-" and
  "plus/minus" -> "plus minus", SV%/GAA/P-GP spelled out).


## Change: 2026-10-12 -- clubscout card and clip in one message
- Matt: the `/clubscout` audio has to land WITH the card, not after it.
  `/clubscout` now does what `/pubcompare` does: the voice task starts as
  soon as the club block exists, the read and render run alongside it, and
  card + clip go out as ONE message. With a voice picked, a one-line
  "card and clip on the way" note posts immediately and `_deliver` EDITS
  that same message into the card + clip when they're ready (fallback: fresh
  send, delete the note). If the clip fails the card still goes out, with
  the error in the message text. `_club_voice_file` returns bytes now, not
  a `discord.File` -- files get rebuilt per send attempt.
- Rule (both commands): never split card and clip into a card-then-reply.

## Change: 2026-10-12 -- Trump stops reading the stat sheet
- Matt: Trump "says every single number" in clips. `TRUMP_NUMBERS_RULE` in
  `bot.py`: at most two numbers per clip, said like a person ("almost a
  point a game"), no decimals, no percentiles-as-numbers, no stat labels, no
  position-by-position walk. Bolted on via `bot.voice_rules(voice)` (length
  rule + Trump rule) for `/pubscout`, `/pubcompare`, `/clubscout`.
  `TRUMP_SCOUT_PROMPT` / `voice.py` / `/ask-trump` untouched, per rule 2.

## Fix: 2026-10-12 -- club card read cut off, "6GANG7" spelled out
- Card: `_wrap(max_lines=4)` was silently dropping the end of the club read
  (6GANG7's ended "a track meet with"). `card._fit_read` now steps the font
  27 -> 25 -> 23 with 4 -> 5 -> 6 lines until the whole read fits, and if it
  still doesn't, cuts at the last COMPLETE sentence. `CLUB_READ_PROMPT` is
  35-50 words, three sentences max.
- Voice: TTS spelled "6GANG7" letter by letter. `club.spoken_name()` splits
  digits from letters and lowercases all-caps words ("6 Gang 7", "Wildman
  Hockey"); `format_block` tells the model `say the name "6 Gang 7"`, and
  `club.say_name()` swaps the raw tag out of the script before TTS.
- Trump on `/clubscout` only: `TRUMP_CLUB_FLAVOR` -- one nickname for the
  club, one or two for roster guys, max three per clip. Not on /pubscout or
  /pubcompare. Trump prompt constants still untouched.

## Incident: 2026-10-21 -- Railway env-var breach, OpenRouter keys revoked
- Railway disclosed unauthorized access to customer environment variables.
  OpenRouter disabled Matt's keys (ASKBOTS-PUBS, GM ASK, golf) on 10-22.
- New `OPENROUTER_API_KEY` set in Railway 10-22. Still to rotate once Railway
  deploys are healthy: `DISCORD_BOT_TOKEN` (Dev Portal -> Bot -> Reset
  Token, paste into Railway immediately), `FISH_API_KEY`,
  `FISH_TRUMP_API_KEY`. Voice/model IDs and tuning vars are not secrets.
- No secrets live in this repo (no `.env`; everything is read from env).

## Fix: 2026-10-23 -- Trump called a bad-scoring D "a good scorer"
- SenorOGPotato: D, 23G 114A in 96 GP, card says SCORING BAD. Trump said he
  scores. Cause was the data again: the skater standout trait still came
  from `ea`'s absolute bands, where 1.4 P/GP = "production: very good" for
  anyone, and the block only listed the D's four BAR skills, so his scoring
  percentile never reached the model at all.
- Fix (bot.py): `_skater_standout` picks the most extreme skill on the
  card's radar in the card's tier word (goalies already worked this way).
  `scout_block` now lists EVERY radar skill with its tier word and what it
  measures ("SCORING (goals per game): BAD, 24th..."), plus a rule that
  scoring means goals and points are not a grade. Feeds `/pubscout`,
  `/pubcompare` and @mention notes.
- `ea.standout_trait`, `ea.grade_positions` and the absolute bands are now
  unused by the bot; `enforce_grade_word` no-ops for skaters, as it already
  did for goalies.

## Fix: 2026-10-25 -- club clips read the card; "Pl0t" said as P-L-zero-T
- Every voice on `/clubscout` (not just Trump) was reciting "elite, elite,
  elite, bad". System cause: `club.format_block` ends with "Use the card's
  words: 90+ elite, 78+ stud..." -- the CARD READ's instruction, and the
  voices got the same block. The player prompts also say "use the exact
  grade word", so the model obeyed.
- Fix, same mechanism that fixed /pubcompare: voices now get
  `club.voice_block(s)` -- a code-written STORY first (`club.story`: what
  they are, record in words, form, best skill, weakest skill, how you beat
  them, goalie, best player, weak link -- all from the card's percentiles,
  so it can't disagree with the card) and the grades only as "reference,
  never read out". `CLUB_VOICE_RULE` in bot.py OVERRIDES the player-report
  rules for every voice: one tier word max, one number max, no percentiles,
  no skill-by-skill walk. The card read still uses `format_block` as before.
- Leetspeak names: `club.spoken_name` turns a digit WRAPPED in letters into
  the letter ("Pl0t" -> "Plot", "Sn1per" -> "Sniper"); edge digits stay
  numbers ("6GANG7", "PS5"). Applied to club names, roster names in club
  clips, and now gamertags in /pubscout and /pubcompare clips (block says
  `SAY HIS NAME "..."`; `club.say_name(s)` swaps the raw tag before TTS).
- `club.ordinal()`: "22nd", not "22th", everywhere the model sees a
  percentile (voices were reading "twenty-two-th").
