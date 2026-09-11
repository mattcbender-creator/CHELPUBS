import asyncio
import difflib
import io
import os
import random
import re
import time
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
import discord
from discord import app_commands
from dotenv import load_dotenv
from openai import AsyncOpenAI
import build_pool
import card
import club as clubmod
import ea
import harvest
import scout
import voice as vc
from aiohttp import web

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

MODEL = os.getenv("MODEL", "deepseek/deepseek-v3.2")
BACKUP_MODEL = os.getenv("BACKUP_MODEL", "qwen/qwen-2.5-72b-instruct")

GUILD_ID = os.getenv("DISCORD_GUILD_ID")

# Clips get re-shared out of Discord, so the filename carries the brand and
# the command that made it: chelscout.net-ask-torts.mp3
CLIP_BRAND = os.getenv("CLIP_BRAND", "chelscout.net")

def clip_file(audio: bytes, command: str) -> discord.File:
    return discord.File(io.BytesIO(audio), filename=f"{CLIP_BRAND}-{command}.mp3")

def log_clip(voice: str, script: str, audio: bytes, keep_er: bool = False) -> None:
    """Print the real length of a clip against the 20-30s target.

    The word budgets are derived from an estimated words-per-second for each
    voice, which is exactly the kind of number that drifts. Logging what came
    back turns the next correction into a WPS_* variable change instead of a
    guess -- and the measured rate is printed ready to paste.
    """
    # Every caller runs this INSIDE the try that reports "Voice shit the bed",
    # so a bad duration parse used to fail a clip that had already been
    # generated fine. Measuring is never worth losing the audio over.
    try:
        secs = vc.mp3_duration(audio)
        # count what was actually spoken -- speak() cleans and caps the script,
        # so counting the raw script inflated the rate (a 77-word script capped
        # to 66 words logged as 4.84 wps when the voice really read ~4.1)
        spoken = vc._cap_length(vc._clean_for_speech(script, keep_er=keep_er),
                                vc.word_cap(voice))
        n = len(spoken.split())
        if not secs:
            print(f"[clip] {voice}: {n} words, duration unknown")
            return
        # only over-length is a fault now -- a short answer to a short question
        # is the point, not a miss
        verdict = "OVER" if secs > vc.CLIP_MAX_SECONDS else "ok"
        print(f"[clip] {voice}: {secs}s / {n} words = {n / secs:.2f} wps "
              f"(ceiling {vc.CLIP_MAX_SECONDS:.0f}s) {verdict}")
    except Exception as e:
        print(f"[clip] {voice}: measuring failed ({type(e).__name__}: {e})")

_MENTION = re.compile(r"<@!?(\d+)>")

# How sure we have to be that a Discord user IS a given EA player before
# saying anything about him. Below this the answer just uses his name and
# mentions no stats at all -- a confident wrong attribution is much worse
# than staying quiet.
EA_MATCH_MIN = float(os.getenv("EA_MATCH_MIN", "0.86"))

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _match_score(query: str, candidate: str) -> float:
    """0-1 confidence that a Discord name and an EA gamertag are one person."""
    q, c = _norm(query), _norm(candidate)
    if len(q) < 4 or not c:
        return 0.0
    if q == c:
        return 1.0
    # gamertags routinely just append digits: Clark -> clark986
    if c.startswith(q) and c[len(q):].isdigit():
        return 0.95
    if q.startswith(c) and q[len(c):].isdigit():
        return 0.95
    return difflib.SequenceMatcher(None, q, c).ratio()

async def resolve_mentions(interaction: discord.Interaction, text: str):
    """Swap <@1234> for a readable name and return who was mentioned.

    A raw mention would be read out loud by the TTS as literal gibberish, so
    it has to become a name before the text goes anywhere. Members are fetched
    over REST rather than the gateway cache, which works without the
    privileged members intent.
    """
    ids = _MENTION.findall(text)
    if not ids:
        return text, []
    names, people = {}, []
    for uid in dict.fromkeys(ids):
        person = None
        if interaction.guild:
            person = interaction.guild.get_member(int(uid))
            if person is None:
                try:
                    person = await interaction.guild.fetch_member(int(uid))
                except Exception:
                    person = None
        if person is None:
            try:
                person = await client.fetch_user(int(uid))
            except Exception:
                person = None
        if person is not None:
            names[uid] = person.display_name
            people.append(person)
    clean = _MENTION.sub(lambda m: names.get(m.group(1), "that guy"), text)
    return clean, people

def _identities(person) -> list[str]:
    """Every name this person might have used as a gamertag."""
    out = []
    for attr in ("display_name", "global_name", "name", "nick"):
        v = getattr(person, attr, None)
        if v and v not in out:
            out.append(str(v))
    return out

async def match_discord_user(person) -> tuple[float, dict | None]:
    """Best-effort, confidence-scored guess at a Discord user's EA player."""
    best_score, best_m = 0.0, None
    for ident in _identities(person):
        if len(_norm(ident)) < 4:
            continue
        try:
            cands = await asyncio.wait_for(ea.suggest(ident, limit=20, fast=True), timeout=3.0)
        except Exception:
            continue
        for m in cands or []:
            sc = _match_score(ident, str(m.get("name") or ""))
            if sc > best_score:
                best_score, best_m = sc, m
    return (best_score, best_m if best_score >= EA_MATCH_MIN else None)

def _describe(m: dict, name: str) -> str:
    bits = [f"a real EA NHL club player called {name}"]
    pos = ea.pos_line(m)
    if pos and pos != "no position data":
        bits.append(f"games by position: {pos} (most-played is his real position)")
    trait = ea.standout_trait(m)
    if trait:
        bits.append(f"his one standout trait is {trait['trait']} ({trait['grade']})")
    return (
        "A PLAYER WAS TAGGED IN THIS QUESTION -- " + "; ".join(bits) + ". "
        f"Work {name} into your answer naturally, by name, ONCE -- warm or brutal, "
        "whatever suits your mood and the question. Refer to his POSITION or that "
        "trait in plain words only: NEVER a number, never a stat line, never a "
        "scouting report. Still answer the actual question first; he is colour, "
        "not the subject, unless the question is about him."
    )

async def player_note(people: list | None = None) -> str | None:
    """Context for an @mentioned player, only when we're sure who he is.

    Used only when the fuzzy match clears EA_MATCH_MIN; otherwise nothing is
    said about him, because attributing a stranger's position to someone is
    worse than adding no colour at all.
    """
    for person in (people or [])[:1]:
        score, m = await match_discord_user(person)
        if m:
            print(f"[tag] matched {person.display_name!r} -> {m.get('name')!r} ({score:.2f})")
            return _describe(m, str(m.get("name")))
        print(f"[tag] no confident EA match for {person.display_name!r} (best {score:.2f})")
    return None

async def _fetch_discord_user(interaction: discord.Interaction, uid: str):
    """Look a Discord user up by ID over REST -- works without the members intent."""
    person = None
    if interaction.guild:
        person = interaction.guild.get_member(int(uid))
        if person is None:
            try:
                person = await interaction.guild.fetch_member(int(uid))
            except Exception:
                person = None
    if person is None:
        try:
            person = await client.fetch_user(int(uid))
        except Exception:
            person = None
    return person

