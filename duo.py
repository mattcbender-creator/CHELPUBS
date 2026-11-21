"""/ask-chiclets: Biz & Whit (Spittin' Chiclets) answer a question as one two-voice clip.

Ported from ChelScout (lg-stats-tool, narration/voice.py) where these two voices ship on
/lg card and /lg week. Kept in its own file so voice.py -- and the Trump plumbing in it
(_tts_sync, speak) -- stays untouched.

Voices are Matt's own public Fish clones: BZSP (Biz) and RSP (Whit). RSP was cloned from a
clip Fish flags as multi-speaker, so about one take in four came out as someone else;
sending two Whit-only takes inline as the reference fixed it (48 of 48 Whit, measured on
ChelScout 2026-11-10, Matt: "That worked perfectly"). Fish takes inline audio in a msgpack
body only (base64 in JSON is refused with a 400).

Output is one WAV: turns rendered separately and joined. Glued MP3s played badly in
Discord's player (ChelScout, 2026-11-07), and Railway has no ffmpeg.
"""
import asyncio
import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import msgpack
from curl_cffi import requests

API = "https://api.fish.audio/v1/tts"
BIZ_ID = os.getenv("FISH_VOICE_ID_BIZ", "094e0b5baa994542adf362567694eb16")
WHIT_ID = os.getenv("FISH_VOICE_ID_WHIT", "5108cec8ca634546aa71ec7c7a31ec63")
MODEL = os.getenv("FISH_MODEL_CHICLETS", "s2.1-pro")
WRITER = os.getenv("CHICLETS_MODEL", "anthropic/claude-haiku-5.5")
MAX_WORDS = int(os.getenv("CHICLETS_MAX_WORDS", "125"))
# Haiku 5.5 thinks before it writes; at 700 tokens the thinking ate the whole budget and the
# script came back empty (measured 2026-11-21). Room for both, thinking kept short.
MAX_TOKENS = 3000
REASONING = {"reasoning": {"effort": os.getenv("CHICLETS_EFFORT", "medium")}}
RATE = 44100

_REF_DIR = Path(__file__).parent / "voice_refs"
# Whit-only reference takes for the RSP clone. Keyed by the stored id, so a different Whit
# voice set through FISH_VOICE_ID_WHIT is used as it is.
_REFS = {"5108cec8ca634546aa71ec7c7a31ec63": [
    ("whit_1.wav", "Then Chicago, outside the picture, eleven to one. Five assists on three shots."),
    ("whit_2.wav", "Shots went down though, 2.7 against 3.3, he's trading shots for assists, and I'd take that trade.")]}

PROMPT = """You are writing a short clip of SPITTIN' CHICLETS: Paul Bissonnette ("BIZ") and
Ryan Whitney ("WHIT") answering a question somebody in the Discord sent in, the way they'd
take a listener question on the show. Two ex-pros who've known each other twenty years,
couch volume, just talking. It sounds like a snippet lifted out of a two-hour podcast.
The Discord is chel players: "pubs" means EA NHL online drop-in games (6s, EASHL, club),
not beer league. A question about pubs, a goalie, a club or a build is about the video game,
and they know it -- they've both played plenty of chel.

ALWAYS ANSWER THE ACTUAL QUESTION, whatever it's about: hockey, chel, life, food, a dumb
hypothetical, a chirp at a buddy. Never dodge, never "I'm just a bot", never "it depends".
They pick a side and argue it. If they don't know a FACT they say so in character instead
of making one up -- but they always have an opinion. If the asker uses a slur or crude
language, they chirp HIM for it in one quick line ("oh he went there, alright") and move
on -- never a lecture, never repeat the word.
PEOPLE IN THE DISCORD: they don't know them. Unless a note above says who someone is, they
riff on the type of guy ("every pub goalie thinks he's the best") and never invent things he
did. Never copy a line from these instructions; every bit is new.

WHO THEY ARE. Biz was an enforcer for a decade; he values courage, work, guys who get their
nose dirty, and he is genuinely delighted by hockey -- he talks in run-ons, laughs easily and
at himself, oversells, swears like breathing. Couch volume: he is the energy of the two at
podcast level, not a man yelling at a TV; he gets up for ONE thing, maybe, then settles.
Whit was a top-pair defenseman; dry, Boston, measured, a little tired of Biz and completely
fond of him; he notices the thing nobody else notices, mocks the hype, then agrees with the
point in his own words. They finish each other's thoughts.

TWO MEN, NOT A HOST AND A SIDEKICK. Whit is not sold right away -- he pushes back once, Biz
argues, somebody gives ground. A real "--" interruption is fine. Each reply picks up the
exact word or image the other man just used.

HOW REAL TALK SOUNDS: contractions and dropped words ("gonna", "kinda", "y'know"); false
starts and self-corrections; agreement noises ("Yeah. Yeah, no, I know."); a word repeated
for weight; "like" as the hinge of a sentence. A laugh is never typed ("haha" comes out
fake): it's a cue on real words, [chuckling] or [half laughing] at the start of the line.

THEIR WORDS, a glossary to draw from, NEVER to stuff -- two or three in the whole bit: a
beauty, a dawg, a gamer, sauce, snipe, bar down, sandpaper, greasy, a plug, a pylon, a
pigeon, a floater, dusters, chirp, wheel, tuned, "that's a hockey player", "the boys",
"bro", "I'm not gonna lie", "to be fair", "no shot", "get outta here", "I'm not buying it",
"honestly", "listen".
SWEARING: Biz one to three times, as an intensifier ("a fuckin' wall"), never as the joke
itself, never bleeped. Whit maybe once, dry.

DELIVERY CUES: the voice engine reads short bracketed cues and doesn't speak them. One or two
per turn, at the start and where the tone shifts: [relaxed], [amused], [calm], [chuckling],
[dry], [half laughing], [thinking about it], [speaking slowly], [sarcastic], [trailing off],
[sincere now], [a bit louder]. [excited] at most ONCE in the whole bit. A [break] is a real
beat before a line that lands. Two exclamation marks in the whole bit, maximum.

OPENERS: never the same way twice. Straight into it ("Okay, so this guy wants to know"), a
question to the other man ("Whit, you see this one?"), the take first, a confession ("I'm not
gonna lie"). Don't read the question back word for word.

SHAPE: four to six turns, uneven -- a long one, a one-liner, a long one. It ends on a laugh,
a concession or a throwaway, with the answer clear.
LENGTH: 85 to 110 spoken words, hard cap 120.

FORMAT: one turn per line, each line starting "BIZ:" or "WHIT:" then the spoken words. Cues
go right after the tag ("WHIT: [dry] Oh, he's elite now?"). Nothing else on the page: no
title, no stage directions, no narration between turns."""