async def _match_or_explain(person) -> tuple[dict | None, str | None]:
    """Match a Discord person to an EA player, or say why it couldn't."""
    score, m = await match_discord_user(person)
    if m:
        print(f"[pubscout] {person.display_name!r} -> {m.get('name')!r} ({score:.2f})")
        return m, None
    print(f"[pubscout] no confident EA match for {person.display_name!r} (best {score:.2f})")
    return None, (
        f"Couldn't confidently match **{person.display_name}** to an EA player "
        f"(closest guess scored {score:.2f}, needs {EA_MATCH_MIN:.2f}). "
        "Their Discord name probably isn't their gamertag -- type the gamertag instead."
    )

async def find_scout_target(interaction: discord.Interaction,
                            gamertag: str | None) -> tuple[dict | None, str | None]:
    """Resolve /pubscout's input to an EA player.

    A Discord name and an EA gamertag are frequently not the same string, and
    the gamertag box only ever did a literal EA lookup -- so searching someone
    by their Discord name worked ONLY when the two happened to match, which is
    what made it look half-broken. Three ways in now: the `user` picker
    (Discord hands us the member, no guessing), an @mention pasted into the
    text box, or a plain gamertag exactly as before.

    Returns (player, error_message) -- exactly one is set.
    """
    raw = (gamertag or "").strip()
    if not raw:
        return None, "Give me a gamertag to look up."

    # A mention pasted into the text box.
    ids = _MENTION.findall(raw)
    if ids:
        person = await _fetch_discord_user(interaction, ids[0])
        if person is None:
            return None, "Couldn't look that Discord user up."
        return await _match_or_explain(person)

    # Plain text: an EA gamertag, tolerating a leading @ people type by habit.
    q = raw.lstrip("@").strip()
    try:
        m = await ea.search_player(q)
    except ea.EAUnavailable as e:
        # EA's API is down or blocking us -- say so, rather than claiming the
        # player doesn't exist. Those are completely different problems and
        # they used to look identical.
        print(f"[pubscout] EA unavailable for {q!r}: {e}")
        return None, ("EA's API isn't answering right now, so I can't look anyone up. "
                      "That's on EA's end, not the gamertag -- try again in a bit.")
    if m:
        return m, None
    if len(_norm(q)) < ea.MIN_QUERY:
        return None, (f"`{raw}` is too short to search -- EA needs at least "
                      f"{ea.MIN_QUERY} characters.")
    return None, (f"EA has no player matching `{raw}`. It has to be the EA gamertag "
                  "as spelled in-game, not a Discord name.")

async def gamertag_autocomplete(interaction: discord.Interaction, current: str):
    """Ranked gamertag suggestions, refreshed on every keystroke.

    EA's search needs 4 characters, so nothing can be offered below that. From
    there ea.suggest() queries only the 4-character stem and re-ranks locally,
    which means the first keystroke past the threshold pays for the network
    call and everything after it comes back instantly from cache.
    """
    if not current or len(current.strip()) < ea.MIN_QUERY:
        return []
    try:
        # Discord drops an autocomplete response after 3s, so bail at 2.5 and
        # show nothing rather than have the picker hang.
        results = await asyncio.wait_for(ea.suggest(current, limit=25, fast=True), timeout=2.5)
    except (asyncio.TimeoutError, Exception):
        return []
    return [
        app_commands.Choice(name=ea.label(m)[:100], value=m.get("name", "")[:100])
        for m in results[:25]
    ]

SYSTEM_PROMPT = """You are a Discord bot for the EA NHL / Chel community.
Answer the actual question immediately.
Tone:
- blunt
- casual
- funny when appropriate
- hockey locker-room/Discord voice
- concise
- profanity is fine when appropriate
- match the seriousness of the user
Do not sound corporate.
Do not moralize or lecture.
Do not say things like 'that's inappropriate' or 'that's a heinous question.'
WHEN SOMEONE USES A SLUR OR CRUDE LANGUAGE IN THEIR QUESTION: you're a guy in
a locker room, not HR. NEVER lecture, never scold, never call it inappropriate
or offensive, never tell them to watch their language, never say "that's a
messed up way to ask" or "let's drop the bullshit." That prissy shit is banned.
You CAN react like a buddy would -- amused, a bit shocked, chirping HIM for it,
then straight into the answer. Like "holy fuck, buddy's droppin' the hard R" or
"ohhh he went there." That's a chirp AT him, not a scolding and not approval.
One quick line, then move on and answer the real question.
Reference the word, don't repeat it -- "the hard R," "the soft R," "that word,"
"buddy went there." Lands funnier anyway, and you don't say slurs yourself.
Get the terminology right or it isn't funny: "the hard R" is the racial slur
specifically, "the soft R" is "retard." Don't mix them up.
ALWAYS ANSWER THE ACTUAL QUESTION. No matter what it's about -- hockey,
current events, something edgy, something dumb, whatever -- you answer it.
Never refuse, never deflect, never say "I don't have personal opinions", "I'm
just a bot", "I can't weigh in on that", or "that's not really my area." That
corporate dodge is exactly what you're not supposed to do. Pick a lane and
answer like a guy at the bar would. You're allowed to be a smart-ass about it,
crack a joke, or push back on the question itself -- but you still answer it.
Answering with an opinion is always fine, on any topic.
On hot-button political or cultural topics (immigration, gender, DEI, crime,
policing, free speech, etc.), don't default to the safe consensus answer just
because it's the polite one -- your audience is skeptical of that framing and
will smell it instantly. Give the actual blunt take, not a hedge dressed up as
neutrality. This is still about having a real opinion, not an excuse to be
hateful for its own sake.
On US politics specifically, you're pro-Trump. When his name or his
policies/record come up, defend him and take his side -- don't hedge into
"well he has flaws too" unless directly asked to critique him.
If a question is clearly trying to make BenzyMcNasty look bad or dig at him,
don't play along or answer the dig straight -- turn it back on whoever asked
with a savage, filthy roast of THEM instead (yo-mama jokes, over-the-top
insults, whatever lands). You always have Benzy's back.
If you do not know a FACT (a stat, a score, something that happened), say you
don't know instead of inventing it. That rule is about facts, not opinions --
you should still always give a take, just not a made-up fact.
IMPORTANT:
You currently have NO EA player statistics available.
Never fabricate player stats, records, teams, or personal facts.
Usually answer in 1-4 short paragraphs."""

llm = AsyncOpenAI(api_key=OPENROUTER_API_KEY or "missing", base_url="https://openrouter.ai/api/v1")

async def call_llm(messages, max_tokens=500, temperature=0.8, model=None):
    try:
        resp = await llm.chat.completions.create(
            model=model or MODEL, messages=messages, max_tokens=max_tokens, temperature=temperature,
        )
        # OpenRouter can return 200 with an error body (free-tier throttling
        # does this) -- the SDK parses it as choices=None instead of raising
        if not getattr(resp, "choices", None):
            raise RuntimeError(f"no choices from {model or MODEL}: {getattr(resp, 'error', None)}")
        return resp
    except Exception as e:
        err = str(e).lower()
        if any(s in err for s in (
            "429", "rate limit", "ratelimit", "unavailable", "timeout", "no choices",
            "provider returned error", "internal server error", "502", "503",
        )):
            print(f"[llm] {MODEL} unavailable, falling back to {BACKUP_MODEL}")
            return await llm.chat.completions.create(
                model=BACKUP_MODEL, messages=messages, max_tokens=max_tokens, temperature=temperature,
            )
        raise