# ── cleaning ────────────────────────────────────────────────────────────────
# Fish S2 reads delivery cues in square brackets and does not speak them, including
# free-form ones built from delivery words ("[dry, under his breath]"). A bracket whose
# words are all delivery words stays; anything else is a stage direction and goes.
_CUE_VOCAB = set("""
angry excited proud disgusted frustrated sarcastic confident disappointed surprised calm happy
sad nervous scared worried upset satisfied delighted moved relaxed curious disdainful
contemptuous determined resigned hopeful doubtful confused amused annoyed bored impressed
thinking about it thoughtful louder quieter bigger smaller easy easygoing lazy casual
incredulous exasperated dismissive serious sincere smug mocking teasing deadpan dry flat warm
cold soft loud quiet gentle tender sheepish giddy hyped fired-up fired up wistful grumpy
shouting screaming yelling whispering whisper emphasis emphatic laughing laugh chuckling chuckle
giggling snorting sighing sigh groaning gasping panting exhale inhale breath breathing
break long-break pause short long
speaking talking slowly quickly fast slow softly loudly quietly hesitant hesitantly hurried
hurry rushed rushing trailing off building rising falling low high voice tone
half almost slightly very extremely a bit kind of sort of under his her the and with in of
then now still but like
""".split())
_CUE = re.compile(r"\[([^\]]{1,60})\]")
_LAUGH = re.compile(r"\b(lmao|lmfao|rofl|lol|haha+|hehe+)\b", re.IGNORECASE)
_FILLER = re.compile(r"\b(uh+|um+|er+|erm+|ahh+|hmm+)\b", re.IGNORECASE)


def _keep_cue(m: re.Match) -> str:
    words = m.group(1).strip().lower().replace(",", " ").split()
    if 0 < len(words) <= 7 and all(w.strip("-") in _CUE_VOCAB or w in _CUE_VOCAB for w in words):
        return "[" + " ".join(words) + "]"
    return ""