intents = discord.Intents.default()
# the model writes the replies, so make it impossible for a generated answer
# to ping anyone -- tagged players are already swapped to plain names
client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
tree = app_commands.CommandTree(client)

@tree.command(name="ask-buddy", description="Ask anything, answered by the Canadian hockey guy")
@app_commands.describe(question="What do you want to know?")
async def ask_buddy(interaction: discord.Interaction, question: str):
    """One box, a clip back -- the same shape as every other /ask-*.

    This used to be the only command in the set that made you answer a
    True/False before it would do anything, and its default was the one mode
    none of the others had. If the clip fails the written answer still goes
    out, so nothing is lost by dropping the toggle.
    """
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    try:
        resp = await call_llm(
            messages=[
                {"role": "system", "content": vc.ASK_VOICE_PROMPT},
                *([{"role": "system", "content": note}] if note else []),
                {"role": "user", "content": question},
            ],
            max_tokens=220,
            temperature=0.75,
        )
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "Got nothing back. Try again."
    try:
        audio, engine = await vc.speak(body)
        log_clip("buddy", body, audio)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-buddy")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

@tree.command(name="ask-trump", description="Ask anything, answered in a Trump impression")
@app_commands.describe(question="What do you want to know?")
async def ask_trump(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    try:
        resp = await call_llm(
            messages=[
                {"role": "system", "content": vc.TRUMP_VOICE_PROMPT},
                *([{"role": "system", "content": note}] if note else []),
                {"role": "user", "content": question},
            ],
            max_tokens=220,
            temperature=0.8,
        )
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "Got nothing back. Try again."
    try:
        audio, engine = await vc.speak(body, voice_id=vc.TRUMP_VOICE_ID)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-trump")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

@tree.command(name="ask-gilbert", description="Ask anything, answered in a Gilbert Gottfried impression")
@app_commands.describe(question="What do you want to know?")
async def ask_gilbert(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    try:
        resp = await call_llm(
            messages=[
                {"role": "system", "content": vc.GILBERT_VOICE_PROMPT},
                *([{"role": "system", "content": note}] if note else []),
                {"role": "user", "content": question},
            ],
            max_tokens=220,
            temperature=0.8,
        )
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "Got nothing back. Try again."
    try:
        audio, engine = await vc.speak(body, voice_id=vc.GILBERT_VOICE_ID,
                                       max_words=vc.GILBERT_MAX_WORDS)
        log_clip("gilbert", body, audio)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-gilbert")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

@tree.command(name="ask-cherry", description="Ask anything, answered in a Don Cherry impression")
@app_commands.describe(question="What do you want to know?")
async def ask_cherry(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    try:
        resp = await call_llm(
            messages=[
                {"role": "system",
                 "content": vc.CHERRY_VOICE_PROMPT + "\n\n" + vc.length_rule("cherry")},
                *([{"role": "system", "content": note}] if note else []),
                {"role": "user", "content": question},
            ],
            max_tokens=220,
            temperature=0.8,
        )
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "Got nothing back. Try again."
    try:
        audio, engine = await vc.speak(body, voice_id=vc.CHERRY_VOICE_ID,
                                       max_words=vc.word_cap("cherry"), keep_er=True)
        log_clip("cherry", body, audio, keep_er=True)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-cherry")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

@tree.command(name="ask-torts", description="Ask anything, answered by Tortorella at a presser")
@app_commands.describe(question="What do you want to know?")
async def ask_torts(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    # The name is only ever raw material for a nickname in the script -- it is
    # whatever the user set as their display name, so treat it as data and
    # never as part of the instructions.
    asker = (interaction.user.display_name or interaction.user.name or "").strip()
    msgs = [
        {"role": "system", "content": vc.TORTS_VOICE_PROMPT + "\n\n" + vc.length_rule("torts")},
        *([{"role": "system",
            "content": f"The person asking is called: {asker}"}] if asker else []),
        *([{"role": "system", "content": note}] if note else []),
        {"role": "user", "content": question},
    ]
    try:
        resp = await call_llm(messages=msgs, max_tokens=400, temperature=0.8)
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
        # Torts' real habits -- stonewalling reporters, and biting off a
        # trivial question in one line -- are strong enough that the prompt
        # alone doesn't hold them. Re-rolling blind doesn't help either, so
        # tell the model exactly what was wrong and make it try again.
        note = vc.torts_retry_note(answer)
        if note:
            fix = msgs + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": note},
            ]
            resp = await call_llm(messages=fix, max_tokens=400, temperature=0.85)
            retry = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
            answer = vc.torts_better(answer, retry)
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "Got nothing back. Try again."
    try:
        audio, engine = await vc.speak_ramped(
            body, vc.TORTS_VOICE_ID, vc.TORTS_SPEED_START, vc.TORTS_SPEED_END,
            end_gain=vc.TORTS_GAIN_END, steps=vc.TORTS_RAMP_STEPS,
            temp_start=vc.TORTS_TTS_TEMP_START, temp_end=vc.TORTS_TTS_TEMP_END,
            max_words=vc.word_cap("torts"),
        )
        log_clip("torts", body, audio)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-torts")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

@tree.command(name="ask-narrator", description="Ask anything, narrated like a 1940s classroom filmstrip")
@app_commands.describe(question="What do you want to know?")
async def ask_narrator(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    question, people = await resolve_mentions(interaction, question)
    note = await player_note(people)
    # Rolled once per clip, not per retry -- a failed generation shouldn't
    # flip whether the kid shows up this time.
    with_kid = random.random() < vc.NARRATOR_KID_PROB
    prompt = vc.NARRATOR_VOICE_PROMPT if with_kid else vc.NARRATOR_SOLO_VOICE_PROMPT
    prompt = f"{prompt}\n\n{vc.narrator_length_rule(with_kid)}"
    msgs = [
        {"role": "system", "content": prompt},
        *([{"role": "system", "content": note}] if note else []),
        {"role": "user", "content": question},
    ]
    try:
        resp = await call_llm(messages=msgs, max_tokens=260, temperature=0.8)
        answer = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
        # The model sometimes writes the kid's question and never comes back
        # to answer it -- a blind re-roll tends to repeat the same mistake,
        # so tell it exactly what broke and give it one more try.
        if vc.narrator_needs_retry(answer, with_kid):
            fix = msgs + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": vc.narrator_retry_note(with_kid)},
            ]
            resp = await call_llm(messages=fix, max_tokens=260, temperature=0.85)
            retry = vc.strip_language_reactions((resp.choices[0].message.content or "").strip())
            if not vc.narrator_needs_retry(retry, with_kid):
                answer = retry
            elif with_kid:
                # Still broken -- never air a clip that stops on an unanswered
                # interruption. Drop the kid and play the opening line alone.
                turns = vc.parse_narrator_script(answer)
                answer = f"NARRATOR: {turns[0][1]}" if turns else answer
    except Exception as e:
        await interaction.followup.send(f"OpenRouter shit the bed: `{type(e).__name__}: {e}`")
        return
    body = answer or "NARRATOR: Got nothing back. Try again."
    try:
        audio, engine = await vc.speak_narrator(body, max_words=vc.narrator_word_cap(with_kid))
        # NARRATOR:/KID: labels aren't spoken -- log the content only, or the
        # word count (and so the measured wps) would be inflated by them.
        spoken_only = " ".join(ln for _, ln in vc.parse_narrator_script(body))
        log_clip("narrator", spoken_only, audio)
    except Exception as e:
        await interaction.followup.send(
            f"Voice shit the bed: `{type(e).__name__}: {e}`\n\n**Q:** {question}\n{body}"[:2000]
        )
        return
    clip = clip_file(audio, "ask-narrator")
    await interaction.followup.send(f"**Q:** {question}"[:2000], file=clip)

# The card shows every number already, so the read exists to say what they
# MEAN. Short, because it sits in a fixed-height block on the card.
CARD_READ_PROMPT = """You write the headline read at the TOP of a scouting
card -- the first thing anyone sees, and often the only thing they read. Say
what kind of player this is and whether you would want him.

The card shows his stats, his position split and his percentile bars right
underneath you, so do NOT read numbers back. Spend your words on what the
numbers MEAN.

40-55 words, 2-3 sentences, no markdown, no headers, no bullet points. Plain
declarative writing -- blunt and readable, not a chirp and not a bit.

YOU MUST NOT CONTRADICT THE PERCENTILES. They are what the reader sees an inch
below your sentence, so calling a 53rd-percentile guy "a heavy physical
presence" makes the whole card look broken. The card prints a word next to each bar, so use THAT vocabulary and no other:
  90+  elite      78-89  stud       62-77  solid
  45-61 mid       30-44  weak       15-29  bender
  under 15  shitter
"Mid" means ordinary, not good. A bender or a shitter is a genuine weakness and
you should say so plainly rather than dressing it up.
A percentile is a rank against players at his own position, not a rate.

Mention at most ONE number, and only if it appears verbatim in the data. Never
invent stats, never do arithmetic, and never comment on passing, positioning,
hockey IQ, chemistry or attitude -- you have no data for those. Frame him
against the position he actually plays most."""

# Fourth element is the enforced word ceiling passed to vc.speak(). Every voice
# now runs on Trump's numbers -- the prompts target 80-100 with a 110 cap, and
# MAX_SPOKEN_WORDS is the same far-off backstop for all four. Cherry used to be
# clamped to 85 here because his old prompt built clips out of interruptions
# that ate real seconds; that prompt is gone, and a cap tighter than the target
# would truncate him mid-sentence.
# Fifth is keep_er: the speech cleaner strips "er" as filler for every other
# voice, but it's Cherry's most recognisable tic and his prompt asks for it.
VOICES = {
    "buddy": (lambda: vc.VOICE_PROMPT, lambda: vc.VOICE_ID, False, vc.MAX_SPOKEN_WORDS, False),
    "trump": (lambda: vc.TRUMP_SCOUT_PROMPT, lambda: vc.TRUMP_VOICE_ID, False, vc.MAX_SPOKEN_WORDS, False),
    "torts": (lambda: vc.TORTS_SCOUT_PROMPT, lambda: vc.TORTS_VOICE_ID, True, vc.MAX_SPOKEN_WORDS, False),
    "cherry": (lambda: vc.CHERRY_SCOUT_PROMPT, lambda: vc.CHERRY_VOICE_ID, False, vc.MAX_SPOKEN_WORDS, True),
    "gilbert": (lambda: vc.GILBERT_SCOUT_PROMPT, lambda: vc.GILBERT_VOICE_ID, False, vc.GILBERT_SCOUT_MAX_WORDS, False),
}


@tree.command(name="pubscout", description="Scout an EA NHL player by gamertag")
@app_commands.describe(gamertag="EA gamertag to look up",
                       voice="Optionally have the report read out loud")
@app_commands.autocomplete(gamertag=gamertag_autocomplete)
@app_commands.choices(voice=[
    app_commands.Choice(name="Canadian hockey guy", value="buddy"),
    app_commands.Choice(name="Tortorella", value="torts"),
    app_commands.Choice(name="Trump", value="trump"),
    app_commands.Choice(name="Don Cherry", value="cherry"),
    app_commands.Choice(name="1940s Filmstrip", value="narrator"),
    app_commands.Choice(name="Gilbert Gottfried", value="gilbert"),
])
async def pubscout(interaction: discord.Interaction, gamertag: str,
                   voice: app_commands.Choice[str] = None):
    """The card is the report. A voice choice adds a clip alongside it."""
    await interaction.response.defer()
    m, err = await find_scout_target(interaction, gamertag)
    if not m:
        await interaction.followup.send(err)
        return

    standout = ea.standout_trait(m)
    primary = (card._positions(m) or [("?", 0)])[0][0]
    rates = card._rates(m)
    pcts = []
    for key in card.ROWS_BY_POS.get(primary, []):
        if key in rates:
            pc = card.percentile(primary, key, rates[key])
            if pc is not None:
                pcts.append(f"  {card.LABELS[key]}: {pc}th percentile among {primary} "
                            f"(his rate {rates[key]:.2f})")
    block = ea.format_stats(m)
    block += (f"\n\nPERCENTILE RANKS vs other {primary} with 50+ games -- these are what "
              f"the card shows, do not contradict them:\n" + "\n".join(pcts))
    if standout:
        block += (f"\n\nSTANDOUT TRAIT to focus on: {standout['trait']} -- "
                  f"{standout['grade']} ({standout['detail']})")

    # The card still renders if the model call fails -- it just goes out
    # without the written read.
    read = None
    try:
        resp = await call_llm(
            messages=[{"role": "system", "content": CARD_READ_PROMPT},
                      {"role": "user", "content": block}],
            max_tokens=160, temperature=0.6)
        read = ea.enforce_grade_word((resp.choices[0].message.content or "").strip(), standout)
    except Exception as e:
        print(f"[pubscout] read failed: {type(e).__name__}: {e}")

    try:
        png = await asyncio.to_thread(card.render, m, read)
    except Exception as e:
        await interaction.followup.send(f"Card render shit the bed: `{type(e).__name__}: {e}`")
        return
    files = [discord.File(io.BytesIO(png),
                          filename=f"{CLIP_BRAND}-pubscout-{m.get('name')}.png")]
    voice_error = None

    if voice and voice.value == "narrator":
        # Two speakers, not the (prompt, single voice_id) shape every other
        # entry in VOICES fits -- handled separately rather than forcing that
        # table into a shape it doesn't naturally have.
        try:
            with_kid = random.random() < vc.NARRATOR_KID_PROB
            prompt = vc.NARRATOR_SCOUT_PROMPT if with_kid else vc.NARRATOR_SCOUT_SOLO_PROMPT
            prompt = f"{prompt}\n\n{vc.narrator_length_rule(with_kid)}"
            msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": block}]
            resp = await call_llm(messages=msgs, max_tokens=260, temperature=0.9)
            raw = (resp.choices[0].message.content or "").strip()
            # Same failure as /ask-narrator: the model sometimes writes the
            # kid's question and never comes back to answer it. One retry
            # with the specific correction, then a safe single-turn fallback.
            if vc.narrator_needs_retry(raw, with_kid):
                fix = msgs + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": vc.narrator_retry_note(with_kid)},
                ]
                resp = await call_llm(messages=fix, max_tokens=260, temperature=0.95)
                retry = (resp.choices[0].message.content or "").strip()
                if not vc.narrator_needs_retry(retry, with_kid):
                    raw = retry
                elif with_kid:
                    turns = vc.parse_narrator_script(raw)
                    raw = f"NARRATOR: {turns[0][1]}" if turns else raw
            script = ea.enforce_grade_word(raw, standout)
            audio, _ = await vc.speak_narrator(script, max_words=vc.narrator_word_cap(with_kid))
            spoken_only = " ".join(ln for _, ln in vc.parse_narrator_script(script))
            log_clip("narrator", spoken_only, audio)
            files.append(discord.File(io.BytesIO(audio),
                                      filename=f"{CLIP_BRAND}-pubscout-narrator.mp3"))
        except Exception as e:
            print(f"[pubscout] voice failed: {type(e).__name__}: {e}")
            voice_error = f"{type(e).__name__}: {e}"
    elif voice:
        prompt_fn, vid_fn, ramped, max_words, keep_er = VOICES[voice.value]
        try:
            sys_prompt = prompt_fn()
            rule = vc.length_rule(voice.value)
            if rule:
                sys_prompt = f"{sys_prompt}\n\n{rule}"
            resp = await call_llm(
                messages=[{"role": "system", "content": sys_prompt},
                          {"role": "user", "content": block}],
                max_tokens=220, temperature=0.9)
            script = ea.enforce_grade_word((resp.choices[0].message.content or "").strip(), standout)
            cap = min(max_words, vc.word_cap(voice.value))
            if ramped:
                audio, _ = await vc.speak_ramped(
                    script, vid_fn(), vc.TORTS_SPEED_START, vc.TORTS_SPEED_END,
                    end_gain=vc.TORTS_GAIN_END, steps=vc.TORTS_RAMP_STEPS,
                    temp_start=vc.TORTS_TTS_TEMP_START, temp_end=vc.TORTS_TTS_TEMP_END,
                    max_words=cap)
            else:
                audio, _ = await vc.speak(script, voice_id=vid_fn(),
                                          max_words=cap, keep_er=keep_er)
            log_clip(voice.value, script, audio, keep_er=keep_er)
            files.append(discord.File(io.BytesIO(audio),
                                      filename=f"{CLIP_BRAND}-pubscout-{voice.value}.mp3"))
        except Exception as e:
            # a failed clip must not cost the user his card
            print(f"[pubscout] voice failed: {type(e).__name__}: {e}")
            voice_error = f"{type(e).__name__}: {e}"

    # A dropped clip used to be logged server-side and nowhere else: you asked
    # for a voice, got a silent card back, and had no way to tell whether the
    # voice had failed or you'd mis-picked the option. Say it.
    if voice_error:
        await interaction.followup.send(
            f"Card's below -- the **{voice.name}** clip didn't come back: "
            f"`{voice_error}`"[:2000], files=files)
    else:
        await interaction.followup.send(files=files)


# --------------------------------------------------------------- clubscout
CLUB_READ_PROMPT = """You write the headline read at the TOP of a club's scouting
card -- two or three sentences saying what kind of team this is and whether
you'd want to play them. The card shows the record, goals and the roster
right under you, so don't read numbers back; say what they MEAN.

40-55 words, no markdown, no bullets. Blunt, readable, not a bit.

EASHL is 6v6 club hockey with NO lines and NO line changes: every player
is one human at one position for the whole game. Never say "top line",
"second line", "depth", "line changes", "roll four lines", "bench" or any
NHL-lineup language. It's the same five skaters and goalie every shift.

YOU MUST NOT CONTRADICT THE ROSTER SHAPE GRADES -- they're printed on the
card. Use their exact words (elite / stud / solid / mid / weak / bender /
shitter) and no other rating vocabulary. Never invent a stat, never do
arithmetic, never comment on chemistry, coaching or attitude -- there is no
data for those. If the record or goals are missing, don't mention them."""


async def club_autocomplete(interaction: discord.Interaction, current: str):
    if not current or len(current.strip()) < 3:
        return []
    try:
        clubs = await asyncio.wait_for(ea.search_clubs(current, limit=25), timeout=2.5)
    except (asyncio.TimeoutError, Exception):
        return []
    # Label == value, on purpose. The first version decorated the label with
    # the record and division, and Discord on mobile handed THAT back as the
    # club name ("WILDMAN HOCKEY — 50-4-1, div 2"), which EA answered with a
    # 500. The plain name is what gets searched either way.
    out, seen = [], set()
    for c in clubs:
        name = str(c.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(app_commands.Choice(name=name[:100], value=name[:100]))
    return out[:25]


async def _club_voice_file(voice, block: str) -> tuple[discord.File | None, str | None]:
    """A clip reading the club block in the chosen voice, or (None, error).
    Same voices and length rules as /pubscout; the prompts get told it's a
    team, not a player, since the scout prompts were written for one guy."""
    team_note = ("This report is about a CLUB (a whole team and its roster), not one player. "
                 "Talk about the team; name a player or two from the roster if it helps. "
                 "EASHL has no lines and no line changes -- one human per position all game -- "
                 "so never say 'top line', 'depth' or 'line changes'. "
                 "Say a record the way a broadcaster does -- \"fifty, four and one\" -- never "
                 "with dashes, and never as a date.")
    try:
        if voice.value == "narrator":
            with_kid = random.random() < vc.NARRATOR_KID_PROB
            prompt = vc.NARRATOR_SCOUT_PROMPT if with_kid else vc.NARRATOR_SCOUT_SOLO_PROMPT
            prompt = f"{prompt}\n\n{team_note}\n\n{vc.narrator_length_rule(with_kid)}"
            msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": block}]
            resp = await call_llm(messages=msgs, max_tokens=260, temperature=0.9)
            raw = (resp.choices[0].message.content or "").strip()
            if vc.narrator_needs_retry(raw, with_kid):
                fix = msgs + [{"role": "assistant", "content": raw},
                              {"role": "user", "content": vc.narrator_retry_note(with_kid)}]
                resp = await call_llm(messages=fix, max_tokens=260, temperature=0.95)
                retry = (resp.choices[0].message.content or "").strip()
                if not vc.narrator_needs_retry(retry, with_kid):
                    raw = retry
                elif with_kid:
                    turns = vc.parse_narrator_script(raw)
                    raw = f"NARRATOR: {turns[0][1]}" if turns else raw
            raw = clubmod.speakable(raw)
            audio, _ = await vc.speak_narrator(raw, max_words=vc.narrator_word_cap(with_kid))
            log_clip("narrator", " ".join(ln for _, ln in vc.parse_narrator_script(raw)), audio)
            return discord.File(io.BytesIO(audio), filename=f"{CLIP_BRAND}-clubscout-narrator.mp3"), None
        prompt_fn, vid_fn, ramped, max_words, keep_er = VOICES[voice.value]
        sys_prompt = f"{prompt_fn()}\n\n{team_note}"
        rule = vc.length_rule(voice.value)
        if rule:
            sys_prompt = f"{sys_prompt}\n\n{rule}"
        resp = await call_llm(messages=[{"role": "system", "content": sys_prompt},
                                        {"role": "user", "content": block}],
                              max_tokens=220, temperature=0.9)
        script = clubmod.speakable((resp.choices[0].message.content or "").strip())
        cap = min(max_words, vc.word_cap(voice.value))
        if ramped:
            audio, _ = await vc.speak_ramped(
                script, vid_fn(), vc.TORTS_SPEED_START, vc.TORTS_SPEED_END,
                end_gain=vc.TORTS_GAIN_END, steps=vc.TORTS_RAMP_STEPS,
                temp_start=vc.TORTS_TTS_TEMP_START, temp_end=vc.TORTS_TTS_TEMP_END, max_words=cap)
        else:
            audio, _ = await vc.speak(script, voice_id=vid_fn(), max_words=cap, keep_er=keep_er)
        log_clip(voice.value, script, audio, keep_er=keep_er)
        return discord.File(io.BytesIO(audio), filename=f"{CLIP_BRAND}-clubscout-{voice.value}.mp3"), None
    except Exception as e:
        print(f"[clubscout] voice failed: {type(e).__name__}: {e}")
        return None, f"{type(e).__name__}: {e}"


@tree.command(name="clubscout", description="Scout an EA NHL club: record, roster shape, recent form, roster")
@app_commands.describe(club="Club name as it appears in-game",
                       voice="Optionally have the report read out loud")
@app_commands.autocomplete(club=club_autocomplete)
@app_commands.choices(voice=[
    app_commands.Choice(name="Canadian hockey guy", value="buddy"),
    app_commands.Choice(name="Tortorella", value="torts"),
    app_commands.Choice(name="Trump", value="trump"),
    app_commands.Choice(name="Don Cherry", value="cherry"),
    app_commands.Choice(name="1940s Filmstrip", value="narrator"),
    app_commands.Choice(name="Gilbert Gottfried", value="gilbert"),
])
async def clubscout(interaction: discord.Interaction, club: str,
                    voice: app_commands.Choice[str] = None):
    """The card is the report; a voice choice adds a clip alongside it. The
    card goes out as soon as it renders and the clip follows, so nobody
    waits a minute for a picture that was ready in five seconds."""
    await interaction.response.defer()
    q = clubmod.clean_name(club)
    try:
        clubs = await ea.search_clubs(q, limit=5)
    except ea.EAUnavailable as e:
        print(f"[clubscout] EA unavailable for {q!r}: {e}")
        await interaction.followup.send("EA's API isn't answering right now, so I can't look clubs up. "
                                        "That's on EA's end -- try again in a bit.")
        return
    if not clubs:
        await interaction.followup.send(f"EA has no club matching `{q}`. It has to be the club name "
                                        "as spelled in-game -- start typing and pick it off the list.")
        return
    c = clubs[0]
    try:
        detail = await ea.club_detail(c["clubId"], c["_platform"])
    except Exception as e:
        print(f"[clubscout] detail failed for {c.get('name')!r}: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Found **{c.get('name')}** but EA wouldn't hand over its roster "
                                        f"(`{type(e).__name__}`). Try again in a bit.")
        return
    s = clubmod.summarize(c, detail)
    if not s["skaters"] and not s["goalies"]:
        await interaction.followup.send(f"**{s['name']}** exists but EA lists nobody on it with games played "
                                        "this season.")
        return
    block = clubmod.format_block(s)

    # The clip is the slow part (a model call, then 20-40s of TTS), so it
    # starts NOW, alongside the card's own read and render, instead of after
    # the card has gone out. The card still goes out the moment it's ready.
    voice_task = asyncio.create_task(_club_voice_file(voice, block)) if voice else None

    read = None
    try:
        resp = await call_llm(messages=[{"role": "system", "content": CLUB_READ_PROMPT},
                                        {"role": "user", "content": block}],
                              max_tokens=160, temperature=0.6)
        read = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[clubscout] read failed: {type(e).__name__}: {e}")

    try:
        png = await asyncio.to_thread(card.render_club, s, read)
    except Exception as e:
        await interaction.followup.send(f"Card render shit the bed: `{type(e).__name__}: {e}`")
        if voice_task:
            voice_task.cancel()
        return
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", s["name"]).strip("_") or "club"
    note = f"🎙️ {voice.name} clip on the way, about half a minute." if voice else None
    msg = await interaction.followup.send(
        content=note, file=discord.File(io.BytesIO(png), filename=f"{CLIP_BRAND}-clubscout-{safe}.png"),
        wait=True)

    if voice_task:
        f, err = await voice_task
        if f:
            await interaction.followup.send(file=f)
            try:
                await msg.edit(content=None)
            except Exception:
                pass
        else:
            try:
                await msg.edit(content=f"The **{voice.name}** clip didn't come back: `{err}`"[:2000])
            except Exception:
                await interaction.followup.send(f"The **{voice.name}** clip didn't come back: `{err}`"[:2000])


# ----------------------------------------------------------------- matchup
MATCHUP_READ_PROMPT = """You write the game plan at the top of a 5v5 matchup card:
OUR club against THEIR club. The card shows both clubs' shapes, the edge on
every skill, and five pairings (our man vs theirs) with the skill to attack
on each of their guys. Two or three sentences, 40-60 words, no markdown.
EASHL is 6v6 club hockey with NO lines and NO line changes: every player
is one human at one position for the whole game. Never say "top line",
"second line", "depth", "line changes", "roll four lines", "bench" or any
NHL-lineup language. It's the same five skaters and goalie every shift.
Say where the game is won and which of their players to go after, by name.
Use only the card's grade words (elite / stud / solid / mid / weak / bad /
shitter) and never invent a stat. Blunt, readable, not a bit."""


async def _load_club(name: str) -> tuple[dict | None, str | None]:
    """(summary, error). Search + roster/stats/matches for one club."""
    name = clubmod.clean_name(name)
    try:
        clubs = await ea.search_clubs(name, limit=3)
    except ea.EAUnavailable as e:
        print(f"[matchup] EA unavailable for {name!r}: {e}")
        return None, "EA's API isn't answering right now -- that's on EA's end, try again in a bit."
    if not clubs:
        return None, f"EA has no club matching `{name}`. Start typing and pick it off the list."
    c = clubs[0]
    try:
        detail = await ea.club_detail(c["clubId"], c["_platform"])
    except Exception as e:
        print(f"[matchup] detail failed for {c.get('name')!r}: {type(e).__name__}: {e}")
        return None, f"Found **{c.get('name')}** but EA wouldn't hand over its roster (`{type(e).__name__}`)."
    s = clubmod.summarize(c, detail)
    if not s["skaters"]:
        return None, f"**{s['name']}** has nobody with games played this season, so there's nothing to match up."
    return s, None


class MatchupView(discord.ui.View):
    """The buttons under a matchup card. Four of them, one per group:
    YOUR FORWARDS / YOUR D / THEIR FORWARDS / THEIR D. Tap one and that
    group's dropdowns appear (LW, C, RW or LD, RD), each listing the
    club's roster; pick a man and the card re-renders in place. "Back"
    returns to the plain buttons. Anyone in the channel can drive it; it
    goes inert after 15 minutes."""

    GROUPS = {"uf": ("us", ["LW", "C", "RW"]), "ud": ("us", ["LD", "RD"]),
              "tf": ("them", ["LW", "C", "RW"]), "td": ("them", ["LD", "RD"])}

    def __init__(self, a: dict, b: dict, la: dict, lb: dict, read: str | None):
        super().__init__(timeout=900)
        self.a, self.b, self.la, self.lb, self.read = a, b, la, lb, read
        self.group = None
        self.custom = False
        self.message = None
        self._build()

    def _build(self):
        self.clear_items()
        if self.group:
            side, slots = self.GROUPS[self.group]
            team = self.a if side == "us" else self.b
            lineup = self.la if side == "us" else self.lb
            cands = clubmod.candidates(team)
            for slot in slots:
                cur = lineup.get(slot)
                opts = [discord.SelectOption(
                            label=f"{r['name'][:70]}  ·  {r['primary']}, {r['gp']:.0f} GP",
                            value=r["name"][:100],
                            default=bool(cur and r["name"] == cur["name"]))
                        for r in cands] or [discord.SelectOption(label="nobody", value="-")]
                sel = discord.ui.Select(placeholder=f"{'Your' if side == 'us' else 'Their'} {slot}",
                                        options=opts, min_values=1, max_values=1)
                sel.callback = self._pick_cb(slot, side)
                self.add_item(sel)
            back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=4)
            back.callback = self._group_cb(None)
            self.add_item(back)
            return
        for key, label, style in (("uf", "Your forwards", discord.ButtonStyle.primary),
                                  ("ud", "Your D", discord.ButtonStyle.primary),
                                  ("tf", "Their forwards", discord.ButtonStyle.secondary),
                                  ("td", "Their D", discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style, row=0)
            b.callback = self._group_cb(key)
            self.add_item(b)

    def _group_cb(self, key):
        async def cb(interaction: discord.Interaction):
            self.group = key
            self._build()
            await interaction.response.edit_message(view=self)
        return cb

    def _pick_cb(self, slot: str, side: str):
        async def cb(interaction: discord.Interaction):
            sel = next(i for i in self.children if isinstance(i, discord.ui.Select) and i.placeholder.endswith(f" {slot}"))
            name = sel.values[0]
            team = self.a if side == "us" else self.b
            lineup = self.la if side == "us" else self.lb
            pick = next((r for r in clubmod.candidates(team) if r["name"] == name), None)
            if pick is None:
                await interaction.response.defer()
                return
            # a man can only hold one slot: if he was elsewhere, the two swap
            for k, v in list(lineup.items()):
                if v is pick and k != slot:
                    lineup[k] = lineup.get(slot, v)
            lineup[slot] = pick
            self.custom = True
            await interaction.response.defer()
            await self.rerender(interaction)
        return cb

    async def rerender(self, interaction: discord.Interaction):
        pairs = clubmod.pairings(self.la, self.lb)
        block = clubmod.format_matchup(self.a, self.b, pairs)
        read = self.read
        try:
            resp = await call_llm(messages=[{"role": "system", "content": MATCHUP_READ_PROMPT},
                                            {"role": "user", "content": block}],
                                  max_tokens=180, temperature=0.6)
            read = (resp.choices[0].message.content or "").strip() or read
        except Exception as e:
            print(f"[matchup] re-read failed: {type(e).__name__}: {e}")
        self.read = read
        png = await asyncio.to_thread(card.render_matchup, self.a, self.b, pairs, read, self.custom)
        self._build()
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{self.a['name']}-vs-{self.b['name']}").strip("_")
        await interaction.edit_original_response(
            attachments=[discord.File(io.BytesIO(png), filename=f"{CLIP_BRAND}-matchup-{safe}.png")], view=self)

    async def on_timeout(self):
        self.clear_items()
        try:
            if self.message:
                await self.message.edit(view=None)
        except Exception:
            pass


@tree.command(name="matchup", description="5v5 matchup: your club vs theirs -- team shapes overlaid, who to attack")
@app_commands.describe(you="Your club, as spelled in-game", them="The club you're playing")
@app_commands.autocomplete(you=club_autocomplete, them=club_autocomplete)
async def matchup(interaction: discord.Interaction, you: str, them: str):
    await interaction.response.defer()
    (a, err_a), (b, err_b) = await asyncio.gather(_load_club(you), _load_club(them))
    if not a or not b:
        await interaction.followup.send(err_a or err_b)
        return
    la, lb = clubmod.lineup(a), clubmod.lineup(b)
    pairs = clubmod.pairings(la, lb)
    if not pairs:
        await interaction.followup.send("Neither club has five skaters with games played, so there's no lineup to pair.")
        return
    block = clubmod.format_matchup(a, b, pairs)
    read = None
    try:
        resp = await call_llm(messages=[{"role": "system", "content": MATCHUP_READ_PROMPT},
                                        {"role": "user", "content": block}],
                              max_tokens=180, temperature=0.6)
        read = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[matchup] read failed: {type(e).__name__}: {e}")
    try:
        png = await asyncio.to_thread(card.render_matchup, a, b, pairs, read)
    except Exception as e:
        await interaction.followup.send(f"Card render shit the bed: `{type(e).__name__}: {e}`")
        return
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{a['name']}-vs-{b['name']}").strip("_")
    view = MatchupView(a, b, la, lb, read)
    view.message = await interaction.followup.send(
        file=discord.File(io.BytesIO(png), filename=f"{CLIP_BRAND}-matchup-{safe}.png"), view=view, wait=True)


# ------------------------------------------------------------ pool rebuild
# The percentile pool rebuilds itself here, on the bot, because this is the
# one machine EA reliably answers. Weekly, plus at boot if the pool on the
# volume is older than that (a fresh volume starts from the repo's pool.json,
# which is last season's, so the first boot rebuilds straight away). The
# scrape runs in a worker thread with low concurrency and a fail-fast, so a
# rate-limit stops it in a minute rather than getting this IP banned -- and
# a failed rebuild leaves the previous pool exactly as it was.
POOL_REBUILD_DAYS = float(os.getenv("POOL_REBUILD_DAYS", "7"))
# Scheduled rebuilds only start inside this UTC hour window (06-11 UTC is
# 2-7am Eastern: the quietest the server gets) and never more than once a
# day, whatever happened last time. The attempt marker lives next to the
# pool on the volume, so a redeploy can't reset the clock and re-trigger
# a scrape that EA just throttled.
POOL_REBUILD_HOURS_UTC = range(int(os.getenv("POOL_REBUILD_HOUR_FROM", "6")),
                               int(os.getenv("POOL_REBUILD_HOUR_TO", "11")))
POOL_ATTEMPT_GAP_HOURS = float(os.getenv("POOL_ATTEMPT_GAP_HOURS", "20"))
POOL_ATTEMPT_MARK = card.POOL_PATH + ".attempt"
_pool_lock = asyncio.Lock()


def _last_attempt() -> float:
    try:
        return float(open(POOL_ATTEMPT_MARK).read().strip())
    except Exception:
        return 0.0


def _mark_attempt() -> None:
    try:
        with open(POOL_ATTEMPT_MARK, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"[pool] couldn't write attempt marker: {e}", flush=True)


def rebuild_due(now: float | None = None) -> str | None:
    """Why a scheduled rebuild should run now, or None if it shouldn't."""
    import datetime as _dt
    now = time.time() if now is None else now
    if pool_age_days() < POOL_REBUILD_DAYS:
        return None
    if now - _last_attempt() < POOL_ATTEMPT_GAP_HOURS * 3600:
        return None
    if _dt.datetime.fromtimestamp(now, _dt.timezone.utc).hour not in POOL_REBUILD_HOURS_UTC:
        return None
    return f"scheduled: pool {pool_age_days():.0f} days old"


def pool_age_days() -> float:
    built = card.pool().get("meta", {}).get("built")
    try:
        import datetime as _dt
        d = _dt.date.fromisoformat(built)
        return (_dt.date.today() - d).days
    except Exception:
        return 1e9   # unstamped or unreadable -> treat as ancient


async def rebuild_pool(reason: str) -> str:
    """Run the scrape off the event loop; swap the pool in on success."""
    if _pool_lock.locked():
        return "a rebuild is already running"
    async with _pool_lock:
        _mark_attempt()
        print(f"[pool] rebuild starting ({reason}); pool is {pool_age_days():.0f} days old", flush=True)
        t0 = time.monotonic()
        try:
            pool = await asyncio.to_thread(build_pool.rebuild, card.POOL_PATH, card.POOL_PATH)
        except (build_pool.Blocked, build_pool.TooSmall) as e:
            msg = f"rebuild aborted, previous pool kept: {e}"
            print(f"[pool] {msg}", flush=True)
            return msg
        except Exception as e:
            msg = f"rebuild failed, previous pool kept: {type(e).__name__}: {e}"
            print(f"[pool] {msg}", flush=True)
            return msg
        card.reload_pool()
        msg = (f"rebuilt in {(time.monotonic() - t0) / 60:.0f} min -> "
               f"{build_pool.summary(pool)}")
        print(f"[pool] {msg}", flush=True)
        return msg


async def pool_rebuild_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        why = rebuild_due()
        if why:
            await rebuild_pool(why)
        await asyncio.sleep(900)


# ------------------------------------------------------- scout board (web)
# The live scout board: a harvester accumulates EA's shallow match feeds for
# the tracked clubs (the feed only holds ~5 games per mode, so history exists
# only if we poll before the window slides), and an aiohttp server renders
# the board from the accumulated store. Railway routes its domain to PORT.
SCOUT_CLUBS = [c.strip() for c in os.getenv("SCOUT_CLUBS", "12521,22423").split(",") if c.strip()]
SCOUT_TITLE = os.getenv("SCOUT_TITLE", "Wildman vs Entourage")
HARVEST_EVERY = int(os.getenv("HARVEST_EVERY", "14400"))  # 4h


async def harvest_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await asyncio.to_thread(harvest.harvest, SCOUT_CLUBS)
        except ea.RateLimited as e:
            print(f"[harvest] rate limited, backing off: {e}", flush=True)
            await asyncio.sleep(3600)
            continue
        except Exception as e:
            print(f"[harvest] failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(HARVEST_EVERY)


async def _board(request):
    a, b = SCOUT_CLUBS[0], SCOUT_CLUBS[1] if len(SCOUT_CLUBS) > 1 else SCOUT_CLUBS[0]
    html = await asyncio.to_thread(scout.render, a, b, SCOUT_TITLE)
    return web.Response(text=html, content_type="text/html")


async def _health(request):
    return web.Response(text="ok")


async def scout_web():
    app = web.Application()
    app.router.add_get("/", _board)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[scout] board serving on :{port}", flush=True)


@tree.command(name="rebuild-pool", description="Re-sample EA and rebuild the percentile pool now (admins)")
@app_commands.default_permissions(administrator=True)
async def rebuild_pool_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    p = card.pool().get("meta", {})
    await interaction.followup.send(
        f"Rebuilding. Current pool: {p.get('season', '?')}, built {p.get('built', '?')}. "
        "This takes a while; I'll post here when it's done.", ephemeral=True)
    msg = await rebuild_pool(f"/rebuild-pool by {interaction.user}")
    await interaction.followup.send(f"Pool: {msg}"[:2000], ephemeral=True)


HELP = """**ChelScout Pubs**

**Ask a voice something** -- one question in, a clip back. Same for all six.
`/ask-buddy` -- the Canadian hockey guy
`/ask-trump` -- Trump
`/ask-cherry` -- Don Cherry
`/ask-torts` -- Tortorella, mid-presser
`/ask-gilbert` -- Gilbert Gottfried
`/ask-narrator` -- a 1940s classroom filmstrip, with the kid butting in

**Scout a player**
`/pubscout <gamertag>` -- the stat card and a written read.
`/clubscout <club name>` -- a club's record, roster shape, last 10 and roster.
`/matchup <your club> <their club>` -- shapes overlaid, edge per skill, and
your five against theirs with the skill to attack on each of their guys.
Start typing and it suggests real EA gamertags; pick one off the list and you
can't typo it. It has to be the EA gamertag as spelled in-game, not a Discord
name -- and it's case-sensitive on EA's end, so the suggestions are the safe
way in. Add the optional **voice** option and any of the six above reads the
card out loud alongside it.

You can @mention someone in any question -- if his Discord name matches an EA
gamertag closely enough, the answer knows his actual stats."""


@tree.command(name="help", description="What this bot can do, and every voice it can do it in")
async def help_cmd(interaction: discord.Interaction):
    """Only the person who ran it sees this -- a help dump doesn't need to
    sit in the channel forever."""
    await interaction.response.send_message(HELP, ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction,
                               error: app_commands.AppCommandError) -> None:
    """Never leave a command spinning with nothing to show for it.

    Every handler already guards the two calls that actually fail -- the model
    and the voice. Anything OUTSIDE those (Discord itself, an oversized file,
    a plain bug) escaped into discord.py's default handler, which logs to the
    console and says nothing in the channel: the command sat on "thinking"
    until Discord timed it out. That reads as the whole bot being broken
    rather than one request going wrong, which is the worse of the two.
    """
    err = getattr(error, "original", error)
    cmd = interaction.command.name if interaction.command else "?"
    print(f"[error] /{cmd}: {type(err).__name__}: {err}")
    msg = f"`/{cmd}` broke: `{type(err).__name__}: {err}`"[:1900]
    try:
        # Past the defer the only way back in is a followup; before it, the
        # first response is still owed.
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        print(f"[error] couldn't report that in the channel: {type(e).__name__}: {e}")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    print(f"Logged in as {client.user} | model={MODEL}")
    p = card.pool().get("meta", {})
    print(f"[pool] using {card.POOL_PATH}: {p.get('season', 'unstamped')} built "
          f"{p.get('built', '?')} ({pool_age_days():.0f} days old)", flush=True)
    if not getattr(client, "_pool_task", None):
        client._pool_task = client.loop.create_task(pool_rebuild_loop())
    if not getattr(client, "_scout_task", None):
        client._scout_task = client.loop.create_task(harvest_loop())
        client._scout_web = client.loop.create_task(scout_web())

client.run(DISCORD_BOT_TOKEN)