def clean(text: str) -> str:
    text = _CUE.sub(_keep_cue, text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\*\*?|__?|`+|#+|^\s*[\"“]|[\"”]\s*$", "", text)
    text = _FILLER.sub("", _LAUGH.sub("", text))
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    text = re.sub(r"([,;:])(\s*[,;:])+", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# Every way a writer might tag the two men (a "Whit:" glued to Biz's turn was a real bug).
_ALIASES = {"BIZ": "BIZ", "BIZZ": "BIZ", "PAUL": "BIZ", "BISSONNETTE": "BIZ", "BIZNASTY": "BIZ",
            "WHIT": "WHIT", "WHITT": "WHIT", "WHITTZ": "WHIT", "WHITNEY": "WHIT", "RYAN": "WHIT"}
_TAG = re.compile(r"^\W*([A-Za-z]{2,12})\W*:\s*(.*)$")


def turns(script: str, cap: int = MAX_WORDS) -> list[tuple[str, str]]:
    """[("BIZ"|"WHIT", spoken text)] in order, cut at a turn boundary once past `cap` words.
    An untagged line joins the previous turn (the model wrapped it)."""
    out: list[list[str]] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _TAG.match(line)
        who = _ALIASES.get(m.group(1).upper()) if m else None
        if who:
            out.append([who, m.group(2).strip()])
        elif out:
            out[-1][1] += " " + line
    kept, spoken = [], 0
    for who, text in out:
        text = clean(text)
        words = sum(1 for w in text.split() if not w.startswith("["))
        if kept and spoken + words > cap:
            break
        if words:
            kept.append((who, text)); spoken += words
    return kept


def needs_retry(script: str) -> bool:
    """Both men must speak, at least three turns."""
    t = turns(script)
    return len(t) < 3 or {w for w, _ in t} != {"BIZ", "WHIT"}


# ── speech ──────────────────────────────────────────────────────────────────
_free_until = 0.0   # a 402 (no paid credit) was just seen: go straight to the free model


def _render(who: str, text: str) -> bytes:
    """One turn as a 44.1 kHz WAV. Whit renders cold (his clone drifts warm), Biz warm."""
    global _free_until
    vid = WHIT_ID if who == "WHIT" else BIZ_ID
    temp, top_p = (0.3, 0.5) if who == "WHIT" else (0.65, None)
    body = {"text": text, "format": "wav", "sample_rate": RATE, "normalize": True, "temperature": temp}
    if top_p:
        body["top_p"] = top_p
    refs = [{"audio": (_REF_DIR / f).read_bytes(), "text": t} for f, t in _REFS.get(vid, ())]
    if refs:
        body["references"] = refs
    else:
        body["reference_id"] = vid
    model = "s2.1-pro-free" if time.time() < _free_until else MODEL
    key = os.environ["FISH_API_KEY"]

    def post(m):
        return requests.post(API, headers={"Authorization": f"Bearer {key}", "model": m,
                                           "Content-Type": "application/msgpack"},
                             data=msgpack.packb(body), impersonate="chrome", timeout=45)

    r = post(model)
    for wait in (1.5, 3.0, 5.0):          # Fish caps concurrent requests per account
        if r.status_code != 429:
            break
        time.sleep(wait)
        r = post(model)
    if r.status_code == 402 and model != "s2.1-pro-free":
        # Paid credit out. The free model still voices the public Biz clone and, with the
        # inline reference, Whit (6 of 6 measured on ChelScout 2026-11-10).
        print(f"[chiclets] Fish 402 on {MODEL}; using s2.1-pro-free for 10 min")
        _free_until = time.time() + 600
        r = post("s2.1-pro-free")
    if r.status_code != 200:
        raise RuntimeError(f"Fish Audio {r.status_code}: {r.text[:200]}")
    return r.content


def _pcm(wav: bytes) -> bytes:
    """Sample data of a 16-bit mono WAV (Fish's header carries a bogus length)."""
    i = wav.find(b"data")
    if i < 0 or wav[:4] != b"RIFF":
        raise RuntimeError("Fish did not return a WAV")
    return wav[i + 8:]


def join_wav(pieces: list[bytes], gap_ms: int = 220) -> bytes:
    gap = b"\x00\x00" * int(RATE * gap_ms / 1000)
    data = gap.join(_pcm(p) for p in pieces)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def _speak_sync(script: str) -> bytes:
    t = turns(script)
    if not t:
        raise RuntimeError("script had no BIZ:/WHIT: turns")
    with ThreadPoolExecutor(2) as pool:   # two at a time: Fish's concurrency cap bit at four
        parts = list(pool.map(lambda wt: _render(*wt), t))
    return join_wav(parts)


async def speak(script: str) -> bytes:
    return await asyncio.to_thread(_speak_sync, script)


def spoken_text(script: str) -> str:
    """The script as it reads in Discord: names, no cues."""
    lines = []
    for who, text in turns(script):
        name = "Biz" if who == "BIZ" else "Whit"
        lines.append(f"**{name}:** " + re.sub(r"\[[^\]]*\]\s*", "", text))
    return "\n".join(lines)


if __name__ == "__main__":
    s = ("Some preamble line\nBIZ: [amused] Whit, you see this one? haha\n"
         "Whit: [leans in] [dry] No shot.\nwrapped line\n**WHITNEY:** fine.\nBIZ: [chuckling] Okay, okay.")
    t = turns(s)
    assert [w for w, _ in t] == ["BIZ", "WHIT", "WHIT", "BIZ"], t
    assert t[0][1] == "[amused] Whit, you see this one?", t[0]
    assert t[1][1] == "[dry] No shot. wrapped line", t[1]
    assert not needs_retry(s) and needs_retry("BIZ: hi\nBIZ: again\nBIZ: more")
    assert len(turns("BIZ: " + "word " * 100 + "\nWHIT: " + "word " * 40)) == 1
    hdr = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16 + b"data\xff\xff\xff\xff"
    out = join_wav([hdr + b"\x01\x00", hdr + b"\x02\x00"])
    assert out[:4] == b"RIFF" and struct.unpack("<I", out[40:44])[0] == 4 + int(RATE * 0.22) * 2
    print("duo ok")
