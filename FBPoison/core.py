# fbPoison/fb_core.py
# Core Facebook automation logic, ADB helpers, AI generation, and screenshot.
# Screenshot now uses multiple fallbacks + detailed logging.

import subprocess
import os
from private_settings import get_private_setting
import sys
import time
import glob
import re
import html
import random
import json
import threading
import concurrent.futures
import urllib.request
import urllib.error
import urllib.parse
from typing import Any
import xml.etree.ElementTree as ET
import uiautomator2 as u2

def _utf8_console():
    """Make stdout/stderr survive emoji.

    Account names and captions routinely contain emoji, and this console is
    cp1252. Printing a name like "Ana 💀" raised UnicodeEncodeError from inside
    print(), which killed that device's worker thread mid-run. Replace rather
    than raise: a log line is never worth losing a phone over.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_console()


def _silence_subprocess_windows():
    if sys.platform != "win32" or getattr(subprocess, "_no_window_patched", False):
        return
    _orig_init = subprocess.Popen.__init__
    def _init(self, *args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _init
    subprocess._no_window_patched = True

_silence_subprocess_windows()

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)

def resource_path(*parts):
    for base in (HERE, BUNDLE_DIR):
        path = os.path.join(base, *parts)
        if os.path.exists(path):
            return path
    return os.path.join(HERE, *parts)

def _find_adb():
    import shutil
    # One adb for the whole suite. ADB_PATH, set by the hub, pins every
    # client (the hub's scans and both child apps) to the SAME adb so they
    # share one server instead of fighting over port 5037 - a client of a
    # different version kills the running server and starts its own, which
    # drops phones mid-run when another adb (the phone mirror's) is also up.
    pinned = os.environ.get("ADB_PATH", "").strip()
    if pinned and os.path.isfile(pinned):
        return pinned
    if sys.platform != "win32":
        onpath = shutil.which("adb")
        if onpath:
            return onpath
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(os.path.join(meipass, "platform-tools", "adb.exe"))
    cands.append(os.path.join(os.path.dirname(sys.executable),
                              "platform-tools", "adb.exe"))
    cands.append(os.path.join(HERE, "platform-tools", "adb.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    onpath = shutil.which("adb")
    return onpath if onpath else "adb"

ADB = _find_adb()
FB_PACKAGE = "com.facebook.lite"
FB_ACTIVITY = "com.facebook.lite/.MainActivity"
RECOVER_RETRIES = 3

# ── AI comment generation ──
POLLINATIONS_TEXT_URL = "https://text.pollinations.ai/{prompt}"
GROQ_API_KEY = get_private_setting("groq_api_key", "GROQ_API_KEY")
if not GROQ_API_KEY:
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.1-8b-instant produced broken Tagalog and wrong meanings ("ang dami"
# = many, "talo na ito" = defeated, plus invented non-words). The 70b model
# paraphrases Tagalog/Taglish correctly, which is the whole job here.
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_FALLBACK_MODEL = "llama-3.1-8b-instant"

# This is a PARAPHRASE job, not a "write your own comment" job. The old prompt
# assigned a random tone and "angle" (be excited / share a personal experience /
# add a detail), which made the model invent feelings and facts that were never
# in the example — e.g. "omg ang laki nito" came back as "ang saya saya sabay
# laki naman ito". Same meaning, different words is the whole requirement.
COMMENT_SYSTEM = (
    "You rewrite one Facebook comment using different words.\n"
    "Your output MUST mean EXACTLY the same thing as the example comment: "
    "the same subject, the same opinion, the same feeling.\n"
    "NEVER add a feeling, reason, event, person, place, number or detail that is "
    "not already in the example. Do not explain and do not react to it.\n"
    "Do not change what is being said about the subject, and never say the "
    "opposite of it.\n"
    "Keep the SAME quality being described. If the example says something is "
    "big, your version must still say it is big — not beautiful (ganda), not "
    "many (dami), not strong (lakas), not funny. Only the words may change, "
    "never the quality being talked about.\n"
    "Change ONLY the wording: different words, different word order, "
    "slightly different length.\n"
    "Use the exact same language mix as the example "
    "(English stays English; Tagalog/Taglish stays Taglish). Never translate.\n"
    "Keep it about as short as the example — one short sentence at most.\n"
    "Do NOT use any punctuation marks or symbols at all — no periods, commas, "
    "exclamation points, question marks, quotes, dashes, hashtags, or emoji.\n"
    "Output ONLY the rewritten comment on one line: no quotes, labels, "
    "explanations, or extra lines."
)

# Meaning-preserving variations only — none of these may introduce new content.
_COMMENT_ANGLES = [
    "Use different words for the same idea.",
    "Keep the same words' meaning but change the word order.",
    "Swap the intensifier for another one that means the same.",
    "Say it a bit more casually, same meaning.",
    "Say it slightly shorter, same meaning.",
    "Say it slightly longer, same meaning.",
    "Begin with a different word than the example does.",
    "Use the plainest everyday phrasing for the same idea.",
    "Use a common texting-style spelling, same meaning.",
]

STOP_REQUESTED = None
# Per-THREAD, because every device runs in its own thread. A single global was
# overwritten by whichever device started last, so _is_reel_context() could read
# another phone's URL and send this one down the reel path (or the reverse). It
# was also stale for links 2+ of a mass-links run.
_tls = threading.local()


def set_current_post_url(url):
    _tls.post_url = url


def current_post_url():
    return getattr(_tls, "post_url", None)

def stop_requested():
    try:
        return bool(STOP_REQUESTED and STOP_REQUESTED())
    except Exception:
        return False

# ── Abort support ──
# Abort must be felt immediately, not at the next loop boundary. Every long
# wait in the automation goes through abort_sleep(), so a running action bails
# out within ~0.1s instead of finishing its retry budget.
def abort_sleep(seconds, step=0.1):
    """time.sleep() that returns False the instant Abort is requested."""
    end = time.time() + seconds
    while True:
        if stop_requested():
            return False
        remain = end - time.time()
        if remain <= 0:
            return True
        time.sleep(min(step, remain))

def close_fb(serial):
    """Force-stop Facebook Lite on one device."""
    try:
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial, timeout=15)
        print(f"    [{serial}] Facebook Lite closed.")
        return True
    except Exception as e:
        print(f"    [{serial}] could not close Facebook Lite: {e}")
        return False

def abort_devices(serials):
    """Force-stop Facebook Lite on every given device, in parallel."""
    threads = []
    for s in serials or []:
        t = threading.Thread(target=close_fb, args=(s,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=12)

REACTIONS = {"like", "love", "care", "haha", "wow", "sad", "angry"}
REACTION_ORDER = ["like", "love", "care", "haha", "wow", "sad", "angry"]

REACTION_LABELS = {
    "like":  ["like", "like reaction", "thumbs up"],
    "love":  ["love", "love reaction", "heart"],
    "care":  ["care", "care reaction"],
    "haha":  ["haha", "haha reaction", "ha ha"],
    "wow":   ["wow", "wow reaction"],
    "sad":   ["sad", "sad reaction"],
    "angry": ["angry", "angry reaction"],
}

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
ACCOUNT_SKIP_TEXT = ("dismiss", "switch accounts", "log in to another account",
                     "log into another account", "add account", "create new account",
                     # Buttons that sit in the account-switcher sheet but are NOT
                     # accounts — clicking them navigates away from the task.
                     "accounts center", "account center", "see all profiles",
                     "manage accounts", "add another account", "log out", "logout",
                     # menu chrome sitting behind the switcher sheet
                     "switch account", "view your profile", "see more",
                     "switch accounts",
                     # Filipino builds label the same chrome differently
                     # ("I-dismiss", "Lumipat ng account", ...)
                     "lumipat ng account", "lumipat sa account",
                     "magpalit ng account", "palitan ang account",
                     "mag-log out", "mag-logout", "tingnan ang iyong profile")
# Two tiers, because a "suspended" verdict is the ONLY thing in this file that
# can permanently remove a working account from a phone (it is what calls
# logout_current_account). Getting it wrong is not a retry - it is a login gone
# for good, and that is exactly what was happening: the list used to contain
# the bare word "appeal", so ANY post or comment saying "appeals", "appealed"
# or "Court of Appeals" was read as a suspension screen and the account was
# signed out. Measured: 5 of 5 ordinary captions containing the word tripped
# it, and phones that should hold 10 logins were down to 8-9.
#
# STRONG: Facebook telling THIS user about THEIR account. Nothing else phrases
# itself this way, so one of these alone is proof.
SUSPENDED_STRONG = (
    "your account has been suspended",
    "we suspended your account",
    "your account has been disabled",
    "we disabled your account",
    "suspendido ang iyong account",
    "sinuspinde ang iyong account",
    "na-suspend ang iyong account",
    "na-disable ang iyong account",
    "hindi na pinapagana ang iyong account",
)

# WEAK: real wording on the screen, but equally ordinary words in someone's
# post. Never enough on their own - they must be corroborated by the suspension
# LAYOUT before anything irreversible happens.
SUSPENDED_WEAK = (
    "account suspended",
    "account disabled",
    "suspendido ang account",
    "hindi sumasang-ayon sa desisyon",
    "disagree with decision",
    "mag-apela",
    "submit an appeal",
    "appeal this decision",
    "request a review",
)

# kept for anything still importing the old name
SUSPENDED_MARKERS = SUSPENDED_STRONG + SUSPENDED_WEAK

NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

AI_MIN_GAP = 2.5 if GROQ_API_KEY else 4.0
AI_REFERRER = "fbreactor"
_ai_slot_lock = threading.Lock()
_ai_next_slot = [0.0]
def _ai_wait_slot():
    with _ai_slot_lock:
        start_at = max(time.time(), _ai_next_slot[0])
        _ai_next_slot[0] = start_at + AI_MIN_GAP
    delay = start_at - time.time()
    if delay > 0:
        # abort_sleep, not time.sleep: with many devices this queue can be
        # seconds deep, and PURGE has to cut through it immediately.
        return abort_sleep(delay)
    return True

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002300-\U000023FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D\U0000202A-\U0000202E]",
    flags=re.UNICODE)

_TAGALOG_MARKERS = {
    "ang", "ng", "mga", "naman", "talaga", "ganda", "maganda", "grabe", "sana",
    "ako", "ikaw", "siya", "kami", "tayo", "kayo", "sila", "natin", "namin",
    "ninyo", "po", "opo", "yung", "kasi", "lang", "dito", "diyan", "ganyan",
    "ganito", "astig", "lupit", "petmalu", "idol", "salamat", "pangit", "kain",
    "pwede", "hindi", "wala", "meron", "ba", "nga", "raw", "daw", "ito", "iyan",
    "sobrang", "napaka", "ansaya", "saya", "bongga", "wagi", "keri", "sulit",
    "talaga", "tama", "oo", "oo nga", "eh", "di ba", "ano", "mismo", "sakto",
    "lodi", "paps", "sis", "bes", "mhie", "chika", "chismis", "nakakaloka",
    # Common everyday words that were missing, so short Taglish examples like
    # "wow its big, super laki" were misread as pure English and the model got
    # an ambiguous language instruction.
    "laki", "malaki", "napakalaki", "liit", "maliit", "sobra", "sobrang",
    "ganito", "ganda", "astig", "ang", "yan", "ito", "nito", "sya", "siya",
    "akin", "atin", "grabe", "parang", "medyo", "tapos", "pala", "yata",
}

def _detect_lang(text):
    t = (text or "").strip().lower()
    if not t:
        return "tl"
    if any(ord(c) > 127 and c.isalpha() for c in t):
        return "other"
    words = re.findall(r"[a-z]+", t)
    tagalog_score = sum(1 for w in words if w in _TAGALOG_MARKERS)
    if tagalog_score >= 2:
        return "tl"
    if len(words) > 0 and tagalog_score / len(words) < 0.2:
        return "en"
    return "tl" if tagalog_score > 0 else "en"

def _plain_text(s):
    import unicodedata
    apostrophes = "'’‘`´ʼ"
    out = []
    for ch in (s or ""):
        if ch in apostrophes:
            continue
        out.append(" " if unicodedata.category(ch)[0] in ("P", "S") else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()

def sanitize_comment(text, fallback):
    if not text:
        return fallback
    text = html.unescape(text).strip()
    for tag in ("</s>", "<|end|>", "<|assistant|>", "<|user|>",
                "<|system|>", "assistant", "Comment:", "comment:"):
        text = text.replace(tag, " ")
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    pick = ""
    for ln in lines:
        low = ln.lower()
        if low.startswith(("here", "sure", "this response", "here's", "note",
                           "explanation", "i ", "as an", "okay", "ok,")):
            continue
        pick = ln
        break
    if not pick:
        pick = lines[0] if lines else fallback
    pick = pick.strip().strip('"').strip("'").strip("`").strip()
    pick = re.sub(r"^[\-\*\d\.\)\s]+", "", pick)
    pick = _EMOJI_RE.sub("", pick)
    pick = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", pick)
    pick = _plain_text(pick)
    # "MALAKAS MALAKAS NYA" style shouting reads as generated, not typed.
    letters = [c for c in pick if c.isalpha()]
    if len(letters) > 5 and all(c.isupper() for c in letters):
        pick = pick.lower()
    if len(pick) > 200:
        pick = pick[:200].rsplit(" ", 1)[0].strip()
    if len(pick.split()) < 2 or pick.lower() in ("nice", "good", "great", "ok", "wow", "love"):
        return fallback
    return pick or fallback

def _groq_chat(prompt_text, timeout=20, system=None, max_tokens=60, model=None):
    # Try the good model first; fall back to the small one only if the big one
    # is unavailable (rate limit / decommissioned), so a bad key or a busy
    # endpoint degrades instead of failing outright.
    models = [model] if model else [GROQ_MODEL, GROQ_FALLBACK_MODEL]
    last = None
    for mdl in models:
        payload = json.dumps({
            "model": mdl,
            "messages": [
                {"role": "system", "content": system or COMMENT_SYSTEM},
                {"role": "user",   "content": prompt_text},
            ],
            "temperature": 0.85,
            "top_p": 0.9,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            GROQ_URL, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {GROQ_API_KEY}",
                     "User-Agent": "Mozilla/5.0"})
        try:
            with NOPROXY.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            continue
    raise last if last else RuntimeError("groq: no model available")

def _pollinations_get(prompt_text, timeout=35, system=None):
    seed = random.randint(1, 999999)
    encoded = urllib.parse.quote(prompt_text, safe="")
    url = (f"https://text.pollinations.ai/{encoded}"
           f"?seed={seed}&model=openai-fast&referrer={AI_REFERRER}"
           f"&system={urllib.parse.quote(system or COMMENT_SYSTEM, safe='')}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": AI_REFERRER})
    with NOPROXY.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()

def _pollinations_post(prompt_text, timeout=30, system=None, max_tokens=60):
    import urllib.parse
    urls = [
        "https://text.pollinations.ai/openai",
        "https://api.pollinations.ai/v1/chat/completions",
    ]
    payload = json.dumps({
        "model": "openai",
        "messages": [
            {"role": "system", "content": system or COMMENT_SYSTEM},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.65,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "seed": random.randint(1, 1_000_000),
    }).encode("utf-8")
    for url in urls:
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with NOPROXY.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except Exception:
            continue
    raise RuntimeError("all Pollinations POST endpoints failed")

def _good_variation(out, example):
    """A usable paraphrase: not a copy, not a fragment, not a ramble.

    Length is the cheap signal for drift — once the model runs long it starts
    inventing content that was never in the example.
    """
    if not out:
        return False
    if out.lower() == example.lower():
        return False
    words = out.split()
    if len(words) < 2:
        return False
    limit = max(6, len(example.split()) * 2 + 3)
    if len(words) > limit:
        print(f"  [ai] rejected (too long, {len(words)}w > {limit}w): {out[:60]}")
        return False
    # The model sometimes leaks the transformation itself, e.g.
    # "ang laki nito becomes whew ang laki nito".
    low = f" {out.lower()} "
    for meta in (" becomes ", " rewritten ", " rewrite ", " paraphrase ",
                 " version ", " original ", " example ", " output ",
                 " same meaning ", " translates "):
        if meta in low:
            print(f"  [ai] rejected (leaked instruction text): {out[:60]}")
            return False
    return True

def generate_comment(example, retries=3, avoid_openings=None, tone=None):
    example = example.strip()
    lang_hint = {
        "tl": "Tagalog / Taglish (Filipino) — the same Tagalog-and-English mix",
        "en": "the exact same language as the example (do not translate it)",
        "other": "the exact same language as the example (do not translate it)",
    }[_detect_lang(example)]
    # No random "tone": forcing a mood the example never had is exactly what
    # produced off-meaning comments. Only meaning-preserving rewording varies.
    angle = random.choice(_COMMENT_ANGLES)
    avoid = ""
    if avoid_openings:
        sample = [o for o in avoid_openings if o][-12:]
        if sample:
            avoid = (" Other people already began their comments with: "
                     + "; ".join(f'"{o}"' for o in sample)
                     + ". Start with clearly different words than those, but do"
                       " NOT change the meaning to do it.")
    prompt_text = (
        "Example Facebook comment (between the triple quotes):\n"
        f'"""{example}"""\n\n'
        f"Rewrite that comment in {lang_hint}. It must mean EXACTLY the same "
        "thing as the example — same subject, same opinion, same feeling — but "
        "worded differently, as if a different person typed the same reaction. "
        f"Variation to apply: {angle} "
        "Add NOTHING that is not already in the example: no new feelings, no "
        "reasons, no extra details, no greetings. Never say the opposite of the "
        "example. Keep it about the same length as the example and no longer "
        "than one short sentence."
        + avoid +
        " Reply with only the rewritten comment, on one line."
    )

    for attempt in range(retries):
        if not _ai_wait_slot():
            return example          # PURGE: don't start a fresh AI call
        if GROQ_API_KEY:
            try:
                raw = _groq_chat(prompt_text, max_tokens=40)
                out = sanitize_comment(raw, example)
                if _good_variation(out, example):
                    return out
            except Exception as e_groq:
                print(f"  [ai] Groq attempt {attempt+1} failed: {e_groq}")
        try:
            raw = _pollinations_get(prompt_text)
            out = sanitize_comment(raw, example)
            if _good_variation(out, example):
                return out
        except Exception as e_get:
            print(f"  [ai] GET attempt {attempt+1} failed: {e_get}")
        try:
            raw = _pollinations_post(prompt_text, max_tokens=40)
            out = sanitize_comment(raw, example)
            if _good_variation(out, example):
                return out
        except Exception as e_post:
            print(f"  [ai] POST attempt {attempt+1} failed: {e_post}")

    print("  [ai] all AI attempts failed, using original example as comment")
    return example

def ai_reply(prompt_text, system=None, max_tokens=90, retries=2):
    for _ in range(retries):
        if not _ai_wait_slot():
            return ""               # PURGE: don't start a fresh AI call
        if GROQ_API_KEY:
            try:
                r = _groq_chat(prompt_text, system=system, max_tokens=max_tokens)
                if r and r.strip():
                    return r
            except Exception:
                pass
        try:
            r = _pollinations_get(prompt_text, system=system)
            if r and r.strip():
                return r
        except Exception:
            pass
        try:
            r = _pollinations_post(prompt_text, system=system, max_tokens=max_tokens)
            if r and r.strip():
                return r
        except Exception:
            pass
    return ""

def start_llama():
    if GROQ_API_KEY:
        print("  [ai] Checking Groq AI (free tier)...")
        try:
            _groq_chat("hi", timeout=15)
            print("  [ai] Groq AI ready.")
            return True
        except Exception as e:
            print(f"  [ai] Groq check failed ({e}); falling back to Pollinations...")
    import urllib.parse
    print("  [ai] Checking Pollinations AI (plain-text endpoint)...")
    try:
        req = urllib.request.Request(
            "https://text.pollinations.ai/hi?model=openai-fast",
            headers={"User-Agent": "Mozilla/5.0"})
        with NOPROXY.open(req, timeout=12) as resp:
            resp.read()
        print("  [ai] Pollinations AI ready.")
        return True
    except Exception as e:
        print(f"  [ai] GET endpoint failed ({e}), trying POST fallback...")
        try:
            _pollinations_post("hi", timeout=15)
            print("  [ai] Pollinations POST fallback ready.")
            return True
        except Exception as e2:
            print(f"  [ai] Pollinations AI fully unreachable: {e2}")
            return False

def stop_llama():
    pass

# Expanded comment pools (fallback only)
COMMENT_POOL = [
    "Grabe ang ganda!", "Sobrang ganda nito!", "Ang galing naman!", "Nice one!",
    "Astig nito!", "Ganda ah!", "Love it!", "Solid to!", "Petmalu talaga!",
    "Angas nito!", "Sana all", "Ang cute naman!", "Grabe idol!", "Perfect to!",
    "Napakaganda!", "Wow ang ganda!", "The best talaga!", "Sobrang lupit!",
    "Ganda ng vibes!", "Idol talaga!", "Bongga!", "Ang saya nito!", "Goals to!",
    "Napa-wow ako!", "Ang husay!", "Ansaya naman!", "Legit ang ganda!",
    "Love this!", "Ang ganda ng shot!", "Panalo to!", "Sobrang cute!", "Grabe ka!",
    "Ganda ganda!", "Good vibes!", "Nice nice!", "Napakaganda talaga!", "Superb!",
    "Ang ganda ha!", "Wagi to!", "Sulit na sulit!", "Ang ganda ng kuha!",
    "Fave ko to!", "Grabe sa ganda!", "Keri to!", "Ganda promise!",
    "Two thumbs up!", "Ang ganda ng araw!", "Wow lang!", "Ang ganda talaga!",
    "Ang lupit nito!", "Grabeng ganda naman!", "Sobrang worth it!", "Ang refreshing!",
    "Nakakatuwa!", "Ang galing!", "Panalo ang ganda!", "Walang kupas!",
    "Gandang-ganda ako!", "Sobrang nakaka-inspire!", "Ang saya-saya!",
    "Ganda ng pagkakakuha!", "Solid na solid!", "Ang ganda ng kulay!",
    "Napaka-creative!", "Ang galing ng shots!", "Sobrang aesthetic!",
    "Gandang tanawin!", "Ang sarap balikan!", "Ang ganda ng place!",
]

ENGLISH_POOL = [
    "This is amazing!", "So good!", "Love this!", "Absolutely stunning!",
    "Incredible shot!", "This looks great!", "Wow, beautiful!", "Nailed it!",
    "So cool!", "This is the best!", "Looking good!", "Perfect!",
    "That's awesome!", "Big fan of this!", "Superb!", "Well done!",
    "Love the vibe!", "This made my day!", "Pure gold!", "Top notch!",
    "Simply the best!", "So impressive!", "This is fire!", "Gorgeous!",
    "Amazing work!", "Keep it up!", "So inspiring!", "This is everything!",
    "Beautiful capture!", "Really nice!", "Outstanding!", "Loving this!",
    "Way to go!", "This hits different!", "Stunning as always!", "So clean!",
    "Great job!", "This is a whole vibe!", "Total goals!", "Absolute perfection!",
    "Obsessed with this!", "You crushed it!", "Right on point!", "Legendary!",
    "This deserves more love!", "Instant favorite!", "So worth it!",
    "Can't get enough!", "This is perfection!", "Masterpiece!",
    "I'm blown away!", "This is incredible!", "So happy for you!",
    "Fantastic!", "Brilliant!", "What a gem!", "This is gold!",
    "So proud!", "Amazing effort!", "This is next level!", "Inspiring!",
]

def _example_variants(example):
    e = example.strip()
    core = e.rstrip(" .!?…")
    outs = [e, core + "!", core + "!!", core + "...", core + " " + core]
    seen, res = set(), []
    for o in outs:
        k = o.strip().lower()
        if o.strip() and k not in seen:
            seen.add(k)
            res.append(o.strip())
    return res

def _opening_key(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return " ".join(words[:2])

_used_comments = set()
_used_openings = set()
_used_lock = threading.Lock()
_ai_fails = [0]
_ai_off = [False]

def reset_comment_pool():
    with _used_lock:
        _used_comments.clear()
        _used_openings.clear()
        _ai_fails[0] = 0
        _ai_off[0] = False

# Occasional light misspellings on purpose — they make the comments read like a
# real person typing on a phone rather than generated text.
TYPO_PROB = 0.30

def humanize_typo(text, prob=None):
    prob = TYPO_PROB if prob is None else prob
    if not text or len(text) < 6 or random.random() >= prob:
        return text
    words = text.split(" ")
    idxs = [i for i, w in enumerate(words) if len(w) >= 4]
    if not idxs:
        return text
    i = random.choice(idxs)
    w = words[i]
    j = random.randint(1, len(w) - 2)
    kind = random.choice(("swap", "swap", "drop", "double"))
    if kind == "swap":
        w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
    elif kind == "drop":
        w = w[:j] + w[j + 1:]
    else:
        w = w[:j] + w[j] + w[j:]
    words[i] = w
    return " ".join(words)

def unique_comment(example=""):
    example = (example or "").strip()
    if example and not _ai_off[0]:
        with _used_lock:
            avoid = set(_used_openings)
        c = ""
        for _try in range(2):
            try:
                c = (generate_comment(example, retries=2, avoid_openings=avoid) or "").strip()
            except Exception:
                c = ""
            if not c or c.lower() == example.lower():
                c = ""
                break
            op = _opening_key(c)
            with _used_lock:
                fresh_text = c.lower() not in _used_comments
                fresh_open = op not in _used_openings
                if fresh_text and (fresh_open or _try == 1):
                    _ai_fails[0] = 0
                    _used_comments.add(c.lower())
                    if op:
                        _used_openings.add(op)
                    return c
            avoid = avoid | {op}
        with _used_lock:
            _ai_fails[0] += 1
            if _ai_fails[0] >= 6:
                _ai_off[0] = True
    lang = _detect_lang(example) if example else "tl"
    if lang == "en":
        base_pool = ENGLISH_POOL
    elif lang == "other" and example:
        base_pool = _example_variants(example)
    else:
        base_pool = COMMENT_POOL
    pool = [_plain_text(x) for x in base_pool if _plain_text(x)]
    random.shuffle(pool)
    with _used_lock:
        for c in pool:
            if c.lower() not in _used_comments:
                _used_comments.add(c.lower())
                return c
        src = pool if pool else [_plain_text(x) for x in COMMENT_POOL]
        for _ in range(80):
            combo = f"{random.choice(src)} {random.choice(src)}"
            if combo.lower() not in _used_comments:
                _used_comments.add(combo.lower())
                return combo
        return random.choice(src)

# ── IMPROVED Screenshot function with triple fallback ──
# Shots are saved to the PC (screenshots/<serial>/account_<n>/) AND kept on the
# phone itself (DEVICE_SHOT_DIR, which shows up in the Gallery) when
# keep_on_device is set.
DEVICE_SHOT_DIR = "/sdcard/Pictures/FBReactor"

def _scan_device_media(serial, device_path):
    # Nudge the phone so a freshly-saved image appears in Gallery/Photos.
    try:
        subprocess.run([ADB, "-s", serial, "shell", "am", "broadcast",
                        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                        "-d", f"file://{device_path}"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

def _push_shot_to_device(serial, pc_path, filename):
    # Copy a PC-side screenshot back onto the phone so it also lives in the gallery.
    try:
        device_path = f"{DEVICE_SHOT_DIR}/{filename}"
        subprocess.run([ADB, "-s", serial, "shell", "mkdir", "-p", DEVICE_SHOT_DIR],
                       capture_output=True, timeout=10)
        push = subprocess.run([ADB, "-s", serial, "push", pc_path, device_path],
                              capture_output=True, timeout=20)
        if push.returncode == 0:
            _scan_device_media(serial, device_path)
            print(f"    [SCREENSHOT] also saved on device: {device_path}")
            return True
        print(f"    [SCREENSHOT] push to device failed (code {push.returncode})")
    except Exception as e:
        print(f"    [SCREENSHOT] push-to-device exception: {e}")
    return False

def take_screenshot(serial, account_num, action, d=None, keep_on_device=True):
    try:
        # Serials for TCP/wifi/emulator devices look like "127.0.0.1:5555" — the
        # ':' (and other reserved chars) are illegal in Windows paths, so makedirs
        # would raise WinError 267 and the shot would silently fail. Sanitize the
        # serial for filesystem use while still passing the real serial to adb.
        safe_serial = re.sub(r'[<>:"/\\|?*]', '_', str(serial)).strip() or "device"
        shot_dir = os.path.join(HERE, "screenshots", safe_serial, f"account_{account_num}")
        os.makedirs(shot_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"fb_{safe_serial}_acc{account_num}_{action}_{timestamp}.png"
        filepath = os.path.join(shot_dir, filename)
        device_path = f"{DEVICE_SHOT_DIR}/{filename}"

        # ── Method 1: capture straight to a file ON THE PHONE, then pull a copy to
        # the PC. This is the only method that leaves the shot in the phone's
        # gallery, so it runs first whenever keep_on_device is requested.
        if keep_on_device:
            print(f"    [SCREENSHOT] capturing on device for {serial}...")
            try:
                subprocess.run([ADB, "-s", serial, "shell", "mkdir", "-p", DEVICE_SHOT_DIR],
                               capture_output=True, timeout=10)
                cap = subprocess.run([ADB, "-s", serial, "shell", "screencap", "-p", device_path],
                                     capture_output=True, timeout=15)
                if cap.returncode == 0:
                    pull = subprocess.run([ADB, "-s", serial, "pull", device_path, filepath],
                                          capture_output=True, timeout=20)
                    _scan_device_media(serial, device_path)
                    if pull.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        print(f"    [SCREENSHOT] OK saved on device ({device_path}) and PC ({filepath})")
                        return filepath
                    print(f"    [SCREENSHOT] device pull failed (code {pull.returncode}): "
                          f"{pull.stderr.decode(errors='replace')}")
                else:
                    print(f"    [SCREENSHOT] on-device screencap failed: "
                          f"{cap.stderr.decode(errors='replace')}")
            except Exception as e:
                print(f"    [SCREENSHOT] device-save method exception: {e}")

        # ── Method 2: exec-out screencap (binary-safe, PC only). ──
        print(f"    [SCREENSHOT] trying screencap (binary safe) for {serial}...")
        try:
            cmd = [ADB, "-s", serial, "exec-out", "screencap", "-p"]
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0 and len(result.stdout) > 0:
                with open(filepath, "wb") as f:
                    f.write(result.stdout)
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    print(f"    [SCREENSHOT] OK (exec-out) saved to {filepath}")
                    if keep_on_device:
                        _push_shot_to_device(serial, filepath, filename)
                    return filepath
            print(f"    [SCREENSHOT] exec-out failed(size {len(result.stdout)})")
        except Exception as e:
            print(f"    [SCREENSHOT] exec-out exception: {e}")
        print(f"    trying another method + pull for {serial}...")
        try:
            temp_path = "/sdcard/temp_screenshot.png"
            capture_cmd = [ADB, "-s", serial, "shell", "screencap", "-p", temp_path]
            cap_result = subprocess.run(capture_cmd, capture_output=True, timeout=10)
            if cap_result.returncode == 0:
                pull_cmd = [ADB, "-s", serial, "pull", temp_path, filepath]
                pull_result = subprocess.run(pull_cmd, capture_output=True, timeout=10)
                subprocess.run([ADB, "-s", serial, "shell", "rm", temp_path], capture_output=True, timeout=5)
                if pull_result.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    print(f"    [SCREENSHOT] Ok (pull) saved to {filepath}")
                    if keep_on_device:
                        _push_shot_to_device(serial, filepath, filename)
                    return filepath
                else:
                    print(f"    [SCREENSHOT] pull failed (code {pull_result.returncode}): "
                        f"{pull_result.stderr.decode(errors='replace')}")
                    if os.path.exists(filepath):
                        os.remove(filepath)
            else:
                print(f"    [SCREENSHOT] screencap failed: {cap_result.stderr.decode(errors='replace')}")
        except Exception as e:
            print(f"    [SCREENSHOT pull-method exception: {e}]")
        if d is not None:
            print(f"    [SCREENSHOT] trying uiautomator2 for {serial}...")
            try:
                img = d.screenshot()
                if img is not None:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(filepath, "PNG")
                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        print(f"    [SCREENSHOT] OK (u2) saved to {filepath}")
                        if keep_on_device:
                            _push_shot_to_device(serial, filepath, filename)
                        return filepath
                    else:
                        print(f"    [SCREENSHOT] u2 saved empty file")
                        if os.path.exists(filepath):
                            os.remove(filepath)
                else:
                    print(f"    [SCREENSHOT] u2 returned no image")
            except Exception as e:
                print(f"    [SCREENSHOT] u2 failed: {e}")

        print(f"    [SCREENSHOT] all methods failed for {serial}")
        return None
    except Exception as e:
        print(f"  [screenshot] {serial} error: {e}")
        return None

def _keyboard_shown(serial):
    # True when the soft keyboard/IME is currently covering part of the screen.
    try:
        _, out, _ = run(["shell", "dumpsys", "input_method"], serial=serial)
        return "mInputShown=true" in out
    except Exception:
        return False

def prepare_post_view_for_shot(serial, d):
    # After posting a comment the soft keyboard usually still covers half the
    # screen. Drop it (a BACK press, only while the keyboard is actually up) so a
    # single screenshot can show BOTH the reaction on the post and the comment
    # that was just posted. The is-shown guard prevents an extra BACK from
    # navigating away from the post.
    try:
        for _ in range(2):
            if not _keyboard_shown(serial):
                break
            run(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial)
            time.sleep(0.8)
    except Exception:
        pass
    try:
        poll_until(lambda: post_ready(d), 3)
    except Exception:
        pass
    time.sleep(0.6)
# ── ADB helpers ──

def run(args, serial=None, timeout=60):
    cmd = [ADB]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        # encoding must be pinned: text=True decodes with the Windows locale
        # (cp1252), and ADB output routinely carries emoji and non-Latin text -
        # names, captions, dumpsys. That raised UnicodeDecodeError inside
        # subprocess' reader thread and handed back EMPTY output, which then
        # looked like "the switcher would not open" or "got the system window".
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except UnicodeError as e:
        print(f"  [adb] output decode failed: {e}")
        return 1, "", "decode-error"

_BOX_ROLE = "poison"


def _box_config_path():
    """Find boxes.json from wherever this app was started.

    A frozen build runs from dist/, and copies of this app live outside the
    suite entirely, so the config sits several levels up rather than next
    door. Missing it would leave the app unfiltered - the exact failure this
    guard exists to prevent - so search upward from every plausible root.
    """
    override = os.environ.get("FBVENOISON_CONFIG", "").strip()
    if override and os.path.isfile(override):
        return override
    roots = [HERE, os.path.dirname(os.path.abspath(sys.argv[0] or ".")),
             os.path.dirname(os.path.abspath(sys.executable))]
    for root in roots:
        directory = os.path.abspath(root)
        for _ in range(6):
            for parts in (("boxes.json",), ("FBVenoison", "boxes.json")):
                path = os.path.join(directory, *parts)
                if os.path.isfile(path):
                    return path
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return None


def _box_allow():
    """Serials this app may drive, or None when no phone box is configured.

    The hub passes its box in FBVENOISON_DEVICES, but only through its own
    launcher. Reading boxes.json here as well keeps the other box invisible
    when the app is started on its own, where that variable is never set.
    """
    env = {s.strip() for s in os.environ.get("FBVENOISON_DEVICES", "").split(",")
           if s.strip()}
    if env:
        return env
    path = _box_config_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            assign = json.load(fh).get("assign")
    except Exception as e:
        # Grabbing every phone is the failure this guard exists to prevent.
        print(f"[box] {path} unreadable ({e}); driving no phone")
        return set()
    if not isinstance(assign, dict) or not assign:
        return None
    return {s for s, role in assign.items() if role == _BOX_ROLE}


def list_devices():
    _, out, _ = run(["devices"])
    devices = {}
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        serial, _, state = line.partition("\t")
        serial = serial.strip()
        state  = state.strip()
        if serial:
            devices[serial] = state or "unknown"
    allow = _box_allow()
    if allow is not None:
        devices = {s: st for s, st in devices.items() if s in allow}
    return devices

def ensure_online():
    devices = list_devices()
    for attempt in range(1, RECOVER_RETRIES + 1):
        bad = {s: st for s, st in devices.items()
               if st in ("offline", "unauthorized", "unknown")}
        if not bad:
            break
        print(f"[recover] attempt {attempt}: {len(bad)} phone(s) not ready, trying to recover...")
        run(["reconnect", "offline"])
        time.sleep(2)
        devices = list_devices()
        if any(st in ("offline", "unauthorized", "unknown") for st in devices.values()):
            run(["kill-server"])
            run(["start-server"])
            time.sleep(3)
            devices = list_devices()
    return devices

def has_fb(serial):
    _, out, _ = run(["shell", "pm", "list", "packages", FB_PACKAGE], serial=serial)
    return FB_PACKAGE in out

def find_apk():
    candidates = glob.glob(os.path.join(HERE, "*.apk"))
    if not candidates:
        return None
    for c in candidates:
        name = os.path.basename(c).lower()
        if "lite" in name or "facebook" in name:
            return c
    return candidates[0]

# URL cleanup is deliberately local and deterministic. Redirect-only links are
# resolved below, once per run, after this stage has made both apps agree on
# the same spelling (including the exact /share/v/<token> form).
_FB_WEB_HOST_RE = re.compile(r"(?:^|\.)facebook\.com$|(?:^|\.)fb\.com$", re.I)
_FB_SHORT_HOSTS = {"fb.watch", "www.fb.watch", "fb.me", "www.fb.me",
                   "fb.gg", "www.fb.gg"}
_FB_TRACKING_KEYS = {
    "fbclid", "mibextid", "sfnsn", "rdid", "extid", "ref", "refid",
    "refsrc", "paipv", "eav", "locale", "acontext", "app", "app_id",
    "notif_id", "notif_t", "hc_ref", "__cft__", "__tn__", "_rdr",
}


def _facebook_host(host):
    host = str(host or "").lower().rstrip(".")
    return bool(_FB_WEB_HOST_RE.search(host) or host in _FB_SHORT_HOSTS)


def _url_from_pasted_text(raw):
    """Extract one Facebook link from copied text, preserving a bare URL."""
    text = html.unescape(str(raw or "")).replace("\\/", "/").strip()
    text = text.strip('"').strip("'").strip()
    if not text:
        return ""
    m = re.search(
        r"(?i)(?:(?:facebook|fb)://[^\s<>]+|"
        r"(?:https?://|//)?(?:[a-z0-9-]+\.)*(?:facebook|fb)\.com/[^\s<>]+|"
        r"(?:https?://|//)?(?:www\.)?(?:fb\.watch|fb\.me|fb\.gg)/[^\s<>]+)",
        text)
    if m:
        text = m.group(0)
    return text.rstrip(".,;!)]}>")


def _drop_fb_tracking(query):
    kept = []
    for key, value in urllib.parse.parse_qsl(query or "", keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in _FB_TRACKING_KEYS or low.startswith("__"):
            continue
        kept.append((key, value))
    return urllib.parse.urlencode(kept, doseq=True)


def normalize_url(raw):
    """Any shape of Facebook link -> one the phone can be told to open.

    Every form this fleet has actually been handed goes through here: share
    links (/share/p/, /share/r/, /share/v/), page and profile permalinks with a
    numeric id or a pfbid, /reel/, /watch/?v=, /videos/, /photo/?fbid=,
    permalink.php, group posts, fb.watch shorteners, m./mbasic./web. hosts, and
    all of the above wrapped in l.facebook.com redirects or trailing tracking
    parameters.
    """
    url = _url_from_pasted_text(raw)
    if not url:
        return ""
    scheme = re.match(r"(?i)^(facebook|fb)://", url)
    if scheme:
        return scheme.group(1).lower() + "://" + url[scheme.end():]

    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not _facebook_host(host):
        return ""

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query_pairs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

    # Unwrap Facebook's outbound/share wrappers only when their destination is
    # itself Facebook. External links are never turned into arbitrary openers.
    if (_FB_WEB_HOST_RE.search(host) and
            path.lower().rstrip("/") in ("/l.php", "/share.php")):
        nested = (query_pairs.get("u") or query_pairs.get("href") or [""])[0]
        if nested:
            unwrapped = normalize_url(urllib.parse.unquote(nested))
            if unwrapped:
                return unwrapped

    clean_host = host if host in _FB_SHORT_HOSTS else "www.facebook.com"
    if clean_host == "www.fb.watch":
        clean_host = "fb.watch"
    elif clean_host == "www.fb.me":
        clean_host = "fb.me"
    elif clean_host == "www.fb.gg":
        clean_host = "fb.gg"

    if path.lower().endswith("/amp"):
        path = path[:-4] or "/"
    if path != "/":
        path = path.rstrip("/")
    query = _drop_fb_tracking(parts.query)
    return urllib.parse.urlunsplit(("https", clean_host, path, query, ""))

def parse_action_mode(mode):
    raw     = (mode or "").strip().lower()
    compact = re.sub(r"[^a-z]+", "", raw)
    if compact in ("", "r", "react", "reaction"):
        return True, False
    if compact in ("c", "comment", "comments"):
        return False, True
    if compact in ("b", "both", "rc", "cr",
                   "reactcomment", "commentreact",
                   "reactandcomment", "commentandreact",
                   "reactcomments", "commentsreact"):
        return True, True
    return False, False

def install_fb(serial, apk):
    print(f"  [install] {serial} <- {os.path.basename(apk)} ...")
    rc, out, err = run(["install", "-r", apk], serial=serial, timeout=300)
    ok = "Success" in out or "Success" in err
    return ok and has_fb(serial)

def connect(serial):
    try:
        return u2.connect(serial)
    except Exception as e:
        print(f"  [!] {serial} u2 connect failed: {e}")
        return None

def device_state(serial):
    return list_devices().get(serial, "missing")

def wait_online(serial):
    delay    = 2
    announced = False
    while True:
        if stop_requested():
            return False
        st = device_state(serial)
        if st == "device":
            if announced:
                print(f"    [{serial}] back online.")
            return True
        if st == "unauthorized":
            return False
        if not announced:
            print(f"    [{serial}] {st} - waiting for it to come back...")
            announced = True
        run(["reconnect", "offline"])
        run(["reconnect"], serial=serial)
        run(["wait-for-device"], serial=serial, timeout=20)
        if device_state(serial) == "device":
            if announced:
                print(f"    [{serial}] back online.")
            return True
        time.sleep(delay)
        delay = min(delay + 2, 15)

def collapse_shade(serial):
    """Close the notification / quick-settings panel.

    A pulled-down shade owns the ACTIVE window, so uiautomator dumps SystemUI
    instead of Facebook and every detector reads an empty screen - the post is
    on the phone, the run cannot see it, the link is skipped. safe_dump() has a
    nudge for this but only fires it while Facebook is in the foreground, which
    is exactly what a raised shade is not. Seen live on C9021569, sitting on
    quick settings while the run failed every link.
    """
    try:
        run(["shell", "service", "call", "statusbar", "2"], serial=serial)
    except Exception:
        pass


def wake_and_unlock(serial):
    # Do this first, every time: it is one cheap call and it removes the single
    # most common reason a healthy phone reads as a blank screen.
    collapse_shade(serial)
    for attempt in range(3):
        # Screen-wake retries used to hold Abort up for ~9s; bail out at once.
        if stop_requested():
            return
        _, out, _ = run(["shell", "dumpsys", "display"], serial=serial)
        is_off = "mScreenState=OFF" in out
        if is_off:
            run(["shell", "input", "keyevent", "224"], serial=serial)
            if not abort_sleep(1.5):
                return
        _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
        keyguard_active = ("mKeyguard" in out and
                           "mKeyguardOccluded=true" not in out and
                           ("isStatusBarKeyguard" in out or
                            "mKeyguardDrawComplete" in out))
        if not keyguard_active:
            return
        w, h = 720, 1600
        for line in out.splitlines():
            if "Display" in line and "init=" in line:
                m = re.search(r'init=\((\d+),(\d+)\)', line)
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
        if stop_requested():
            return
        run(["shell", "input", "swipe",
             str(w // 2), str(h - 120),
             str(w // 2), str(h // 4)], serial=serial)
        if not abort_sleep(1.5):
            return
        run(["shell", "input", "keyevent", "82"], serial=serial)
        if not abort_sleep(1):
            return

def connect_resilient(serial):
    while True:
        if stop_requested():
            return None
        if not wait_online(serial):
            return None
        d = connect(serial)
        if d is not None:
            try:
                d.window_size()
                return d
            except Exception:
                pass
        time.sleep(2)

def ensure_device(serial, d):
    wait_online(serial)
    if d is not None:
        try:
            d.window_size()
            return d
        except Exception:
            pass
    while True:
        if stop_requested():
            return d
        if device_state(serial) == "unauthorized":
            return d
        wait_online(serial)
        nd = connect(serial)
        if nd is not None:
            try:
                nd.window_size()
                return nd
            except Exception:
                nd = None
        time.sleep(2)

def safe_dump(dev):
    """UI hierarchy, retrying until it actually contains the Facebook window.

    uiautomator dumps the ACTIVE window, and on some phones it hands back the
    SystemUI window instead — a tree holding nothing but the status bar, nav bar
    and notifications. The old version returned that first dump happily, so
    every detector saw an empty screen: no menu button, no switcher, no account
    rows. That is why account switching reported "Could not open account
    switcher" on a phone whose switcher was perfectly fine.

    Only retried while Facebook really is in the foreground; if it genuinely is
    not showing, the dump is correct and returned as-is.
    """
    # A Snapshot is already a finished, validated dump - re-running the retry
    # logic on it would re-do the ~330 ms fb_foreground() check for nothing.
    if getattr(dev, "_frozen", False):
        return dev.dump_hierarchy()
    serial = getattr(dev, "serial", None)
    last = ""
    got_junk = False
    for attempt in range(14):
        try:
            xml = dev.dump_hierarchy()
        except Exception:
            if serial:
                wait_online(serial)
            else:
                time.sleep(1.0)
            continue
        if not xml:
            time.sleep(0.4)
            continue
        last = xml
        if FB_PACKAGE in xml:
            if got_junk:
                print("    [dump] recovered the Facebook window")
            return xml                     # got the Facebook window
        if not serial or not fb_foreground(serial):
            return xml                     # can't verify, or FB really isn't up
        if attempt == 0:
            got_junk = True
        # Waiting alone was not enough. uiautomator dumps the ACTIVE window and
        # SystemUI can hold it - these phones announce every notification icon,
        # so the tree comes back as status bar and nothing else. Six passive
        # retries (~3s) regularly expired, safe_dump returned that junk, and
        # every detector then read an EMPTY SCREEN: a post that was plainly on
        # screen came back as "loading", on_post_view said no, the open was
        # declared failed and the link was skipped entirely. Collapsing the
        # panel hands the active window back to the app - measured working on
        # a phone that had been stuck reading the status bar.
        # Nudge on the FIRST junk dump, not after several passive waits: a
        # recovery that takes 10s is nearly useless because on_post_view() only
        # gets 10-14s in total, so one slow dump would consume the whole budget
        # and the open would be declared failed with the post plainly on screen.
        if serial and attempt in (0, 2, 5, 9):
            try:
                run(["shell", "service", "call", "statusbar", "2"], serial=serial)
            except Exception:
                pass
        time.sleep(0.5)
    if got_junk:
        print("    [dump] STILL the system window after 14 tries - detectors "
              "will see an empty screen")
    return last

# Screen resolution cannot change during a run, but window_size() is a ~400 ms
# ADB round trip and core.py asks for it from 25 places - several of them
# inside the scroll loop. Measured on real hardware, so it is cached per
# device for the life of the process.
_wsize_cache = {}
_wsize_lock = threading.Lock()


def safe_window_size(dev):
    serial = getattr(dev, "serial", None)
    if serial:
        with _wsize_lock:
            hit = _wsize_cache.get(serial)
        if hit:
            return hit
    for _ in range(6):
        try:
            sz = dev.window_size()
            if sz and serial:
                with _wsize_lock:
                    _wsize_cache[serial] = sz
            return sz
        except Exception:
            if serial:
                wait_online(serial)
            else:
                time.sleep(2)
    return dev.window_size()


def forget_window_size(serial=None):
    """Drop cached resolution (device reconnected, or rotated)."""
    with _wsize_lock:
        if serial:
            _wsize_cache.pop(serial, None)
        else:
            _wsize_cache.clear()

def poll_until(check, timeout, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        try:
            if check():
                return True
        except Exception:
            pass
        if not abort_sleep(interval):
            return False
    return False

def fb_foreground(serial):
    _, out, _ = run(["shell", "dumpsys", "window", "windows"], serial=serial)
    return FB_PACKAGE in out

def post_ready(d):
    """Is there a single item on screen that a reaction could be aimed at?"""
    try:
        return detect_surface(d) in ACTIONABLE_SURFACES
    except Exception:
        return False

# ── Target-post lock ───────────────────────────────────────────────────────
# post_ready() alone is NOT enough to act: the news feed also has post action
# rows, so if a deep link fails and Facebook lands on the feed, the old code
# would happily react to whatever post was under the cursor. These markers only
# ever render on a single-post (permalink) view — the feed has no comment
# composer and no comments header — so they pin us to the post we were told to
# act on.
POST_VIEW_MARKERS = (
    # English
    "write a comment", "write a public comment", "comment as",
    "add a comment", "most relevant", "all comments", "top comments",
    "view more comments", "view previous comments", "most recent",
    "be the first to comment", "no comments yet",
    # Tagalog / Filipino locales
    "magsulat ng komento", "sumulat ng komento", "magkomento",
    "lahat ng komento", "pinakabago", "komento",
    # Taglish: a phone set to Filipino still gets the ENGLISH noun from
    # Facebook Lite - the composer reads "Sumulat ng comment..." there, which
    # none of the lines above match. Those phones then failed on_post_view()
    # while the post was plainly on screen, so open_target_post() burned all
    # its attempts and the caller retried the open forever.
    "magsulat ng comment", "sumulat ng comment", "sumulat ng puna",
    "lahat ng comment", "walang komento", "walang comment",
)

# Same idea as POST_VIEW_MARKERS, but tolerant of any verb/noun language mix
# ("Sumulat ng comment", "Magsulat ng komento", "Write a comment", ...) so a
# new locale string does not silently switch the post lock off again.
_POST_VIEW_RE = re.compile(
    r"(?:mag)?sulat\s+ng\s+(?:\w+\s+)?(?:comment|komento|puna)"
    r"|(?:write|add|leave|post)\s+(?:a|an)\s+(?:public\s+)?(?:comment|komento)"
    r"|(?:lahat|wala)\s+ng?\s+(?:comments?|komento)"
    r"|comments?\s+as",
    re.I)


def post_view_text_match(text):
    """True when on-screen text carries a single-post (permalink) landmark."""
    low = (text or "").lower()
    if any(m in low for m in POST_VIEW_MARKERS):
        return True
    return bool(_POST_VIEW_RE.search(low))

def on_post_view(d, timeout=0.0):
    """True only when a single post (or reel) is on screen — never the feed.

    Waits up to `timeout` seconds for the view to settle. Returns False the
    moment Abort is pressed.

    Which single item it is - post, reel, photo or video - is detect_surface()'s
    answer, and it is read off the screen. It used to be read off the URL and
    the words on screen, which is why a reel opened through a /share/p/ link
    was refused on every account (no words, wrong URL) and why a photo link was
    refused always (the photo viewer renders no text at all).
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        if stop_requested():
            return False
        try:
            if detect_surface(d) in ACTIONABLE_SURFACES:
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        if not abort_sleep(0.5):
            return False

# ── Share / redirect link resolution (ported from FBVenom) ─────────────────
# Confirmed on a real phone: a /share/p|r|v/<token> link carries NO post id, so
# FB Lite resolves it itself and lands on the NEWS FEED. Resolving it here on
# the PC turns it into the real permalink, which the alias activities in
# _post_view_attempts() open directly onto the post. Cached, never raises, and
# only accepts a result that still looks like a post (a login/checkpoint page
# can never make things worse - the original link is kept).
_url_resolve_cache = {}
_url_resolve_lock = threading.Lock()
_SHARE_UA = "Mozilla/5.0 (Linux; Android 10)"
_SHARE_HTML_LIMIT = 300000
_SHARE_TOKEN_RE = re.compile(r"/share/[a-z]/", re.I)
_CANONICAL_HREF_RE = re.compile(
    r"<link\b(?=[^>]*\brel=[\"\']canonical[\"\'])[^>]*\bhref=[\"\']([^\"\']+)[\"\']",
    re.I)
_OG_URL_RE = re.compile(
    r"<meta\b(?=[^>]*\bproperty=[\"\']og:url[\"\'])[^>]*\bcontent=[\"\']([^\"\']+)[\"\']",
    re.I)
_POST_ISH_RE = re.compile(
    r"/permalink(?:\.php)?(?:/|\?)|/story\.php\?|story_fbid=|"
    r"[?&]fbid=(?:\d+|pfbid)|/posts/(?:[^/?#]+/)?(?:\d+|pfbid)|"
    r"/groups/.+/(?:posts|permalink)/|"
    r"/reel[s]?/|[?&]v=\d+|/watch/|/video\.php|/videos?/|/photo|"
    r"/stories/|pfbid[0-9A-Za-z]+", re.I)


def _canonical_from_html(body):
    for pattern in (_CANONICAL_HREF_RE, _OG_URL_RE):
        m = pattern.search(body or "")
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def _read_until_canonical(resp, limit=_SHARE_HTML_LIMIT, chunk=32768):
    parts, total = [], 0
    while total < limit:
        block = resp.read(chunk)
        if not block:
            break
        parts.append(block)
        total += len(block)
        text = b"".join(parts).decode("utf-8", "replace")
        if _canonical_from_html(text):
            return text
    return b"".join(parts).decode("utf-8", "replace")


def _accept_resolved_url(candidate):
    if not candidate:
        return ""
    low = candidate.lower()
    if "login" in low or "checkpoint" in low or _SHARE_TOKEN_RE.search(low):
        return ""
    try:
        host = urllib.parse.urlsplit(candidate).hostname or ""
    except Exception:
        return ""
    if not _facebook_host(host):
        return ""
    if not _POST_ISH_RE.search(candidate):
        return ""
    return normalize_url(candidate) or candidate


def _canonicalize_fb_url(url):
    try:
        low = url.lower()
    except Exception:
        return url
    if not re.search(r"/share/[a-z]/|(?:fb\.watch|fb\.me)/", low):
        return url
    with _url_resolve_lock:
        if url in _url_resolve_cache:
            return _url_resolve_cache[url]
    resolved = url
    methods = ("GET",) if _SHARE_TOKEN_RE.search(low) else ("HEAD", "GET")
    for method in methods:
        body = ""
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": _SHARE_UA,
                         "Accept-Language": "en-US,en;q=0.9"})
            with NOPROXY.open(req, timeout=10) as resp:
                final = resp.geturl() or ""
                if method == "GET":
                    body = _read_until_canonical(resp)
        except Exception:
            continue
        good = (_accept_resolved_url(final) or
                _accept_resolved_url(_canonical_from_html(body)))
        if good:
            resolved = good
            break
    with _url_resolve_lock:
        _url_resolve_cache[url] = resolved
    if resolved != url:
        print(f"  [link] resolved share/redirect -> {resolved[:80]}")
    return resolved


def normalize_phone_url(raw):
    """One stable target shared by every phone: cleaned, then share/redirect
    tokens resolved to the real permalink once on the PC before devices fan
    out. Failure is safe - the cleaned original remains usable."""
    url = normalize_url(raw)
    if not url or url.startswith(("facebook://", "fb://")):
        return url
    resolved = _canonicalize_fb_url(url)
    return normalize_url(resolved) or url


def _fb_uri_variants(https_url):
    """Deep-link forms to try. The https permalink (resolved from any /share/
    token) leads, opened via the alias activities; scheme links follow as
    verified fallbacks."""
    variants = []
    if https_url.startswith(("facebook://", "fb://")):
        return [https_url]
    # Resolve /share/ and fb.watch tokens to the real permalink FIRST - the raw
    # share link has no post id and the alias lands it on the feed (measured).
    https_url = normalize_phone_url(https_url)
    if not https_url:
        return []
    reel = re.search(r"/reel[s]?/(\d+)", https_url, re.I)
    if reel:
        return [https_url, f"fb://reel/{reel.group(1)}"]
    post_id = None
    for pattern in (
        r"/permalink/(\d+)", r"[?&]story_fbid=(\d+)", r"[?&]fbid=(\d+)",
        r"/posts/(?:[^/?#]+/)?(\d+)",
        r"/groups/[^/]+/(?:posts|permalink)/(\d+)",
        r"[?&]v=(\d+)", r"/videos?/(?:[^/?]+/)*(\d+)",
        r"/photo[s]?/(?:[^/?]+/)*(\d+)", r"/photo\?fbid=(\d+)",
    ):
        m = re.search(pattern, https_url)
        if m:
            post_id = m.group(1)
            break
    variants.append(https_url)
    if post_id:
        variants.append(f"fb://post/{post_id}")
        variants.append(f"facebook://post?id={post_id}")
    else:
        # pfbid / share posts have no numeric id; FB Lite still routes the
        # opaque token through fb://post.
        m = re.search(r"\b(pfbid[0-9A-Za-z]+)\b", https_url)
        if m:
            variants.append(f"fb://post/{m.group(1)}")
    return list(dict.fromkeys(variants))

# Facebook Lite routes web links through dedicated deep-link ALIAS activities,
# not MainActivity. Handed a post URL, MainActivity ignores it and opens the
# NEWS FEED - which is what made the react loop scroll the feed hunting for a
# Like row. The alias activities land the post itself (a reel URL lands in the
# reel player, action rail and all). Builds without these aliases make
# `am start` fail with "does not exist", which open_post() already treats as
# "try the next attempt", so MainActivity stays as a fallback below.
FB_LINK_ALIASES = (
    "com.facebook.lite.deeplinking.activities.PermalinkPossiblePatternsActivityAlias",
    "com.facebook.deeplinking.aliasactivity.CatchAllUriAliasActivity",
)


def _post_view_attempts(uri):
    is_fb_scheme = uri.startswith(("fb://", "facebook://"))
    if is_fb_scheme:
        return [
            ["shell", "am", "start", "-a", "android.intent.action.VIEW",
             "-d", uri, "-p", FB_PACKAGE, "-f", "0x14000000"],
            ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri],
        ]
    attempts = [
        ["shell", "am", "start", "-n", f"{FB_PACKAGE}/{alias}",
         "-a", "android.intent.action.VIEW", "-d", uri, "-f", "0x14000000"]
        for alias in FB_LINK_ALIASES
    ]
    attempts.append(
        ["shell", "am", "start", "-n", FB_ACTIVITY,
         "-a", "android.intent.action.VIEW", "-d", uri, "-f", "0x14000000"])
    attempts.append(
        ["shell", "am", "start", "--activity-single-top",
         "-a", "android.intent.action.VIEW",
         "-d", uri, "-p", FB_PACKAGE, "-f", "0x14000000"])
    return attempts

def open_post(serial, url, allow_warm=True):
    # Never (re)launch Facebook once Abort has been pressed.
    if stop_requested():
        return False
    url = normalize_url(url)
    if not url:
        print(f"  [!] {serial} invalid or empty post URL.")
        return False
    wait_online(serial)
    variants = _fb_uri_variants(url)

    if allow_warm and fb_foreground(serial):
        for uri in variants:
            for args in _post_view_attempts(uri):
                rc, out, err = run(args, serial=serial)
                low = (out + "\n" + err).strip().lower()
                if rc != 0 or any(k in low for k in
                                  ("error", "exception", "unable to resolve", "not found")):
                    continue
                if poll_until(lambda: fb_foreground(serial), 5):
                    print(f"  [{serial}] post opened warm ({uri[:60]})")
                    return True
                break
        print(f"  [{serial}] warm open didn't stick, cold-starting...")

    run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
    poll_until(lambda: not fb_foreground(serial), 2.0)
    run(["shell", "am", "broadcast",
         "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS"], serial=serial)
    for uri in variants:
        for args in _post_view_attempts(uri):
            rc, out, err = run(args, serial=serial)
            msg = (out + "\n" + err).strip()
            low = msg.lower()
            if rc != 0 or any(k in low for k in
                               ("error", "exception", "unable to resolve", "not found")):
                time.sleep(0.4)
                continue
            if poll_until(lambda: fb_foreground(serial), 6):
                print(f"  [{serial}] post opened in FB Lite ({uri[:70]})")
                return True
            print(f"  [{serial}] FB Lite not in foreground after '{uri[:50]}', trying next...")
            run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
            poll_until(lambda: not fb_foreground(serial), 1.5)
            break
    print(f"  [!] {serial} all attempts failed: {url[:100]}")
    return False

def open_target_post(serial, d, url, attempts=3, fresh=False):
    """Open the post and CONFIRM the post itself is on screen.

    fresh=True skips the warm open and cold-starts the app. A warm open of a
    post Facebook Lite already has open brings it back at the SAME scroll
    position - measured on 1487170579000573: deep in the comments, with the
    Like row above the screen. The run only ever scrolls DOWN (dragging down
    near the top is pull-to-refresh), so it could never get back to the row,
    and every warm retry landed in the same spot: no-action-row three times,
    next account. A cold start always opens the post at the top.

    open_post() only proves Facebook is in the foreground — which is equally
    true when a share link quietly drops us on the news feed. That is how the
    run ends up scrolling the feed instead of doing its job. Here we verify the
    actual post view and cold-start the app when a warm intent failed to
    navigate, so we land on the link and get straight to the task.
    """
    for i in range(attempts):
        if stop_requested():
            return False
        warm = (i == 0) and not fresh
        try:
            opened = open_post(serial, url, allow_warm=warm)
        except Exception:
            opened = False
        if stop_requested():
            return False
        if opened:
            try:
                if dismiss_chooser(serial, d):
                    print(f"    [{serial}] dismissed 'Open with' chooser -> Facebook Lite")
            except Exception:
                pass
            if on_post_view(d, timeout=10.0 if warm else 14.0):
                print(f"  [{serial}] target post is on screen.")
                return True
        if stop_requested():
            return False
        # A phone bumped to the login chooser will never reach the post no
        # matter how often we reopen it, so recover onto a live account first.
        try:
            if login_screen_account_rows(d):
                recover_from_login(serial, d)
        except Exception:
            pass
        print(f"  [{serial}] not on the target post - cold reopening "
              f"({i + 1}/{attempts})")
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
        if not abort_sleep(1.0):
            return False
    print(f"  [!] {serial} could not reach the target post: {url[:90]}")
    return False

def chooser_present(serial, d):
    _, out, _ = run(["shell", "dumpsys", "window"], serial=serial)
    for line in out.splitlines():
        if "mCurrentFocus" in line and (
                "resolver" in line.lower() or "resolveractivity" in line.lower()):
            return True
    for sel in (dict(resourceId="android:id/button_always"),
                dict(resourceId="com.transsion.resolver:id/button_once"),
                dict(text="Open with"), dict(textContains="Open with")):
        if d(**sel).exists:
            return True
    return False

def dismiss_chooser(serial, d):
    handled = False
    for _ in range(3):
        if not chooser_present(serial, d):
            break
        handled = True
        for sel in (dict(text="Lite"), dict(textContains="Lite"),
                    dict(textContains="Facebook")):
            el = d(**sel)
            if el.exists:
                try:
                    el.click()
                    time.sleep(0.5)
                except Exception:
                    pass
                break
        clicked = False
        for sel in (dict(resourceId="android:id/button_always"),
                    dict(text="Always"),
                    dict(resourceId="com.transsion.resolver:id/button_once"),
                    dict(text="Just once")):
            el = d(**sel)
            if el.exists:
                try:
                    el.click()
                    time.sleep(2)
                    clicked = True
                    break
                except Exception:
                    pass
        if not clicked:
            try:
                d.press("enter")
            except Exception:
                pass
            time.sleep(1)
    return handled

def is_logged_in(d):
    xml = safe_dump(d)
    if not xml:
        return True
    markers = ("Mobile number or email", "Create new account", "Forgot password?")
    if "Log in" in xml and ("Password" in xml or any(m in xml for m in markers)):
        return False
    return True

def ask_reaction(prompt):
    while True:
        r = input(prompt).strip().lower()
        if r in REACTIONS:
            return r
        print(f"  choose one of: {', '.join(sorted(REACTIONS))}")

def parse_bounds(value):
    m = BOUNDS_RE.match(value or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2

def center(bounds):
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2

class Snapshot:
    """One frozen read of the screen, reusable by every detector.

    The detectors (_find_post_like, on_post_view, find_reel_like_point) only
    ever read the hierarchy and the screen size, so they can all run against a
    single dump instead of each pulling their own ~140 ms one. Take a fresh
    Snapshot whenever the screen may have moved - never reuse one across a
    swipe or a tap.
    """
    __slots__ = ("serial", "_xml", "_size")
    _frozen = True          # tells safe_dump() not to re-validate this

    def __init__(self, dev):
        self.serial = getattr(dev, "serial", None)
        self._xml = safe_dump(dev)
        self._size = safe_window_size(dev)

    def dump_hierarchy(self, *a, **k):
        return self._xml

    def window_size(self):
        return self._size

    def __bool__(self):
        return bool(self._xml)


def hierarchy_root(d):
    xml = safe_dump(d)
    if not xml:
        return None
    try:
        return ET.fromstring(xml)
    except Exception:
        return None

def clickable_items(d):
    root = hierarchy_root(d)
    if root is None:
        return []
    items = []
    for node in root.iter("node"):
        if node.attrib.get("clickable") != "true":
            continue
        b = parse_bounds(node.attrib.get("bounds"))
        if b:
            items.append((b, node.attrib))
    return items

def clickable_bounds(d):
    return [b for b, _ in clickable_items(d)]

def visible_text(d):
    xml = safe_dump(d)
    if not xml:
        return ""
    vals = []
    for attr in ("text", "content-desc"):
        vals.extend(html.unescape(m.group(1))
                    for m in re.finditer(attr + r'="([^"]*)"', xml))
    return "\n".join(v for v in vals if v)

def _find_reaction_by_label(d, reaction):
    labels = REACTION_LABELS.get(reaction, [reaction])
    root   = hierarchy_root(d)
    if root is None:
        return None
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or
                node.attrib.get("text") or "").lower().strip()
        for lbl in labels:
            if lbl in desc:
                b = parse_bounds(node.attrib.get("bounds"))
                if b:
                    return center(b)
    return None

def _wait_for_reaction_tray(d, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_requested():
            return False
        for reaction in REACTION_ORDER:
            if _find_reaction_by_label(d, reaction) is not None:
                return True
        root = hierarchy_root(d)
        if root is not None:
            imgs = []
            for node in root.iter("node"):
                if node.attrib.get("class") != "android.widget.ImageView":
                    continue
                b = node.attrib.get("bounds", "")
                try:
                    parts = b.replace("][", ",").replace("[","").replace("]","").split(",")
                    if len(parts) != 4: continue
                    x1, y1, x2, y2 = map(int, parts)
                    w, h = x2 - x1, y2 - y1
                    # Measured on real screens: the POST tray draws its emojis
                    # at 104x104, the REEL tray at 74x74. The old 85-130 window
                    # fit only the post, so this gate was blind to an open reel
                    # tray. The >=4 images spread across >35% of the width below
                    # is what actually identifies a tray; the size window only
                    # has to keep out icons, and the reel rail's own glyphs are
                    # 64px Views, not ImageViews.
                    if 70 <= w <= 140 and 70 <= h <= 140:
                        imgs.append(((x1 + x2) // 2, (y1 + y2) // 2))
                except Exception:
                    pass
            if len(imgs) >= 4:
                xs = sorted(x for x, _ in imgs)
                if xs[-1] - xs[0] > int(safe_window_size(d)[0] * 0.35):
                    return True
        time.sleep(0.25)
    return False


def find_reaction_tray_points(d, like_point=None):
    width, height = safe_window_size(d)
    top_xf = (0.297, 0.500, 0.703)
    bot_xf = (0.196, 0.399, 0.601, 0.804)

    root = hierarchy_root(d)
    if root is not None:
        icons = []
        for node in root.iter("node"):
            if node.attrib.get("class") != "android.widget.ImageView":
                continue
            b = parse_bounds(node.attrib.get("bounds"))
            if not b:
                continue
            bw, bh = b[2]-b[0], b[3]-b[1]
            if 85 <= bw <= 130 and 85 <= bh <= 130:
                cx, cy = center(b)
                if like_point is None or cy < like_point[1] + 10:
                    icons.append((cx, cy))
        if icons:
            bands = {}
            for cx, cy in icons:
                bands.setdefault(round(cy / 35), []).append((cx, cy))
            rows = [sorted(v) for v in bands.values() if len(v) >= 3]
            rows.sort(key=lambda r: r[0][1])
            if len(rows) >= 2 and len(rows[0]) == 3 and len(rows[1]) == 4:
                return rows[0] + rows[1]
            four = next((r for r in rows if len(r) == 4), None)
            if four:
                by = sum(p[1] for p in four) // 4
                gap = int(height * 0.14)
                top = [((four[i][0] + four[i+1][0]) // 2, by - gap) for i in range(3)]
                return top + four

    if like_point is not None:
        ly = like_point[1]
        top_y = ly - int(height * 0.256)
        bot_y = ly - int(height * 0.116)
        if top_y < int(height * 0.06):
            top_y = ly + int(height * 0.116)
            bot_y = ly + int(height * 0.256)
        return ([(int(width * f), top_y) for f in top_xf] +
                [(int(width * f), bot_y) for f in bot_xf])

    base_y, row2_y = int(height * 0.48), int(height * 0.60)
    return ([(int(width * f), base_y) for f in top_xf] +
            [(int(width * f), row2_y) for f in bot_xf])

# Facebook throttles accounts that react/comment in bulk. The block screen is
# EXACTLY what a second run walks into — run 1 is what earns it — and the tool
# used to be blind to it, so it just retried and scrolled forever.
ACTION_BLOCK_MARKERS = (
    "temporarily blocked", "you're temporarily blocked", "youre temporarily blocked",
    "misusing this feature", "you can't use this feature", "cant use this feature",
    "action blocked", "we limit how often", "try again later",
    "you're going too fast", "slow down", "this feature is unavailable",
    # Tagalog / Filipino builds
    "pansamantalang naka-block", "hindi mo magamit", "subukan muli mamaya",
)


def is_action_blocked(d):
    """True when Facebook is showing a rate-limit / action-block screen."""
    try:
        text = visible_text(d).lower()
    except Exception:
        return False
    return any(m in text for m in ACTION_BLOCK_MARKERS)


# ── Pacing ────────────────────────────────────────────────────────────────
# Acting as fast as ADB allows is what earns the action block in the first
# place: a human cannot open a post, react and comment in under a second, and
# certainly not on twenty accounts back to back. These randomised pauses are
# the actual fix — the block detection below is only the safety net.
PACE_ACTION = (1.1, 2.8)     # after a reaction / a comment
PACE_LINK   = (2.5, 6.0)     # between two links on the same account
PACE_ACCOUNT = (6.0, 14.0)   # after finishing an account, before switching


# Speed profiles. Pacing is the ONLY thing standing between a run and
# Facebook's action block, so this is a real trade, not free speed:
#   safe   - human-ish timing; lowest block risk
#   fast   - about half the waiting; fine for aged accounts / short runs
#   turbo  - minimal waiting; expect action blocks on large fleets
SPEED_PROFILES = {
    "safe":  {"action": (1.1, 2.8), "link": (2.5, 6.0), "account": (6.0, 14.0)},
    "fast":  {"action": (0.6, 1.4), "link": (1.2, 3.0), "account": (3.0, 7.0)},
    "turbo": {"action": (0.25, 0.6), "link": (0.5, 1.2), "account": (1.5, 3.0)},
}
SPEED = "safe"


def set_speed(profile):
    """Switch pacing profile. Returns the name actually applied."""
    global PACE_ACTION, PACE_LINK, PACE_ACCOUNT, SPEED
    p = SPEED_PROFILES.get(profile)
    if not p:
        return SPEED
    PACE_ACTION, PACE_LINK, PACE_ACCOUNT = p["action"], p["link"], p["account"]
    SPEED = profile
    print(f"  [pace] speed profile: {profile}")
    return SPEED


def pace(span, why=""):
    """Randomised, abort-aware pause. False when Abort was pressed."""
    lo, hi = span
    secs = random.uniform(lo, hi)
    if why:
        print(f"    [pace] waiting {secs:.1f}s {why}")
    return abort_sleep(secs)


DEBUG_DIR = os.path.join(HERE, "debug")


def dump_debug(serial, d, tag):
    """Save a screenshot + UI dump when something repeatedly fails, so the
    actual screen can be inspected afterwards instead of guessed at."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*]', '_', str(serial)).strip() or "device"
        # The tag needs the same scrub as the serial. Tags are result strings
        # like "react-failed:no-action-row", and on Windows that colon turns
        # "name:no-action-row.png" into an NTFS alternate data stream - the
        # dump went into a hidden stream and debug/ filled with 0-byte files,
        # so every screen this was supposed to preserve was lost.
        tag_safe = re.sub(r'[<>:"/\\|?*]', '_', str(tag)).strip() or "debug"
        base = os.path.join(DEBUG_DIR,
                            f"{safe}_{tag_safe}_{time.strftime('%Y%m%d_%H%M%S')}")
        try:
            r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                               capture_output=True, timeout=15)
            if r.returncode == 0 and r.stdout:
                with open(base + ".png", "wb") as fh:
                    fh.write(r.stdout)
        except Exception:
            pass
        try:
            xml = safe_dump(d)
            if xml:
                with open(base + ".xml", "w", encoding="utf-8") as fh:
                    fh.write(xml)
        except Exception:
            pass
        print(f"    [debug] screen saved -> {base}.png / .xml")
        return base
    except Exception:
        return None


def _has_post_action_row(d):
    """True when a normal post's Like/Comment/Share row is on screen.

    Geometry only: the real buttons expose neither content-desc nor text, so a
    label lookup never matches and would just cost another full UI dump.
    """
    try:
        return _find_post_like(d) is not None
    except Exception:
        return False


def _reel_action_stack(d, trust_reel=None):
    # A post's comment list puts a "..." button at the right edge of every
    # comment; three of those stacked are indistinguishable from a reel rail.
    # If a real post action row is present, this is not a reel.
    #
    # ...unless the link we were told to open IS a reel. A reel's own bottom
    # overlay - page avatar, page name, Follow - is three clickables on one
    # line starting at the left edge, which _find_post_like() reads as an
    # action row whenever the avatar is wide enough (96px on a Page reel, but
    # only 56px on a person's reel, which is why some reels worked and others
    # did not). That veto then killed the rail, so find_reel_like_point()
    # returned None, on_post_view() said "not a post", and every phone refused
    # the reel with react-failed:not-on-target-post. The URL is the stronger
    # evidence, so when it says reel, the lookalike does not get a vote.
    #
    # The URL is no longer the deciding evidence, because the same /share/p/
    # link is a post for one id and a reel for the next. Two screen facts
    # replace it: a post permalink always keeps its comment composer pinned at
    # the bottom (a reel never has one), and the rail's icons are uniform in
    # size AND evenly spaced, which a column of comment "..." buttons is not -
    # those sit at whatever interval the comments above them happen to end.
    if trust_reel is None:
        trust_reel = _is_reel_context() or not _composer_in_view(d)
    if not trust_reel and _has_post_action_row(d):
        return None
    width, height = safe_window_size(d)
    col = []
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2 - x1
        h = y2 - y1
        cx = (x1 + x2) // 2
        if cx < int(width * 0.82):
            continue
        if w < 35 or w > 140 or h < 40 or h > 175:
            continue
        if y1 < int(height * 0.42):
            continue
        col.append(b)
    if len(col) < 2:
        return None
    col.sort(key=lambda b: b[1])
    stack = [col[0]]
    for b in col[1:]:
        if b[1] - stack[-1][1] >= 60:
            stack.append(b)
    if len(stack) < 3:
        return None
    if stack[-1][1] - stack[0][1] < 120:
        return None
    if center(stack[0])[1] > int(height * 0.88):
        return None
    if not _evenly_stacked(stack):
        return None                     # a comment "..." column, not a rail
    return [center(b) for b in stack]


def _evenly_stacked(stack):
    """Rail icons are the same size and evenly spaced; comment menus are not.

    Measured on this fleet: the rail is 108x128 / 108x128 / 108x128 / 108x96
    at 144 / 144 / 128 px apart. A column of comment overflow buttons repeats
    at whatever interval the comments above it happen to end, so its gaps
    scatter far wider than this.
    """
    widths = [b[2] - b[0] for b in stack]
    heights = [b[3] - b[1] for b in stack]
    if max(widths) > min(widths) * 1.6 or max(heights) > min(heights) * 1.9:
        return False
    # A rail is a single COLUMN: every icon shares a left edge (measured on
    # this fleet: x1 = 612, 612, 612, 612, exactly). The news feed's own
    # right-edge controls only look like one - x1 = 640, 620, 648 with widths
    # 80, 84, 62 - and the size-ratio test above waves that through (84/62 =
    # 1.35, inside 1.6), which is how the FEED was being detected as a reel and
    # a stranger's post got liked while the target went untouched. Their gaps
    # (80 and 110px) are too close to separate by spacing, but their ragged
    # left edge gives them away.
    lefts = [b[0] for b in stack]
    mid_w = sorted(widths)[len(widths) // 2]
    if max(lefts) - min(lefts) > mid_w * 0.25:
        return False
    gaps = [stack[i + 1][1] - stack[i][1] for i in range(len(stack) - 1)]
    if not gaps:
        return False
    mid = sorted(gaps)[len(gaps) // 2]
    if mid <= 0:
        return False
    return all(abs(g - mid) <= mid * 0.45 for g in gaps)


def _composer_node_in_view(d):
    """The comment box, found by WIDGET rather than by its words.

    Facebook Lite renders the composer as a MultiAutoCompleteTextView (an
    EditText subclass) pinned near the bottom of a post permalink. Nothing else
    the run visits has one: not the feed, not the Menu, not the reel player,
    not the photo viewer.

    This exists because every other "is this a post?" test in here reads TEXT,
    and these phones expose none — measured on a real wall-of-text post, the
    whole accessibility tree carried 231 characters and all of it was status-bar
    notifications. With no text and the action row pushed below the fold by the
    caption, the post was open on screen and still refused as "not on the target
    post". The widget is there whether or not its placeholder is readable.
    """
    try:
        _, height = safe_window_size(d)
        root = hierarchy_root(d)
        if root is None:
            return False
        for node in root.iter("node"):
            cls = node.attrib.get("class", "")
            if "EditText" not in cls and "AutoCompleteTextView" not in cls:
                continue
            b = parse_bounds(node.attrib.get("bounds"))
            if b and center(b)[1] >= int(height * 0.70):
                return True
        return False
    except Exception:
        return False


def _composer_in_view(d):
    """True when the comment composer / comments header is on screen.

    A post permalink keeps it pinned at the bottom; the reel player, the photo
    viewer and the video player have nothing of the sort.
    """
    try:
        if post_view_text_match(visible_text(d)):
            return True
    except Exception:
        pass
    return _composer_node_in_view(d)


def find_reel_like_point(d, trust_reel=None):
    stack = _reel_action_stack(d, trust_reel)
    return stack[0] if stack else None

def find_reel_tray_points(d):
    width, height = safe_window_size(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2 - x1
        h = y2 - y1
        if w < int(width * 0.08) or w > int(width * 0.20):
            continue
        if h < 55 or h > 145:
            continue
        rows.setdefault(y1 // 14, []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        if len(row) >= 7:
            cells = row[:7]
            if cells[0][0] < int(width * 0.12) and cells[-1][2] > int(width * 0.86):
                return [center(b) for b in cells]
    return None

def _is_reel_context():
    u = (current_post_url() or "")
    # /share/v/ belongs here too: measured on this fleet, 1BTeC89d87 and
    # 1CnjLfTvwn both open straight into the reel player, so a /v/ link is
    # every bit as swipe-hostile as an /r/ one.
    return bool(re.search(r"/share/r/|/share/v/|/reel[s]?/|/reel\b", u, re.I))

def _wait_reel_like(d, timeout=6.0):
    pt = find_reel_like_point(d)
    if pt is not None:
        return pt
    w, h = safe_window_size(d)
    deadline = time.time() + timeout
    revealed = False
    while time.time() < deadline:
        if not revealed:
            try:
                d.click(int(w * 0.5), int(h * 0.5))
            except Exception:
                pass
            revealed = True
            time.sleep(0.8)
        else:
            time.sleep(0.4)
        pt = find_reel_like_point(d)
        if pt is not None:
            return pt
    return None

def _find_post_like(d):
    """Like button = leftmost of the post's action row.

    Width limits measured from real Facebook Lite screens: this build renders
    the row as pills of ~0.14-0.20 of the screen width ([Like 453][Comment 33]
    [Share 128] plus the reaction icons), NOT the ~0.30 wide thirds the older
    build used. The previous 0.22 floor rejected every button on this layout,
    so the row was never found and the run just scrolled forever.

    Comment rows look similar but start indented (x ~0.25 of the width), so
    anchoring the row to the left edge is what separates them.
    """
    rows = _post_action_rows(d)
    return center(rows[0][0]) if rows else None


def _find_photo_like(d):
    """Like on the ITEM itself, not on the post that contains it.

    A photo permalink stacks two action rows: the parent post's near the top,
    the photo's own at the very bottom. _find_post_like() walks top-down and
    returns the first, which reacts to the parent post instead.

    Measured on the fleet: the parent's row sits at cy 598 and the photo's own
    at cy 1498 - past _post_action_rows()' 0.92 ceiling, which is there to keep
    Android's nav bar out - so the lower row has to come from the bottom-row
    detector rather than from the row list.
    """
    rows = _post_action_rows(d)
    lowest = center(rows[-1][0]) if rows else None
    bottom = _bottom_action_row(d)
    if bottom is not None and (lowest is None or bottom[1] > lowest[1]):
        return bottom
    return lowest


# ── Self-healing action-row discovery ───────────────────────────────────────
# The built-in shape rules describe the layouts we have MEASURED. Facebook
# renders posts in more shapes than that, and every time one turns up the run
# reports no-action-row (or worse, long-presses a lookalike such as a pair of
# hashtag links). Rather than hand-tune a new rule per post, the run finds the
# Like button by experiment: press a candidate, and if the reaction tray opens,
# that WAS the Like button. The shape is then written down per device+screen,
# so the same layout is recognised instantly next time - and shapes that turned
# out to be lookalikes are written down too, so they are never probed again.
ROWS_FILE = os.path.join(HERE, "learned_rows.json")
ROWS_MAX = 40                       # per device, per kind - keeps the file small
_rows_cache = None


def _rows_store():
    global _rows_cache
    if _rows_cache is None:
        try:
            with open(ROWS_FILE, encoding="utf-8") as fh:
                _rows_cache = json.load(fh)
            if not isinstance(_rows_cache, dict):
                _rows_cache = {}
        except Exception:
            _rows_cache = {}
    return _rows_cache


def _rows_save():
    try:
        tmp = ROWS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_rows_store(), fh, indent=2)
        os.replace(tmp, ROWS_FILE)
    except Exception:
        pass


def _rows_key(d):
    w, h = safe_window_size(d)
    return "%s|%dx%d" % (getattr(d, "serial", "?"), w, h)


def row_signature(row, width, height):
    """A screen-size-independent fingerprint of one candidate row.

    Cell count, each cell's width as a fraction of the screen, the tallest
    cell, and where the row starts and ends. That is enough to tell the real
    Like|Comment|Share row (3 cells at 0.33 spanning 0.00-1.00) from a pair of
    hashtag links (0.42 + 0.48, stopping at 0.93).
    """
    cells = sorted(row, key=lambda b: b[0])
    widths = ",".join("%.2f" % ((b[2] - b[0]) / float(width)) for b in cells)
    tall = max(b[3] - b[1] for b in cells) / float(height)
    return "n%d|w%s|h%.2f|x%.2f-%.2f" % (
        len(cells), widths, tall,
        cells[0][0] / float(width), cells[-1][2] / float(width))


def _learned_sigs(d, kind):
    return set(_rows_store().get(_rows_key(d), {}).get(kind, []))


def learn_row(d, row, worked):
    """Record that this shape either IS or is NOT an action row on this phone."""
    width, height = safe_window_size(d)
    sig = row_signature(row, width, height)
    entry = _rows_store().setdefault(_rows_key(d), {"good": [], "bad": []})
    keep, drop = ("good", "bad") if worked else ("bad", "good")
    if sig in entry.get(drop, []):
        entry[drop].remove(sig)
    if sig not in entry.setdefault(keep, []):
        entry[keep].append(sig)
        entry[keep] = entry[keep][-ROWS_MAX:]
    _rows_save()
    return sig


def _candidate_rows(d):
    """Every horizontal band that COULD be an action row - deliberately loose.

    No left-edge or equal-width requirement: this is the net cast when the
    strict rules have already failed, and the probe decides what is real.
    """
    width, height = safe_window_size(d)
    tm = TouchMap(d)
    groups = {}
    for b in tm.clicks:
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bh < 40 or bh > 140 or bw < int(width * 0.10):
            continue
        cy = (b[1] + b[3]) / 2.0
        if cy < height * 0.15 or cy > height * 0.92:
            continue
        groups.setdefault(int(round(cy / 26.0)), []).append(b)
    out = []
    for _, row in sorted(groups.items()):
        row = sorted(row, key=lambda b: b[0])
        if len(row) < 2:
            continue
        if row[-1][2] - row[0][0] < int(width * 0.55):
            continue
        # never probe a caption link: long-pressing one opens it. Dropped only
        # after the shape test, so this can remove a candidate, never add one.
        row = [b for b in row if b not in tm.links]
        if len(row) < 2 or row[-1][2] - row[0][0] < int(width * 0.55):
            continue
        out.append(row)
    return out


def _learned_rows_on_screen(d, width, height):
    """Rows matching a shape this phone has already PROVEN is an action row."""
    good = _learned_sigs(d, "good")
    if not good:
        return []
    return [row for row in _candidate_rows(d)
            if row_signature(row, width, height) in good
            and not _is_link_row(d, row, width, height)]


def discover_action_row(d, probes=6, steps=16):
    """Find the Like button by experiment, and remember what it looked like.

    Ground truth is the reaction tray: if a long press opens it, that control
    WAS the Like button. Anything else is dismissed with BACK and written down
    as a lookalike so it is never probed again.

    This reopens the post first. By the time it is called the run has already
    scrolled to the bottom hunting for a row it never found, so the row - if it
    is there at all - is now above the fold; probing only what happens to be on
    screen finds nothing but comments. So: back to the top, then step down and
    probe as we go, exactly the way the strict detector searches.

    The tray is closed before returning, so the normal reaction path takes over
    unchanged.
    """
    serial = getattr(d, "serial", None)
    # current_post_url() - the THREAD-LOCAL one every worker sets per link.
    # This used to read a module global CURRENT_POST_URL that does not exist
    # anywhere in this file: it only ever worked under test scripts that set
    # the attribute by hand, and in the real app it raised NameError, which
    # unwound all the way to gui.py and killed the whole phone's run.
    url = current_post_url()
    if serial and url:
        try:
            # fresh: a warm reopen comes back where the last scroll left it -
            # past the row - which is exactly what this is trying to undo
            open_target_post(serial, d, url, fresh=True)
        except Exception:
            pass
    width, height = safe_window_size(d)
    bad = _learned_sigs(d, "bad")
    tried, seen = 0, set()

    for step in range(steps):
        if stop_requested():
            return None
        for row in _candidate_rows(d):
            sig = row_signature(row, width, height)
            if sig in bad or sig in seen:
                continue
            seen.add(sig)
            if tried >= probes:
                return None
            tried += 1
            pt = center(row[0])
            print("    [react] no_react_rows - probing %s at %s" % (sig, pt))
            before = _screen_signature(d)
            try:
                d.long_click(pt[0], pt[1], 1.5)
            except Exception:
                continue
            if _wait_for_reaction_tray(d, timeout=4.0):
                learn_row(d, row, True)
                print("    [react] LEARNED action row %s - this layout will be "
                      "recognised straight away next time" % sig)
                try:
                    d.press("back")
                except Exception:
                    pass
                time.sleep(0.6)
                return pt
            learn_row(d, row, False)
            bad.add(sig)
            print("    [react] %s is not the action row" % sig)
            # Not an unconditional BACK: when the probe did nothing, BACK
            # closes the post and every later probe presses a stranger's screen.
            if undo_stray_press(d, before) == "lost":
                return None
            time.sleep(0.6)
        sx, sy1, _, sy2 = _swipe_path(TouchMap(d), SCROLL_FINE[0],
                                      SCROLL_FINE[1], step)
        try:
            if serial:
                run(["shell", "input", "swipe", str(sx), str(sy1), str(sx),
                     str(sy2), str(_drag_ms(sy1, sy2))], serial=serial)
            else:
                d.swipe(sx, sy1, sx, sy2, _drag_ms(sy1, sy2) / 1000.0)
        except Exception:
            return None
        if not abort_sleep(1.0):
            return None
    return None


def _post_action_rows(d):
    """Every action row on screen, top first, each as its cells left to right."""
    width, height = safe_window_size(d)
    tm = TouchMap(d)
    rows = {}
    found = []
    for b in tm.clicks:
        x1, y1, x2, y2 = b
        bw, bh = x2 - x1, y2 - y1
        if bh < 40 or bh > 140:
            continue
        if bw < int(width * 0.12) or bw > int(width * 0.55):
            continue
        rows.setdefault(round((y1 + y2) / 2 / 26), []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        # Two shapes of action row exist on this build:
        #   3+ cells of ~0.14-0.33w - Like | Comment | Share, the common case
        #   2  cells of ~0.50w each - Like | Share, used when the post shows
        #                             its comments inline and so draws no
        #                             Comment button. Measured on Ralph
        #                             Recto's post: (0,481,360,561) and
        #                             (360,481,720,561), both exactly half the
        #                             width. The old >=3 cell rule plus a
        #                             0.42w ceiling rejected this row twice
        #                             over, so the run scrolled past it into
        #                             the comments every time and then pressed
        #                             a remembered point that landed on one.
        if len(row) >= 3:
            row = [b for b in row if (b[2] - b[0]) <= int(width * 0.42)]
            if len(row) < 3:
                continue
        elif len(row) == 2:
            # Two wide cells are an action row only when they TILE the screen -
            # Like | Share at exactly half each, 0..720 with no gap.
            # Two side-by-side hashtag links look almost the same and are not:
            # measured on Board Member Balba's post, #2in1BokalNgMasa (300px,
            # 0.42w) and #BoardMemberBALBA (344px, 0.48w) matched, so the run
            # long-pressed a HASHTAG, found no tray twice, then blind-tapped it
            # and reported reacted:like while the real row sat six scrolls
            # further down. Equal halves and full-width coverage separate them.
            ws = [b[2] - b[0] for b in row]
            if not all(x >= int(width * 0.40) for x in ws):
                continue
            if max(ws) > min(ws) * 1.12:        # halves match; text links do not
                continue
            if row[0][0] > int(width * 0.03) or row[-1][2] < int(width * 0.97):
                continue                         # must span the whole width
        else:
            continue
        if row[0][0] > int(width * 0.12):               # must hug the left edge
            continue                                     # (comment rows indent)
        if row[-1][2] < int(width * 0.55):              # and reach across
            continue
        # Two lookalikes live outside the post body and must be excluded by
        # position, not by spacing (the older build tiles its action row flush,
        # exactly like the feed's tab bar):
        #   * the feed's tab bar   - pinned ~14% down from the top
        #   * Android's nav bar    - Overview/Home/Back, three 0.19-wide
        #                            buttons at ~98% down, present on EVERY
        #                            screen, which is what made the news feed
        #                            look like a reactable post
        cy = center(row[0])[1]
        if cy < int(height * 0.20) or cy > int(height * 0.92):
            continue
        # A link, hashtag or tagged name inside the caption - or a comment's
        # own Like / Reply - is never the post's Like button. A caption line
        # holding a few of them has exactly the shape of an action row, and
        # long-pressing a link does not open the tray: the release OPENS THE
        # LINK, which is how the run left link posts without reacting.
        if any(b in tm.links for b in row):
            continue
        if _is_link_row(d, row, width, height):
            continue                    # pressing it opened a link earlier
        found.append(row)
    if not found:
        # Nothing matched a built-in shape - fall back to layouts this phone
        # has already proven by experiment. This is what stops a post that
        # once needed probing from ever needing it again.
        found = _learned_rows_on_screen(d, width, height)
    return found

# ── Long-caption handling ────────────────────────────────────────────────────
# A fixed 12-scroll budget was the reason very long captions stopped reacting:
# each step moves ~0.26 of a screen, so 12 steps only ever covered ~3 screens.
# A caption longer than that ran out of budget and the run reported "could not
# find the action row" - which looked like "it just scrolls and never reacts".
SCROLL_BUDGET_MAX = 40      # generous ceiling; stall detection ends it earlier
SCROLL_STALL_LIMIT = 8      # identical screens in a row = bottom reached
# 8, not 2: a screen filled with caption text exposes no clickable nodes, so
# its fingerprint repeats while the page is still scrolling perfectly well.
# Two was low enough to abort real long-caption posts; eight still stops a
# genuinely stuck post in a few swipes instead of burning the whole budget.

# Step sizes as a fraction of screen height.
#   fine   - the original, proven step, used once the comment area is in view
#   coarse - used while still far away, to cross a long caption quickly
# Coarse must stay well under a full screen: the action row shifts by exactly
# this much per swipe, so a jump smaller than the screen can never carry the
# row past the viewport unseen.
SCROLL_FINE = (0.72, 0.46)      # 0.26 screen
SCROLL_COARSE = (0.85, 0.34)    # 0.51 screen


def on_feed_view(d):
    """True when the NEWS FEED is on screen, by shape.

    This exists because the scroll guard had the test backwards. It bailed out
    whenever it could not SEE a comment marker ("write a comment", "all
    comments", ...) - but those live at the very bottom of a post, so a long
    caption hides them for many screens. A big post therefore looked exactly
    like "we left the target post", and the run stopped scrolling before it
    ever reached the action row. That is the long-caption failure.

    The guard's real job is to never act on the feed, so test for the feed
    itself: its tab strip is a row of 5-6 equal cells spanning the full width
    near the top, which a single-post view never has.

    The strip alone is not enough, though: a reel opened from the Reels tab
    wears the very same strip, so every reel read as "the news feed". Anything
    carrying a reel rail is a single item, whatever sits above it.
    """
    d = d if getattr(d, "_frozen", False) else Snapshot(d)
    if not _feed_tab_strip(d):
        return False
    return _reel_action_stack(d, trust_reel=True) is None


def _feed_tab_strip(d):
    """The 5-6 equal cells across the top of the feed (and of the Reels tab)."""
    width, height = safe_window_size(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        bw = x2 - x1
        if y1 > int(height * 0.25):
            continue
        if bw < int(width * 0.10) or bw > int(width * 0.25):
            continue
        rows.setdefault(round((y1 + y2) / 2 / 26), []).append(b)
    for _, row in rows.items():
        row = sorted(row, key=lambda b: b[0])
        if (len(row) >= 5
                and row[0][0] <= int(width * 0.06)
                and row[-1][2] >= int(width * 0.94)):
            return True
    return False


def comment_area_in_view(d):
    """True when the comment composer / comments header is on screen.

    That is the landmark that says the action row is close: it sits directly
    above the comment area. Used to switch from big jumps to the small proven
    steps, so a long caption is crossed fast without overshooting the row.
    """
    try:
        text = visible_text(d)
    except Exception:
        return False
    return post_view_text_match(text)


def _screen_signature(d):
    """Cheap fingerprint of what is on screen, to notice when scrolling has
    stopped moving (bottom of the post).

    A caption-only screen exposes no clickable nodes, so its fingerprint can
    repeat while the page is still scrolling fine. That is why the stall limit
    is 8 rather than 2 - long enough to ride out those blank stretches, short
    enough to stop a genuinely stuck post quickly.
    """
    try:
        items = clickable_bounds(d)
    except Exception:
        return None
    return tuple(sorted(items))[:24]



# ── Safe gestures + text-selection handling ────────────────────────────────
# Ported after a fleet run showed the cause of "it never likes the post":
# dragging down the MIDDLE of a long caption gets read by Android as a long
# press on the text, which raises the "Copy text" selection bar. That bar is
# modal, so every later tap goes to it instead of the post and the reaction is
# cancelled. Two things fix it: start every drag somewhere that is not caption
# text or a link (_swipe_path), and be able to SEE the bar when it does appear.

_SCROLL_COLUMNS = (0.86, 0.90, 0.93, 0.88, 0.91, 0.84)


def _native_swipe(d, x1, y1, x2, y2, ms=140):
    """Scroll with `input swipe` instead of uiautomator's gesture.

    uiautomator presses, then moves; on a loaded phone that press can dwell
    past the long-press threshold before the move arrives, and on a caption
    that dwell IS a long press. `input swipe` is interpolated by the input
    subsystem and starts moving immediately, so it cannot be read as a hold.
    """
    serial = getattr(d, "serial", None)
    if serial:
        rc, _, _ = run(["shell", "input", "swipe", str(int(x1)), str(int(y1)),
                        str(int(x2)), str(int(y2)), str(int(ms))], serial=serial)
        if rc == 0:
            return True
    try:
        d.swipe(int(x1), int(y1), int(x2), int(y2), ms / 1000.0)
        return True
    except Exception:
        return False


def _native_tap(d, x, y):
    """Tap through the device's own input subsystem."""
    serial = getattr(d, "serial", None)
    if serial:
        rc, _, _ = run(["shell", "input", "tap", str(int(x)), str(int(y))],
                       serial=serial)
        if rc == 0:
            return True
    try:
        d.click(int(x), int(y))
        return True
    except Exception:
        return False


def _selection_bar_showing(d):
    """True when Android's text-selection ("Copy text") bar is up, by SHAPE.

    It cannot be found by label: on these phones the bar renders NO text into
    the accessibility tree — visible_text() returns nothing but status-bar
    notifications while "Copy text" is plainly on screen. Raising it
    deliberately and diffing the tree showed what it does add:

        (2, 1526, 716, 1584)   inset panel, h=58, at 94% of a 1612px screen
        (17, 1553, 35, 1572)   the icon

    The panel is INSET a couple of pixels from each edge and floats above the
    bottom. That is what separates it from the Android NAVIGATION BAR
    (0, 1564, 720, 1612), which spans edge to edge and ends exactly at screen
    height — matching that was a false positive that reported a bar on phones
    which had none.
    """
    try:
        width, height = safe_window_size(d)
        if not width or not height:
            return False
        root = hierarchy_root(d)
        if root is None:
            return False
        for node in root.iter("node"):
            b = parse_bounds(node.attrib.get("bounds"))
            if not b:
                continue
            x1, y1, x2, y2 = b
            if not (1 <= x1 <= 8):
                continue
            if not (width - 8 <= x2 <= width - 1):
                continue
            if not (30 <= (y2 - y1) <= 90):
                continue
            if y1 < int(height * 0.90):
                continue
            if y2 > height - 10:
                continue
            return True
        return False
    except Exception:
        return False


def dismiss_text_selection(d):
    """Clear the text-selection bar if it is up. Returns True if it cleared one.

    Detected by shape (see _selection_bar_showing) because these phones expose
    no text for it. Left up it is modal and swallows every tap meant for the
    reaction.
    """
    try:
        text = visible_text(d).lower()
    except Exception:
        text = ""
    named = ("copy text" in text or "select all" in text
             or "kopyahin ang text" in text)
    if not (named or _selection_bar_showing(d)):
        return False
    for _ in range(3):
        try:
            d.press("back")
        except Exception:
            break
        time.sleep(0.35)
        if not _selection_bar_showing(d):
            break
    return True


# ── Links in the caption ────────────────────────────────────────────────────
# "It opens the link in the caption and never reacts, then does the same on the
# next account." A URL, hashtag or tagged name in a caption is a clickable node
# of its own, and a link post adds a full-width preview card under the caption.
# Any touch that lands on one of those leaves the post: Facebook opens the link,
# the run finds no action row on a web page, gives up on the link and moves to
# the next account - which presses the very same spot. So every touch the
# reaction path makes into the post body has to know where the links are.
#
# Found by SHAPE, like everything else here (these phones expose no text): a
# caption or a comment body is a node that is clickable AND long-clickable -
# long press is "Copy text" - and every link / hashtag / mention / comment
# control is a clickable nested inside it. The post's own Like | Comment |
# Share row never is: 38 of 38 action rows in the saved dumps sat outside every
# such block.
RISK_NONE, RISK_TEXT, RISK_MEDIA, RISK_BUTTON, RISK_LINK = range(5)


class TouchMap:
    """One read of the screen: what a finger would land on, anywhere."""
    __slots__ = ("width", "height", "clicks", "blocks", "links")

    def __init__(self, d):
        self.width, self.height = safe_window_size(d)
        self.clicks, self.blocks = [], []
        root = hierarchy_root(d)
        if root is not None:
            for node in root.iter("node"):
                a = node.attrib
                b = parse_bounds(a.get("bounds"))
                if not b:
                    continue
                if a.get("clickable") == "true":
                    self.clicks.append(b)
                if a.get("long-clickable") != "true":
                    continue
                if a.get("package", FB_PACKAGE) != FB_PACKAGE:
                    continue
                cls = a.get("class", "")
                if "EditText" in cls or "AutoCompleteTextView" in cls:
                    continue                    # the comment box, not text
                self.blocks.append(b)
        blocks = set(self.blocks)
        self.links = {b for b in self.clicks
                      if b not in blocks and any(_contains(t, b) for t in blocks)}

    def risk(self, pt):
        """What a finger landing at pt would press, worst case first."""
        x, y = pt
        over = [b for b in self.clicks
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]]
        if not over:
            return RISK_NONE
        if any(b in self.links for b in over):
            return RISK_LINK
        inner = min(over, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        bw, bh = inner[2] - inner[0], inner[3] - inner[1]
        if inner in self.blocks:
            return RISK_TEXT                    # plain caption words
        if bw >= self.width * 0.90 and bh >= self.height * 0.40:
            return RISK_TEXT                    # the post body's own backdrop
        if bw >= self.width * 0.60 and bh >= self.height * 0.10:
            return RISK_MEDIA                   # photo, video, LINK PREVIEW card
        return RISK_BUTTON                      # avatar, name, menu, ...


def _contains(outer, inner):
    return (outer != inner and outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def _swipe_path(tm, y_from, y_to, step=0):
    """A scroll gesture whose finger-down point is not on a link.

    Only the point where the finger goes DOWN decides what gets pressed: once
    the post stops moving - at its end, or while it is still loading - FB Lite
    hands the whole swipe to whatever is under that point as a tap. A fixed
    column put it on a URL line or the link preview card sooner or later.
    Candidates are tried from the usual right-hand column outwards and a little
    up the screen; the first point touching nothing at all wins, else the one
    that risks least. The swipe length never changes, so the action row still
    moves by exactly the step it was tuned for.
    """
    width, height = tm.width, tm.height
    dist = int(height * (y_from - y_to))
    k = int(step) % len(_SCROLL_COLUMNS)
    # never the outer ~10%: on gesture-navigation phones a swipe starting at
    # the screen edge belongs to the system, and any sideways drift is BACK
    xs = _SCROLL_COLUMNS[k:] + _SCROLL_COLUMNS[:k] + (0.78, 0.70, 0.60, 0.50,
                                                      0.40, 0.30, 0.20, 0.12)
    best = None
    for dy in (0.0, -0.05, 0.04, -0.10, -0.15, -0.20):
        y1 = int(height * (y_from + dy))
        if y1 > int(height * 0.88) or y1 - dist < int(height * 0.10):
            continue
        for fx in xs:
            x1 = int(width * fx)
            r = tm.risk((x1, y1))
            if best is None or r < best[0]:
                best = (r, x1, y1)
            if r == RISK_NONE:
                return x1, y1, x1, y1 - dist
    if best is None:
        x1, y1 = int(width * _SCROLL_COLUMNS[k]), int(height * y_from)
    else:
        _, x1, y1 = best
    return x1, y1, x1, y1 - dist


def _drag_ms(y1, y2):
    """How long a scroll drag takes: slow enough that the list does not FLING.

    Measured on 1487170579000573 with the Like row just below the fold: the
    uiautomator container scroll and a 140 ms `input swipe` both threw the
    post ~1300px - 80% of the screen - in one go, straight past the band the
    row detector reads, so the row was never seen and the run reported
    no-action-row on a post with a perfectly good row. At ~1.4 ms per pixel
    the post moves by the drag itself (415px drag -> ~455px, 816px -> ~870px),
    which is what SCROLL_FINE / SCROLL_COARSE were tuned for.
    """
    return max(140, int(abs(y1 - y2) * 1.4))


def _safe_scroll(d, y_from, y_to, step=0, tm=None):
    """Scroll the post down one step without pressing anything on the way.

    A controlled drag from a spot that touches no link (_swipe_path). This used
    to ask the scrollable container to scroll itself first, but uiautomator
    puts that finger down at the container's centre, 10% above its bottom edge
    - on a link post, the preview card or the URL line - and it flings the post
    ~80% of a screen per step (see _drag_ms).
    """
    tm = tm or TouchMap(d)
    x1, y1, x2, y2 = _swipe_path(tm, y_from, y_to, step)
    return _native_swipe(d, x1, y1, x2, y2, _drag_ms(y1, y2))


def _left_the_post(d):
    """True when a press opened something else - a web page, another app, a
    profile, a hashtag feed - instead of leaving us on the target post."""
    if stop_requested():
        return False
    if not _fb_foreground(d):
        return True                     # a web page / another app took over
    snap = Snapshot(d)
    xml = snap.dump_hierarchy() or ""
    if xml and FB_PACKAGE not in xml:
        return True
    if on_post_view(snap):
        return False
    return not on_post_view(d, timeout=2.0)


def back_to_post(d, presses=2):
    """Undo a navigation away from the target post. BACK first, since that is
    what a person would do; reopen the link when BACK is not enough.
    presses=0 goes straight to reopening - for when our own BACK is what
    closed the post, and another one would only go further away."""
    for _ in range(presses):
        if stop_requested():
            return False
        try:
            d.press("back")
        except Exception:
            pass
        if not abort_sleep(1.0):
            return False
        if _fb_foreground(d) and on_post_view(d, timeout=3.0):
            print("    [react] back on the target post")
            return True
    serial, url = getattr(d, "serial", None), current_post_url()
    if serial and url and not stop_requested():
        print("    [react] BACK did not return to the post - reopening the link")
        try:
            return bool(open_target_post(serial, d, url))
        except Exception:
            return False
    return False


def undo_stray_press(d, before=None):
    """After a press that did NOT open the reaction tray, put the post back.

      * it opened something (a caption link, a profile, a web page, another
        app): go back to the post, reopening the link if BACK is not enough;
      * it raised a menu, a sheet or the "Copy text" bar over the post: close it;
      * nothing happened at all: do NOTHING. BACK there closes the post itself,
        and the next press then lands on whatever screen was underneath - a
        random feed post, the launcher.

    `before` is _screen_signature() from just before the press. Returns
    "stayed" (the press left the post where it was), "returned" (it opened
    something and we are back on the post) or "lost" (the post is gone).
    """
    def _back(presses=2):
        return "returned" if back_to_post(d, presses) else "lost"

    try:
        dismiss_text_selection(d)
    except Exception:
        pass
    if _left_the_post(d):
        print("    [react] that press left the post (a link in the caption?) - "
              "going back")
        return _back()
    if before is not None and _screen_signature(d) != before:
        try:
            d.press("back")             # something popped over the post
        except Exception:
            pass
        if not abort_sleep(0.6):
            return "lost"
        if _left_the_post(d):
            # OUR back closed the post, the press itself went nowhere - so
            # this is no evidence against the spot that was pressed
            return "stayed" if back_to_post(d, 0) else "lost"
    return "stayed"


# Rows that, when pressed, took the phone OFF the post: whatever they are, they
# are not a Like button - the real one never navigates anywhere. Kept per phone
# for the rest of the run. Two strikes, not one: a stray system dialog landing
# during a press must never be able to blacklist the real Like button.
LINK_ROW_STRIKES = 2
_link_rows = {}
_link_rows_lock = threading.Lock()


def _row_at(d, pt):
    """The detected action row that the point pt falls in, or None."""
    try:
        for row in _post_action_rows(d):
            for b in row:
                if b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3]:
                    return row
    except Exception:
        pass
    return None


def _note_link_row(d, row):
    if not row:
        return
    width, height = safe_window_size(d)
    sig = row_signature(row, width, height)
    with _link_rows_lock:
        seen = _link_rows.setdefault(_rows_key(d), {})
        seen[sig] = seen.get(sig, 0) + 1
        strikes = seen[sig]
    if strikes >= LINK_ROW_STRIKES:
        print(f"    [react] {sig} opened a link again - it is not the action "
              f"row, never pressing that shape again this run")
    else:
        print(f"    [react] {sig} opened a link instead of the tray")


def _is_link_row(d, row, width, height):
    with _link_rows_lock:
        known = _link_rows.get(_rows_key(d))
        if not known:
            return False
        return known.get(row_signature(row, width, height), 0) >= LINK_ROW_STRIKES


def _scroll_to_post_like(d, max_scrolls=None):
    # A long caption, a tall photo or a stack of images pushes the like/comment
    # row well below the fold, so keep looking for it — but re-check on every
    # scroll that we are STILL on the target post. That keeps it adaptive for
    # big posts while never letting it wander into the feed and act on the
    # wrong thing. Bails out immediately on Abort.
    # This mirrors the original, proven algorithm and must not be "improved":
    #
    #  * Facebook Lite only renders the action row into the accessibility tree
    #    when it is near the MIDDLE of the screen, so the row is found by
    #    scrolling DOWN in small steps and polling after each one.
    #  * Never drag downwards from near the top of the screen to "go back up":
    #    that is Facebook's pull-to-refresh gesture. It reloads the post, the
    #    row never settles, and the run scrolls forever without reacting.
    #  * Detection is geometric only. The buttons carry no content-desc and no
    #    text in the real a11y tree, so a label lookup can never match and only
    #    costs an extra full UI dump per poll.
    #  * NEVER swipe when the target is a reel. On the reel player a swipe is
    #    "next reel", not "scroll down this post", so every step here silently
    #    changes which reel the run is looking at - it ends up reacting to a
    #    stranger's reel instead of the link it was given. The reel path needs
    #    no scrolling at all: the rail is always on screen.
    # Surface only - deliberately NOT the url as well. A reel link often opens
    # as an ordinary post permalink ("Post ni/ng ...", comments, composer), and
    # there scrolling is both correct and safe. Gating this on _is_reel_context()
    # was measured on the fleet: /share/r/1YoZ88Fbmv opened as a post view on 5
    # of 20 phones, scrolling was refused, the action row was never reached and
    # all 5 returned no-action-row. The screen decides, not the link.
    if detect_surface(d) in (SURFACE_REEL, SURFACE_VIDEO):
        return None
    width, height = safe_window_size(d)
    for _ in range(3):                      # let the post finish rendering
        if stop_requested():
            return None
        pt = _find_post_like(d)
        if pt:
            return pt
        if not abort_sleep(0.7):
            return None

    off_target = 0
    budget = SCROLL_BUDGET_MAX if max_scrolls is None else max_scrolls
    last_sig = None
    stalls = 0
    coarse_used = 0
    was_on_post = False
    for i in range(budget):
        if stop_requested():
            return None
        # a dump can come back empty for a frame mid-scroll; only give up on
        # two consecutive misses, never on a single bad read
        # ONE dump answers both questions: is the action row already on
        # screen, and are we still on the target post? Asking them separately
        # meant two ~140 ms dumps of the very same frame. Checking for the row
        # BEFORE swiping also means we stop the moment it appears.
        snap = Snapshot(d)
        pt = _find_post_like(snap)
        if pt:
            if i:
                print(f"    [react] found action row after {i} scroll(s)")
            return pt
        # Bail only on POSITIVE evidence of the feed. "I cannot see a comment
        # marker" is not evidence of leaving - on a long caption those markers
        # are simply still below the fold, and treating that as drift is what
        # stopped big posts from ever reacting.
        if on_post_view(snap):
            off_target = 0
            was_on_post = True
        elif on_feed_view(snap):
            off_target += 1
            if off_target >= 2:
                print("    [react] drifted onto the news feed - stopping")
                return None
        elif was_on_post and _left_the_post(d):
            # The post was on screen before the last swipe and is gone now:
            # the gesture opened something - a link in the caption, the link
            # preview card. Scrolling on would scroll the WEB PAGE until the
            # budget ran out; go back to the post and carry on from there.
            print("    [react] the last swipe opened something (a link in the "
                  "caption?) - going back to the post")
            if not back_to_post(d):
                return None
            last_sig, stalls = None, 0
            continue
        else:
            off_target = 0          # mid-caption: nothing to conclude yet
        # Stop early when the post has stopped moving: we are at the bottom
        # and more swipes cannot reveal anything. Without this, raising the
        # budget for long captions would waste ~30 pointless swipes on every
        # ordinary post that genuinely has no action row.
        sig = _screen_signature(snap)
        if sig is not None and sig == last_sig:
            stalls += 1
            if stalls >= SCROLL_STALL_LIMIT:
                print("    [react] reached the end of the post without an "
                      "action row - stopping")
                return None
        else:
            stalls = 0
        last_sig = sig

        # Far from the comment area -> cross the caption in big jumps and do
        # not wait around, there is nothing to settle yet. Close to it -> the
        # original small step with the original settle time, untouched.
        near = comment_area_in_view(snap)
        if near:
            y_from, y_to = SCROLL_FINE
            polls, settle = 2, 0.7
        else:
            y_from, y_to = SCROLL_COARSE
            polls, settle = 1, 0.35
            coarse_used += 1
        try:
            # A slow drag from a spot that touches no link. Dragging down the
            # middle of a caption is what raises the "Copy text" bar, and what
            # opens a URL / hashtag / the link preview card once the post stops
            # scrolling; a fast one flings the row past unseen. `snap` is this
            # very frame - the screen has not moved since it was read.
            _safe_scroll(d, y_from, y_to, i, tm=TouchMap(snap))
        except Exception:
            pass
        # If a gesture still managed to raise the selection bar, clear it now:
        # left up it is modal and every later tap lands on it, not the post.
        try:
            if dismiss_text_selection(d):
                print("    [react] cleared a 'Copy text' bar raised while scrolling")
        except Exception:
            pass
        for _ in range(polls):              # poll as the row settles near centre
            if not abort_sleep(settle):
                return None
            pt = _find_post_like(d)
            if pt:
                print(f"    [react] found action row after {i + 1} scroll(s)")
                return pt
    return None


# ── What am I looking at? ───────────────────────────────────────────────────
# Facebook opens the SAME link on different surfaces - a /share/p/ link is a
# post for one id and a reel for the next - so the URL cannot decide how to
# act. Every detector below reads the screen instead, by shape, because the
# real buttons carry no text or content-desc on these phones.
#
# tests/surface_replay.py holds this honest: it replays real captured screens
# through detect_surface() offline, so a change that "fixes" reels by breaking
# posts is caught in a second instead of on twenty phones.
SURFACE_POST = "post"
SURFACE_REEL = "reel"
SURFACE_PHOTO = "photo"
SURFACE_VIDEO = "video"
SURFACE_FEED = "feed"
SURFACE_LOADING = "loading"
SURFACE_BLOCKED = "blocked"
SURFACE_UNKNOWN = "unknown"

# Screens that mean "this account cannot open anything", found on the fleet
# while verifying: one phone sat on Facebook's ID checkpoint ("I-upload ang
# iyong ID") and another on the logged-out account picker, and the run spent
# 18 open attempts on each before giving up with a generic open-post-failed.
# Retrying cannot fix either; the account has to be switched or logged in.
CHECKPOINT_MARKERS = (
    # English
    "upload your id", "confirm your identity", "help us confirm",
    "we need to confirm", "confirm it's you", "confirm that it's you",
    "your account has been locked", "account locked",
    # Filipino
    "i-upload ang iyong id", "kumpirmahin ang iyong pagkakakilanlan",
    "kailangan namin tingnan", "kailangan naming tingnan",
    "naka-lock ang iyong account", "kumpirmahin na ikaw",
)

# Surfaces a reaction may be aimed at. The feed is deliberately excluded: it
# holds many posts and acting there hits whichever one is under the cursor.
ACTIONABLE_SURFACES = (SURFACE_POST, SURFACE_REEL, SURFACE_PHOTO, SURFACE_VIDEO)


def _bottom_action_row(d):
    """A full-screen player's action row, pinned to the very bottom.

    The video/live player draws Like/Comment/Share as three equal cells at
    cy ~0.94 - below _find_post_like()'s 0.92 ceiling, which exists to keep
    Android's nav bar out - so that row is invisible to the post detector and
    the player looked like a screen with nothing to press.
    """
    width, height = safe_window_size(d)
    row = []
    for b in clickable_bounds(d):
        cy = (b[1] + b[3]) / 2
        if cy < height * 0.88:
            continue
        w = b[2] - b[0]
        if not (width * 0.25 <= w <= width * 0.40):
            continue
        if not (50 <= b[3] - b[1] <= 130):
            continue
        row.append(b)
    if len(row) < 3:
        return None
    row.sort(key=lambda b: b[0])
    if row[0][0] > int(width * 0.08) or row[-1][2] < int(width * 0.92):
        return None                     # must tile the whole width
    return center(row[0])


def _photo_surface(d):
    """The photo viewer: one big image and almost nothing else.

    No composer, no action row, no text of any kind - which is exactly why the
    old text-based post lock could never pass here and photo links were always
    refused. The row appears after one scroll; getting there is _react_photo's
    job, recognising the surface is this one's.
    """
    width, height = safe_window_size(d)
    items = clickable_bounds(d)
    if len(items) > 6:
        return False
    for b in items:
        if (b[2] - b[0]) >= int(width * 0.90) and (b[3] - b[1]) >= int(height * 0.45):
            return True
    return False


def blocked_reason(d):
    """Why this account cannot act here - or None when nothing is in the way."""
    try:
        text = visible_text(d).lower()
    except Exception:
        return None
    if any(m in text for m in CHECKPOINT_MARKERS):
        return "checkpoint"
    try:
        if is_suspended(d):
            return "suspended"
    except Exception:
        pass
    try:
        if is_password_prompt(d):
            return "password-prompt"
        if not is_logged_in(d):
            return "logged-out"
    except Exception:
        pass
    return None



def _permalink_header_showing(d):
    """True on a single-item permalink view, judged by its chrome.

    A permalink (post / photo / video) replaces the feed's 6-cell tab strip
    with a plain header: one small button in each TOP CORNER — back on the
    left, search on the right — and the item's body filling the rest.
    Measured on a real post: (0,67,96,160) and (608,67,712,160) on a 720px
    screen, with the body as one full-width block 1259px tall.

    This is the last resort for a post that offers nothing else to recognise:
    a caption long enough to push BOTH the action row and the comment box off
    screen, on a phone that exposes no text at all. Such a post was open and
    still refused as "not on the target post", and no amount of scrolling
    helped because the run would not accept the screen in the first place.

    The tall body block is what keeps the MENU out: it wears the same corner
    buttons, but its entries are half-width tiles, never one block over 40% of
    the screen.
    """
    try:
        width, height = safe_window_size(d)
        if not width or not height:
            return False
        if _feed_tab_strip(d):
            return False
        bounds = clickable_bounds(d)
        top = [b for b in bounds
               if center(b)[1] < int(height * 0.13)
               and 60 <= (b[3] - b[1]) <= 140
               and int(width * 0.07) <= (b[2] - b[0]) <= int(width * 0.22)]
        left = any(center(b)[0] < width * 0.20 for b in top)
        right = any(center(b)[0] > width * 0.80 for b in top)
        if not (left and right):
            return False
        body = any((b[2] - b[0]) >= int(width * 0.90)
                   and (b[3] - b[1]) >= int(height * 0.40)
                   for b in bounds)
        return body
    except Exception:
        return False


def _is_watch_link(url):
    """A link that opens Facebook's video / live player - not a reel, not a
    plain text post. These open INSIDE the Videos tab, which wears the news
    feed's top strip, so detect_surface() needs the url to tell the target
    video apart from an ordinary feed."""
    return bool(re.search(r"/videos?/\d|/watch(?:/|\?|$)|/video\.php|"
                          r"[?&]v=\d|/live(?:_videos)?/", str(url or ""), re.I))


def detect_surface(d):
    """post / reel / photo / video / feed / loading / unknown - by shape only.

    Order matters and is not arbitrary:
      1. the composer pins a post permalink; a reel never shows one
      2. the rail pins the reel player - checked before the feed, because the
         Reels tab wears the same 6-cell top strip as the news feed
      3. the feed, so we never react to a random post
      4. the bottom-row player (video/live), then the ordinary action row
      5. the photo viewer, then "nothing has rendered yet"
    """
    d = d if getattr(d, "_frozen", False) else Snapshot(d)
    try:
        if blocked_reason(d):
            return SURFACE_BLOCKED
    except Exception:
        pass
    try:
        if post_view_text_match(visible_text(d)):
            return SURFACE_POST
    except Exception:
        pass
    # Same test, but by widget instead of words — the docstring above says the
    # composer pins a post permalink, and on a phone that exposes no text this
    # is the only way to see it. Checked before the reel rail so a post is
    # never mistaken for one, and before the "bare screen" test so a caption
    # that fills the display is not written off as still loading.
    try:
        if _composer_node_in_view(d):
            return SURFACE_POST
    except Exception:
        pass
    try:
        # trust_reel stays True here. Gating it on the feed strip looks like it
        # would stop the news feed passing as a reel, but it does not: a real
        # Reels player draws its own bottom overlay - avatar, name, Follow at
        # (24,1326) (134,1352) (423,1345) - which _find_post_like() reads as an
        # action row, so the veto fires on genuine reels too and every reel
        # goes back to being refused. The feed lookalike is rejected inside
        # _evenly_stacked() by column alignment instead.
        if _reel_action_stack(d, trust_reel=True) is not None:
            return SURFACE_REEL
    except Exception:
        pass
    # "Nothing but chrome" has to be judged before the feed, or a Reels tab
    # whose video has not painted yet reads as the news feed - and the two want
    # opposite handling: wait for a loading screen, reopen the link for a feed.
    try:
        # A post whose caption is long and mostly TEXT exposes almost no
        # clickable nodes at the top - measured on FB Lite 527: exactly 8, all
        # of them chrome (back, search, the author row, one body block). The
        # count alone therefore cannot tell "still loading" from "a post is
        # right there", and calling it loading made on_post_view() refuse, the
        # open be declared failed and the link skipped with the post plainly on
        # screen. _permalink_header_showing() is the difference: a loading
        # screen has no author header and no full-height body block.
        bare = (len(clickable_bounds(d)) <= 8
                and _find_post_like(d) is None
                and _bottom_action_row(d) is None
                and not _photo_surface(d)
                and not _permalink_header_showing(d))
        if bare:
            return SURFACE_LOADING
    except Exception:
        pass
    try:
        if _feed_tab_strip(d):
            # A video / live link opens INSIDE the Videos tab, which wears the
            # feed's top strip - but we navigated to it on purpose and the
            # target video's action row is right there, so react to it rather
            # than refusing the whole surface as the news feed. Gated on the
            # target URL being a watch link, so a real feed is still refused.
            try:
                _watch = _is_watch_link(current_post_url())
            except Exception:
                _watch = False
            if _watch and _find_post_like(d) is not None:
                return SURFACE_POST
            return SURFACE_FEED
    except Exception:
        pass
    try:
        if _bottom_action_row(d) is not None:
            # Both the photo permalink and the full-screen video player pin a
            # row to the bottom. The photo one is an item inside a post, so it
            # also carries its parent's row further up; the player is on its
            # own. Either way the reaction goes to the BOTTOM row - the item's
            # own - which is what _find_photo_like() returns.
            return SURFACE_PHOTO if _post_action_rows(d) else SURFACE_VIDEO
    except Exception:
        pass
    try:
        if _find_post_like(d) is not None:
            return SURFACE_POST
    except Exception:
        pass
    try:
        if _photo_surface(d):
            return SURFACE_PHOTO
    except Exception:
        pass
    # Last resort: the permalink chrome. A caption long enough to push both the
    # action row and the composer off screen leaves nothing else to match on a
    # phone with no readable text, and returning UNKNOWN there means refusing a
    # post that is plainly open.
    try:
        if _permalink_header_showing(d):
            return SURFACE_POST
    except Exception:
        pass
    return SURFACE_UNKNOWN

def _react_reel(d, reaction, reel_like):
    idx = REACTION_ORDER.index(reaction)
    for attempt in range(2):
        if stop_requested():
            return "stopped"
        # Same invariant as the post path: never long-press unless Facebook
        # Lite is the app in front. A reel that scrolled to the next item, or a
        # stray tap that opened the caption link, must not be pressed blind.
        if not _fb_foreground(d):
            print("    [react] not on Facebook right before the reel press - "
                  "recovering instead of pressing")
            if not back_to_post(d) or not _fb_foreground(d):
                return "react-failed:left-post"
        try:
            d.long_click(reel_like[0], reel_like[1], 1.1 if attempt == 0 else 1.6)
        except Exception as e:
            return f"react-failed:longpress:{e}"
        pt, points = None, None
        # 4.5s, not 3: on these phones the tray fades in, and the first dump
        # after the long-press often catches it half-drawn - which used to be
        # reported as "no tray" on a reel whose tray was opening fine.
        deadline = time.time() + 4.5
        while time.time() < deadline:
            if stop_requested():
                return "stopped"
            pt = _find_reaction_by_label(d, reaction)
            if pt:
                break
            points = find_reel_tray_points(d)
            if points:
                break
            if not abort_sleep(0.25):
                return "stopped"
        if pt:
            try:
                d.click(*pt); time.sleep(1); return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        if points and idx < len(points):
            time.sleep(0.45)
            stable = find_reel_tray_points(d) or points
            if idx < len(stable):
                points = stable
            slot = (len(points) - 1) - idx
            try:
                d.click(*points[slot]); time.sleep(1)
                return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        try:
            d.press("back")
        except Exception:
            pass
        time.sleep(0.6)
    if reaction == "like":
        try:
            d.click(*reel_like); time.sleep(1); return "reacted:like"
        except Exception as e:
            return f"react-failed:tap:{e}"
    return f"react-failed:no-{reaction}-tray"

# -- Per-device UI geometry memory ------------------------------------------
# "Remember the UI per phone and solve the geometry itself." Every reaction
# that lands teaches this cache where Like sat on THAT phone for THAT surface,
# stored as screen fractions so a phone with a different resolution cannot
# poison it. Key: serial|WxH|surface.
#
# A remembered point is only ever used when something clickable is still
# sitting on it (one dump, no blind tap), and GEOM_MAX_MISSES misses forget it
# again - so a post that scrolled elsewhere costs nothing and the phone
# re-learns by itself.
GEOM_FILE = os.path.join(HERE, "ui_geometry.json")
GEOM_MAX_MISSES = 3
_geom_lock = threading.Lock()
_geom_cache = None


def _geom_load():
    global _geom_cache
    if _geom_cache is None:
        try:
            with open(GEOM_FILE, encoding="utf-8") as fh:
                _geom_cache = json.load(fh)
            if not isinstance(_geom_cache, dict):
                _geom_cache = {}
        except Exception:
            _geom_cache = {}
    return _geom_cache


def _geom_save():
    try:
        tmp = GEOM_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_geom_cache, fh, indent=2)
        os.replace(tmp, GEOM_FILE)
    except Exception:
        pass          # a cache that cannot be written still runs, just slower


def _geom_key(d, surface):
    serial = getattr(d, "serial", None)
    if not serial:
        return None
    try:
        w, h = safe_window_size(d)
    except Exception:
        return None
    if not w or not h:
        return None
    return "%s|%dx%d|%s" % (serial, w, h, surface), (w, h)


def remember_geometry(d, surface, like=None, long_press=None):
    """Learn on success - called after every reaction that actually landed."""
    keyed = _geom_key(d, surface)
    if not keyed:
        return
    key, (w, h) = keyed
    with _geom_lock:
        data = _geom_load()
        entry = data.setdefault(key, {})
        if like:
            entry["like"] = [round(like[0] / float(w), 4),
                             round(like[1] / float(h), 4)]
        if long_press:
            entry["long_press"] = round(float(long_press), 2)
        entry["misses"] = 0
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _geom_save()


def recall_like(d, surface):
    keyed = _geom_key(d, surface)
    if not keyed:
        return None
    key, (w, h) = keyed
    with _geom_lock:
        like = (_geom_load().get(key) or {}).get("like")
    if not like or len(like) != 2:
        return None
    return int(like[0] * w), int(like[1] * h)


def recall_long_press(d, surface, default=1.5):
    keyed = _geom_key(d, surface)
    if not keyed:
        return default
    key, _ = keyed
    with _geom_lock:
        hold = (_geom_load().get(key) or {}).get("long_press")
    try:
        hold = float(hold)
    except (TypeError, ValueError):
        return default
    return hold if 0.5 <= hold <= 4.0 else default


def note_geometry_miss(d, surface):
    """Self-correct: forget a point that stopped working."""
    keyed = _geom_key(d, surface)
    if not keyed:
        return
    key, _ = keyed
    with _geom_lock:
        entry = _geom_load().get(key)
        if not entry:
            return
        entry["misses"] = int(entry.get("misses", 0) or 0) + 1
        if entry["misses"] >= GEOM_MAX_MISSES:
            entry.pop("like", None)
            entry["misses"] = 0
        _geom_save()


def _clickable_at(d, point):
    """True when the remembered point sits on something shaped like an ACTION ROW.

    Containment alone is not enough. A post's action row lives wherever its
    caption happens to end, so a point remembered from one post lands anywhere
    on the next — and on a long post that "anywhere" is the comment list, where
    every comment carries its own Like affordance and its own bubble. Accepting
    any clickable there meant pressing a COMMENT and reporting the post as
    reacted: seen live on Ralph Recto's post, where the run liked Macario
    Reyes' comment and returned "reacted:like".

    So the covering node must also look like an action-row cell: a wide, short
    pill. A comment's thumb is a small square at the right margin (~0.09 of the
    width) and fails this; a comment bubble is far taller than a row cell and
    fails it too.
    """
    try:
        width, _ = safe_window_size(d)
        tm = TouchMap(d)
        if tm.risk(point) == RISK_LINK:
            return False                # a caption link now sits there
        for b in tm.clicks:
            if not (b[0] <= point[0] <= b[2] and b[1] <= point[1] <= b[3]):
                continue
            bw, bh = b[2] - b[0], b[3] - b[1]
            if not (40 <= bh <= 140):
                continue
            if not (int(width * 0.10) <= bw <= int(width * 0.60)):
                continue
            return True
    except Exception:
        pass
    return False


def _fb_foreground(d):
    """True when Facebook Lite is the app actually in front.

    Every surface detector here is geometry only, which means a screen from a
    COMPLETELY DIFFERENT app can satisfy one. Measured live on L015271: the
    Google app's Discover feed puts a heart / share / overflow row under each
    article card, _find_post_like() read that as a post action row, and the run
    pressed the heart on an LA Times story while reporting reacted:like. No
    amount of shape-matching can rule that out, so ask the system what is
    really on screen before touching anything.

    mCurrentFocus is useless here - it reads null on these phones even with an
    app plainly in the foreground - so this goes by topResumedActivity.
    """
    serial = getattr(d, "serial", None)
    if not serial:
        return True                 # nothing to check with; never block a run
    try:
        _, out, _ = run(["shell", "dumpsys", "activity", "activities"],
                        serial=serial)
    except Exception:
        return True
    for line in (out or "").splitlines():
        if "topResumedActivity" in line or "mResumedActivity" in line:
            # A link in a post opens in Facebook Lite's OWN browser - same
            # package, so it passed as "Facebook" - measured on 15885375C9006183:
            #   com.facebook.lite/.inappbrowser.activity.
            #       FBLiteBrowserLiteInMainProcessActivity
            # A web page is not the post, whoever draws it.
            return FB_PACKAGE in line and "inappbrowser" not in line.lower()
    return True                     # nothing to go on - let the detectors rule


def react(d, reaction):
    if stop_requested():
        return "stopped"
    # Before any geometry: is this even Facebook? See _fb_foreground().
    if not _fb_foreground(d):
        # Most often a link that an earlier press opened (Facebook Lite's own
        # browser). Step back to the post instead of burning this attempt.
        print("    [react] the post is not on screen (a link opened?) - going "
              "back to it")
        if not back_to_post(d) or not _fb_foreground(d):
            print("    [react] Facebook is not the app on screen - refusing")
            return "react-failed:facebook-not-foreground"
    # Rate-limited? Retrying just burns 200 swipes against a wall.
    if is_action_blocked(d):
        print("    [react] Facebook has action-blocked this account")
        return "action-blocked"
    # Only ever react to the post we were told to react to. If we are not on a
    # post view (e.g. the deep link dumped us on the feed), refuse and let the
    # caller re-open the target URL rather than reacting to a random post.
    if not on_post_view(d, timeout=5.0):
        if stop_requested():
            return "stopped"
        if is_action_blocked(d):
            print("    [react] Facebook has action-blocked this account")
            return "action-blocked"
        # Tell the two apart in the log: a player that has not painted yet
        # wants another moment, a feed wants the link opened again. Both are
        # answered by the caller reopening, but only one of them is a bug.
        seen = detect_surface(d)
        if seen == SURFACE_LOADING:
            print("    [react] nothing rendered yet - not reacting to a blank screen")
            return "react-failed:not-ready"
        print(f"    [react] {seen} on screen, not the target item - "
              f"refusing to react here")
        return "react-failed:not-on-target-post"
    # One question decides everything from here: what is actually on screen?
    # Not what the link said - the same /share/p/ id is a post for one item and
    # a reel for the next, and that guess is what refused reels and photos.
    surface = detect_surface(d)

    if surface == SURFACE_REEL:
        # The rail is always in view on a reel; there is nothing to scroll to,
        # and a swipe here means "next reel" - it would walk off the link.
        rail = find_reel_like_point(d, trust_reel=True)
        if rail is None:
            rail = _wait_reel_like(d, 5.0)
        if rail is None:
            print("    [react] reel not ready - staying on the link instead of "
                  "scrolling off it")
            return "react-failed:reel-not-ready"
        return _react_reel(d, reaction, rail)

    if surface == SURFACE_VIDEO:
        # Full-screen video/live: the action row is pinned at the very bottom,
        # below what _find_post_like() will look at, and swiping changes video.
        like = _find_photo_like(d)
        if like is None:
            return "react-failed:no-action-row"
        return _react_at_row(d, reaction, like, surface)

    if surface == SURFACE_PHOTO:
        # The photo viewer opens with the image alone; its row is one scroll
        # down. Vertical scrolling stays on the same photo (only a sideways
        # swipe moves to the next one), so this is safe.
        like = _find_photo_like(d)
        if like is None and _scroll_to_post_like(d) is not None:
            like = _find_photo_like(d)
        if like is None:
            return "react-failed:no-action-row"
        return _react_at_row(d, reaction, like, surface)

    hold = recall_long_press(d, surface, 1.5)
    like = _scroll_to_post_like(d)
    if like is None and not stop_requested():
        # Scrolling down never met the row. When the post was opened warm it
        # may have come back mid-comments (see open_target_post), with the row
        # ABOVE the screen - start it again from the top, once.
        serial, url = getattr(d, "serial", None), current_post_url()
        if serial and url:
            print("    [react] no action row below - reopening the post from "
                  "the top")
            try:
                if open_target_post(serial, d, url, fresh=True):
                    like = _scroll_to_post_like(d)
            except Exception:
                pass
    if stop_requested():
        return "stopped"

    # Rescue path: Facebook Lite only renders the action row into the a11y
    # tree near mid-screen, so "not detected" is not proof it is not there.
    # This phone reacted on this surface before, so re-use what it taught us -
    # but only when a clickable is genuinely covering that point.
    used_remembered = False
    if like is None:
        remembered = recall_like(d, surface)
        if remembered is not None and _clickable_at(d, remembered):
            print(f"    [react] action row not detected - using remembered "
                  f"{surface} Like at {remembered}")
            like = remembered
            used_remembered = True

    # Self-healing: the shape rules and the remembered point both came up
    # empty, so go and find the Like button by experiment instead of failing.
    if like is None:
        try:
            learned = discover_action_row(d)
        except Exception as e:
            print(f"    [react] self-heal probe failed ({e}) - giving up on "
                  f"this link only")
            learned = None
        if learned is not None:
            like = learned

    if like is None:
        # There used to be one more fallback here: click the biggest picture in
        # the post, hoping it opened a reel player with a rail to press. On a
        # link post the biggest picture IS the link preview card, so this is
        # where the run opened the link, found no rail on the web page, and
        # moved on to the next account to do it again. (Its tray branch also
        # read an undefined `idx` and crashed.) Reels are recognised by shape
        # up front now, so reopening the link is the honest next step.
        return "react-failed:no-action-row"
    return _react_at_row(d, reaction, like, surface,
                         refind=lambda: _scroll_to_post_like(d),
                         used_remembered=used_remembered, hold=hold)


def _react_at_row(d, reaction, like, surface, refind=None,
                  used_remembered=False, hold=None):
    """Long-press a Like button, wait for the tray, tap the right face.

    Shared by every surface that reacts through an action row - post, photo and
    the full-screen video player. Only the reel keeps its own path, because its
    tray is a single reversed row rather than this 3-over-4 grid.
    """
    # used_remembered is cleared on the first tray miss (so the retry re-hunts),
    # which means it cannot tell us at the END whether we ever stood on a real
    # action row. row_confirmed does: it starts true only when the CALLER found
    # the row with the detector, and turns true again only when a refind() does.
    row_confirmed = not used_remembered
    idx = REACTION_ORDER.index(reaction)
    if hold is None:
        hold = recall_long_press(d, surface, 1.5)
    x, y = like
    held = hold
    tray = False

    for attempt in range(2):
        if stop_requested():
            return "stopped"
        held = hold if attempt == 0 else hold + 0.2
        # Hard invariant, checked immediately before the press: the run only
        # ever acts on the target post inside Facebook Lite. on_post_view() was
        # checked back in react(), but a slow phone can slip - a shade, a
        # reopen animation, a stray earlier press - in the gap before we get
        # here, and a blind long_click on whatever took over is exactly how a
        # press lands in Chrome / Google / the launcher. If we are not on
        # Facebook now, recover to the post first; never press off-target.
        if not _fb_foreground(d):
            print("    [react] not on Facebook right before the press - "
                  "recovering instead of pressing")
            if not back_to_post(d) or not _fb_foreground(d):
                return "react-failed:left-post"
            again = refind() if refind is not None else _find_post_like(d)
            if not again:
                return "react-failed:left-post"
            x, y = again
            row_confirmed = True
        snap = Snapshot(d)
        before = _screen_signature(snap)
        pressed_row = _row_at(snap, (x, y))
        try:
            d.long_click(x, y, held)
        except Exception as e:
            return f"react-failed:longpress:{e}"
        if _wait_for_reaction_tray(d, timeout=5.0):
            tray = True
            break
        if stop_requested():
            return "stopped"
        if used_remembered:
            # the remembered spot no longer opens the tray: forget it after
            # GEOM_MAX_MISSES and hunt for the action row the slow way
            note_geometry_miss(d, surface)
            used_remembered = False
        print(f"    [react] tray not found, re-finding Like + retrying...")
        # Undo whatever the press did instead - but only if it did something.
        # This used to be an unconditional BACK: after a press that opened
        # nothing, that closed the post itself, and the retry then pressed
        # whatever screen was underneath.
        undo = undo_stray_press(d, before)
        if undo == "lost":
            return "react-failed:left-post"
        if undo == "returned":
            # It took us OFF the post, so it was a link, not the Like button.
            # Never let the detector offer that shape again this run.
            _note_link_row(d, pressed_row)
            row_confirmed = False
        if not abort_sleep(0.8):
            return "stopped"
        if refind is not None:
            again = refind()
            if again:
                like = again
                row_confirmed = True
                x, y = like

    if stop_requested():
        return "stopped"
    if tray:
        pt = _find_reaction_by_label(d, reaction)
        if pt:
            try:
                d.click(*pt); time.sleep(0.8)
                remember_geometry(d, surface, like=(x, y), long_press=held)
                return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        points = find_reaction_tray_points(d, like_point=like)
        if idx < len(points):
            try:
                d.click(*points[idx]); time.sleep(0.8)
                remember_geometry(d, surface, like=(x, y), long_press=held)
                return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
        # The video player's tray has not been seen open on this fleet, so try
        # the reel's single reversed row too before giving up - one dump.
        row = find_reel_tray_points(d)
        if row and idx < len(row):
            try:
                d.click(*row[(len(row) - 1) - idx]); time.sleep(0.8)
                remember_geometry(d, surface, like=(x, y), long_press=held)
                return f"reacted:{reaction}"
            except Exception as e:
                return f"react-failed:tap:{e}"
    elif reaction != "like":
        # No tray means no emojis on screen. The spots find_reaction_tray_points()
        # would compute are then plain post body 190-410px above the Like button
        # - on a link post, squarely on the link preview card. Tapping them
        # "optimistically" opened the link and reported reacted:<x> with nothing
        # reacted, and the run moved on to the next account to do it again.
        print("    [react] the tray never opened - not tapping blind into the "
              "post (that is where the link preview sits)")
        return "react-failed:no-tray"
    if reaction == "like":
        # A remembered point that never opened the tray has not been shown to
        # be the Like button at all. Tapping it anyway - and calling that a
        # reaction - is how a comment got liked while the run reported success,
        # and remember_geometry() then wrote that bad spot down for next time.
        # Fail honestly instead so the caller reopens and hunts properly.
        if not row_confirmed:
            print("    [react] remembered Like never opened the tray and the "
                  "row was never found - not tapping it blind")
            return "react-failed:remembered-point-wrong"
        if not tray and not _clickable_at(d, (x, y)):
            print("    [react] no Like button under that point any more - "
                  "not tapping it blind")
            return "react-failed:no-tray"
        try:
            d.click(x, y); time.sleep(1.2)
            remember_geometry(d, surface, like=(x, y), long_press=held)
            return "reacted:like"
        except Exception as e:
            return f"react-failed:direct-tap{e}"
    return f"react-failed:no-{reaction}-point"

def normalize_account_name(value):
    value = html.unescape(value or "").strip()
    # "9+ notifications" (English) and "9+ na notification" (Taglish) both turn
    # up on this fleet; matching only the English form left the count glued to
    # the name, so every comparison against used_accounts missed.
    value = re.sub(r",?\s*\d+\+?\s+(?:na\s+)?notifications?\b", "",
                   value, flags=re.I).strip()
    value = re.sub(r"\s+", " ", value)
    return value

def is_password_prompt(d):
    text    = visible_text(d).lower()
    markers = ("password", "enter your password", "forgot password",
               "incorrect password",
               # Filipino builds
               "ilagay ang iyong password", "nakalimutan ang password",
               "mali ang password", "hindi tama ang password")
    return (any(m in text for m in markers) and
            ("log in" in text or "login" in text or "continue" in text
             or "magpatuloy" in text or "ituloy" in text))

def is_suspended(d):
    """True only when this account is really suspended.

    Deliberately biased towards MISSING a suspension rather than inventing one.
    A false negative costs a little wasted time on a dead account; a false
    positive logs a healthy account out of the phone for good. So: an
    unambiguous first-person message is proof on its own, and generic wording
    counts only when the suspension layout backs it up. The bare layout is no
    longer enough by itself - it is pure geometry, and it was reachable with no
    text evidence at all.
    """
    text = visible_text(d).lower()
    if any(m in text for m in SUSPENDED_STRONG):
        return True
    if any(m in text for m in SUSPENDED_WEAK) and is_suspended_layout(d):
        return True
    return False

def is_suspended_layout(d):
    width, height = safe_window_size(d)
    items = clickable_items(d)
    if not (3 <= len(items) <= 6):
        return False
    points = [(b, center(b)) for b, _ in items]
    has_top_right = any(
        x > int(width * 0.85) and int(height * 0.04) <= y <= int(height * 0.12)
        and 40 <= b[2]-b[0] <= 90 and 40 <= b[3]-b[1] <= 90
        for b, (x, y) in points)
    has_profile = any(
        int(width*0.40) <= x <= int(width*0.60) and
        int(height*0.10) <= y <= int(height*0.22) and
        70 <= b[2]-b[0] <= 130 and 70 <= b[3]-b[1] <= 130
        for b, (x, y) in points)
    has_appeal_button = any(
        int(width*0.03) <= b[0] <= int(width*0.08) and
        b[2] >= int(width*0.92) and
        int(height*0.35) <= y <= int(height*0.48) and
        45 <= b[3]-b[1] <= 90
        for b, (x, y) in points)
    return has_top_right and has_profile and has_appeal_button

def wait_for_account_block(d, timeout=6):
    """Watch for a suspended / logged-out / password-prompt screen.

    Three things mattered here, all measured on a real run where this was the
    single most expensive step (7.4s per call, twice per account switch):

      * the three checks each pulled their OWN dump, so one iteration cost
        three round trips - they now share one Snapshot;
      * it slept with time.sleep(), so Abort could not interrupt it;
      * a healthy account never returns early, so it always burned the whole
        timeout. That is inherent to waiting for a bad screen to appear, so
        callers that are about to read the UI anyway pass a short timeout.
    """
    deadline = time.time() + timeout
    pending = None
    while True:
        snap = d if getattr(d, "_frozen", False) else Snapshot(d)
        if is_suspended(snap):
            found = "suspended"
        elif is_password_prompt(snap) or not is_logged_in(snap):
            found = "not-successful"
        else:
            found = None
        if found:
            # A Facebook that is still loading briefly looks logged out, and
            # the old code believed the first reading - marking a healthy
            # account blocked. Require the same verdict twice in a row. With
            # timeout=0 there is no second look, so report it as before.
            if timeout <= 0 or pending == found:
                return found
            pending = found
        else:
            pending = None
        if time.time() >= deadline:
            return None
        if not abort_sleep(min(0.6, max(0.05, deadline - time.time()))):
            return None

def open_fb_home(serial, d, force=True):
    # Never relaunch Facebook after Abort — this function restarts the app and
    # taps around the home/search chrome, which is exactly the "it reopened and
    # started doing things on its own" behaviour.
    if stop_requested():
        return
    if force:
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
        poll_until(lambda: not fb_foreground(serial), 1.5)
    if stop_requested():
        return
    run(["shell", "am", "start", "-n", FB_ACTIVITY], serial=serial)
    poll_until(lambda: fb_foreground(serial), 4)
    if chooser_present(serial, d):
        dismiss_chooser(serial, d)
        poll_until(lambda: not chooser_present(serial, d), 2)
    width, height = safe_window_size(d)
    for _ in range(4):
        if stop_requested():
            return
        text = visible_text(d)
        if not any(marker in text for marker in ("Search...", "Search Facebook")):
            break
        d.click(int(width * 0.058), int(height * 0.074))
        if not abort_sleep(1.0):
            return

def find_gear_icon(d):
    width, height = safe_window_size(d)
    candidates = []
    for b, attrs in clickable_items(d):
        cx, cy = center(b)
        if cy > int(height * 0.12) or cx < int(width * 0.80):
            continue
        w, h = b[2]-b[0], b[3]-b[1]
        if 40 <= w <= 100 and 40 <= h <= 100:
            candidates.append((cx, b))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return center(candidates[0][1])

def wait_for_gear_icon(d, timeout=15):
    deadline  = time.time() + timeout
    last_gear = None
    while time.time() < deadline:
        gear = find_gear_icon(d)
        if gear is not None:
            last_gear = gear
        if gear is not None and (is_suspended(d) or is_suspended_layout(d)):
            return gear
        time.sleep(1)
    return last_gear if (is_suspended(d) or is_suspended_layout(d)) else None

def open_gear_menu(serial, d):
    open_fb_home(serial, d)
    gear = wait_for_gear_icon(d)
    if gear is None:
        return False
    d.click(*gear)
    time.sleep(2)
    return True

def find_logout_row(d):
    for b, attrs in clickable_items(d):
        text = normalize_account_name(attrs.get("content-desc") or attrs.get("text"))
        low  = text.lower()
        # "Mag-log out" / "Mag-logout" already contain the English forms;
        # "sign out" is the one wording they do not cover.
        if "log out" in low or "logout" in low or "sign out" in low:
            return center(b)
    width, height = safe_window_size(d)
    sheet_rows = []
    for b, _ in clickable_items(d):
        w  = b[2]-b[0]
        h  = b[3]-b[1]
        cx, cy = center(b)
        if (b[0] <= int(width*0.08) and b[2] >= int(width*0.90) and
                cy > int(height*0.70) and w > int(width*0.75) and h >= 50):
            sheet_rows.append((cy, b))
    if len(sheet_rows) >= 2:
        return center(sorted(sheet_rows)[-1][1])
    return None

def confirm_logout_if_needed(d):
    text = visible_text(d).lower()
    # Filipino: "Mag-log out?" / "Sigurado ka bang gusto mong mag-log out?"
    # with an "Oo" button - none of which the English-only test matched, so the
    # dialog was left standing and the logout silently did nothing.
    if ("log out?" in text or "logout?" in text
            or "are you sure you want to log out" in text
            or "sigurado ka ba" in text):
        for b, attrs in clickable_items(d):
            label = normalize_account_name(
                attrs.get("content-desc") or attrs.get("text")).lower()
            if label in ("yes", "log out", "logout", "oo",
                         "mag-log out", "mag-logout", "sign out"):
                d.click(*center(b))
                time.sleep(3)
                return True
    width, height = safe_window_size(d)
    buttons = []
    for b, _ in clickable_items(d):
        w  = b[2]-b[0]
        h  = b[3]-b[1]
        cx, cy = center(b)
        if (int(width*0.15) <= cx <= int(width*0.85) and
                int(height*0.52) <= cy <= int(height*0.62) and
                150 <= w <= 260 and 45 <= h <= 90):
            buttons.append((cx, b))
    if len(buttons) >= 2:
        d.click(*center(sorted(buttons)[0][1]))
        time.sleep(3)
        return True
    return False

def confirm_suspended(d, tries=3, gap=1.5):
    """Re-read the screen before anything irreversible.

    Facebook Lite repaints while it loads, and a single frame is not evidence.
    Every one of these has to agree.
    """
    for i in range(tries):
        try:
            if not is_suspended(d):
                return False
        except Exception:
            return False
        if i + 1 < tries and not abort_sleep(gap):
            return False
    return True


def logout_current_account(serial, d):
    # Last line of defence. Signing an account out is the one action here that
    # cannot be undone without a password, so it never happens on the strength
    # of one reading. If the re-read disagrees we keep the account signed in;
    # the caller then ends this device's account walk, which costs the rest of
    # a run and nothing permanent - the wrong way round would cost the login.
    if not confirm_suspended(d):
        print(f"    [{serial}] suspension did not hold on re-read - keeping "
              f"this account signed in")
        return False
    if not open_gear_menu(serial, d):
        return False
    found = False
    for _ in range(6):
        row = find_logout_row(d)
        if row:
            d.click(*row)
            time.sleep(2)
            found = True
            break
        d.swipe_ext("up", scale=0.6)
        time.sleep(1)
    if not found:
        return False
    confirm_logout_if_needed(d)
    time.sleep(3)
    return True

# "9+ notifications" / "9+ na notification" - the badge that says this account
# is still live. A row without one is usually stale or password-locked.
_NOTIF_RE = re.compile(r"\d+\+?\s*(?:na\s+)?notifications?", re.I)


def recover_from_login(serial, d):
    """Sign back in when the app drops to the login chooser mid-run.

    Only ever pick a row that is showing notifications. A badge-less account is
    usually stale or password-locked, and tapping one strands the phone on a
    password screen for the rest of the run - so when nothing carries a badge
    we leave the device alone rather than guess.
    """
    rows = login_screen_account_rows(d)
    if not rows:
        return False

    # Badged accounts first - a notification badge means the session is live,
    # so tapping one signs straight in.
    #
    # Facebook Lite 527 redesigned this screen and DROPPED the badges: every
    # profile now shows just a name and a chevron. Refusing every unbadged row
    # (the old rule) leaves the phone stranded on the chooser doing nothing,
    # which is what happened to C9002784 and C9002955. So when nothing is
    # badged we do try a row - but we check straight afterwards, and if it
    # asks for a password we back out and try the next one. The point of the
    # rule was never "don't tap", it was "never leave a phone sitting on a
    # password screen", and that is still honoured.
    ordered = [r for r in rows if r.get("notifs")] or rows
    unbadged = not any(r.get("notifs") for r in rows)

    for row in ordered[:4]:
        print(f"    [{serial}] login page: signing back in as {row['name']}"
              + (" (no badges on this build)" if unbadged else ""))
        try:
            d.click(*row["point"])
        except Exception:
            continue
        time.sleep(6)
        try:
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                time.sleep(2)
        except Exception:
            pass
        try:
            if is_password_prompt(d):
                print(f"    [{serial}] {row['name']} wants a password - backing "
                      f"out, not stranding the phone here")
                for _ in range(3):
                    d.press("back")
                    time.sleep(1)
                    if login_screen_account_rows(d):
                        break
                continue
        except Exception:
            pass
        if not login_screen_account_rows(d):
            return True
    return False


def login_screen_account_rows(d):
    width, height = safe_window_size(d)
    rows = []
    for b, attrs in clickable_items(d):
        raw = attrs.get("content-desc") or attrs.get("text") or ""
        desc = normalize_account_name(raw)
        if not desc:
            continue
        low = desc.lower()
        if any(skip in low for skip in ACCOUNT_SKIP_TEXT):
            continue
        w, h = b[2]-b[0], b[3]-b[1]
        if w < int(width * 0.50) or h < 60:
            continue
        rows.append({"name": desc, "bounds": b, "point": center(b),
                     "notifs": bool(_NOTIF_RE.search(raw))})
    deduped, seen = [], set()
    for row in sorted(rows, key=lambda r: (r["bounds"][1], r["bounds"][0])):
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped

def return_to_login_chooser(d):
    for _ in range(4):
        if login_screen_account_rows(d):
            return True
        try:
            d.press("back")
        except Exception:
            pass
        time.sleep(1)
    return bool(login_screen_account_rows(d))

def find_switch_account_arrow(d):
    width, height = safe_window_size(d)
    arrows = []
    for b, _ in clickable_items(d):
        x1, y1, x2, y2 = b
        w  = x2-x1
        h  = y2-y1
        cx, cy = center(b)
        if cx < int(width*0.78) or cy < int(height*0.12) or cy > int(height*0.34):
            continue
        if 35 <= w <= 90 and 35 <= h <= 90:
            arrows.append((cy, cx, b))
    if not arrows:
        return None
    arrows.sort()
    # Current Facebook Lite menus show a SINGLE chevron next to the profile row.
    # Older layouts had two icons in this band and the switcher was the second
    # one, so keep that pick when there are several — but never give up just
    # because there is only one, which used to abort account switching entirely
    # (the run then processed one account and stopped).
    return center(arrows[1][2] if len(arrows) >= 2 else arrows[0][2])

_MENU_LABELS = ("menu", "see more", "more options", "more", "settings menu")


# ── Label-less builds ────────────────────────────────────────────────────────
# Some Facebook builds expose NO text and NO content-desc anywhere in the
# accessibility tree - verified on CPH2631, where even a raw `uiautomator dump`
# returns bounds only. Reacting still works there because that was always
# geometric, but every account-switching path was label-driven, so those phones
# silently did ONE account and stopped. These helpers recover the same screens
# from shape alone, and are only consulted when the label path finds nothing.

# Once a phone has shown real account NAMES, never fall back to positional
# identities on it again. Mixing the two corrupts used_accounts: the same
# account appears as "Hazel Matias" one round and "#2" the next, so the run
# re-queues switches that actually happened and can revisit or skip accounts.
_labels_seen = set()
_labels_lock = threading.Lock()


def note_labels_seen(serial):
    if serial:
        with _labels_lock:
            _labels_seen.add(serial)


def labels_known(serial):
    if not serial:
        return False
    with _labels_lock:
        return serial in _labels_seen


def screen_is_on(serial):
    """False when the display is off. A sleeping phone still answers dumps,
    and a black screen produced two plausible-looking 'account rows' - so the
    positional fallback must never run on one."""
    if not serial:
        return True
    try:
        _, out, _ = run(["shell", "dumpsys", "display"], serial=serial, timeout=15)
    except Exception:
        return True
    return "mScreenState=OFF" not in out


def dump_has_labels(d):
    """True when the app's own text is visible in the tree.

    SystemUI (status bar, nav bar) always carries labels, so its window is
    excluded - otherwise a label-less Facebook still looks labelled.
    """
    try:
        xml = safe_dump(d)
    except Exception:
        return True                     # unknown: assume the normal path
    if not xml:
        return True
    for m in re.finditer(r'<node\b[^>]*>', xml):
        tag = m.group(0)
        if "com.android.systemui" in tag:
            continue
        tm = re.search(r'\btext="([^"]*)"', tag)
        dm = re.search(r'\bcontent-desc="([^"]*)"', tag)
        if (tm and tm.group(1).strip()) or (dm and dm.group(1).strip()):
            return True
    return False


def _uniform_stack(d, min_rows=2, min_w=0.50):
    """Largest run of same-width, same-height, vertically stacked rows.

    That shape is the account list in the switcher sheet: every row shares one
    x-range, one height, and sits flush against the next.
    """
    width, height = safe_window_size(d)
    rows = []
    for b, _ in clickable_items(d):
        w, h = b[2] - b[0], b[3] - b[1]
        if w < int(width * min_w) or h < 60 or h > 260:
            continue
        if b[1] < int(height * 0.10):
            continue
        rows.append(b)
    groups = {}
    for b in rows:
        key = (b[0] // 8, b[2] // 8, (b[3] - b[1]) // 8)
        groups.setdefault(key, []).append(b)
    if not groups:
        return []
    best = max(groups.values(), key=len)
    if len(best) < min_rows:
        return []
    best.sort(key=lambda b: b[1])
    # Rows of a real list TOUCH each other. Cards on the Menu screen are the
    # same width and height but sit apart, so without this the Menu reads as an
    # account list and the switcher reports "open" while still on the Menu.
    # The contiguous run is the answer or there is no answer - never fall back
    # to the loose group, which is what defeated this check before.
    best_run, run = [], [best[0]]
    for b in best[1:]:
        if abs(b[1] - run[-1][3]) <= max(12, (b[3] - b[1]) // 6):
            run.append(b)
        else:
            if len(run) > len(best_run):
                best_run = run
            run = [b]
    if len(run) > len(best_run):
        best_run = run
    return best_run if len(best_run) >= min_rows else []


def find_menu_button_geometric(d):
    """The hamburger, by shape: right-most small button on the top bar row."""
    width, height = safe_window_size(d)
    top = [b for b, _ in clickable_items(d)
           if b[1] < int(height * 0.12) and (b[2] - b[0]) < int(width * 0.25)]
    if not top:
        return None
    # Band by the TOPMOST row only. The tab strip directly underneath the app
    # bar is also small and also touches the right edge, so a plain "rightmost
    # near the top" picks Marketplace instead of the hamburger.
    top_y = min(b[1] for b in top)
    band = [b for b in top if b[1] - top_y <= 40]
    right = max(band, key=lambda b: b[2])
    if right[2] < int(width * 0.80):     # must actually hug the right edge
        return None
    # Never hand back a TAB STRIP icon. When the feed is scrolled the app bar
    # hides and the strip becomes the topmost row, so this function used to
    # return its right-most cell - Marketplace - with full confidence. The
    # switcher then opened Marketplace, found no accounts, and the run decided
    # every account was done after one. Returning None instead lets the caller
    # put the app bar back and ask again.
    if any(right == cell for cell in tab_strip_cells(d)):
        return None
    return center(right)


def find_switch_account_card(d):
    """The 'Switch account' row on the Menu screen, by shape.

    The Menu is a column of full-width cards followed by a two-column grid.
    The first full-width card is the profile ("View your profile"), the second
    is "Switch account" - verified against the real screen.
    """
    width, height = safe_window_size(d)
    wide = []
    for b, _ in clickable_items(d):
        w = b[2] - b[0]
        if w < int(width * 0.85):
            continue
        if b[1] < int(height * 0.06) or b[1] > int(height * 0.55):
            continue
        wide.append(b)
    if len(wide) < 2:
        return None
    wide.sort(key=lambda b: b[1])
    # drop nested duplicates: keep one card per y-band
    picked = []
    for b in wide:
        if picked and b[1] - picked[-1][1] < 40:
            continue
        picked.append(b)
    if len(picked) < 2:
        return None
    return center(picked[1])            # [0] is the profile card


def find_menu_button(d):
    """The hamburger / menu entry, by label. Falls back to the top-right
    corner tap that the old code hard-coded, which broke whenever the bar
    layout shifted."""
    width, height = safe_window_size(d)
    for b, attrs in clickable_items(d):
        desc = (attrs.get("content-desc") or attrs.get("text") or "").strip().lower()
        if not desc:
            continue
        if desc in _MENU_LABELS:
            cx, cy = center(b)
            if cy < int(height * 0.22):
                return cx, cy
    return find_menu_button_geometric(d)


def current_account_name(d):
    """Name of the account the switcher marks as current (its top row)."""
    rows = account_rows(d)
    return rows[0]["name"] if rows else None


def settled_account_rows(d, timeout=7.0):
    """Account rows, once the sheet has stopped animating.

    A sheet mid-slide renders a partial, still-unlabelled list; reading it the
    instant it becomes non-empty yields 2 nameless rows on a phone that really
    has 9 named ones. Wait for two consecutive identical reads instead.
    """
    deadline = time.time() + timeout
    prev = None
    while time.time() < deadline:
        if stop_requested():
            return []
        rows = account_rows(d)
        if rows:
            sig = [r["name"] for r in rows]
            if sig == prev:
                return rows
            prev = sig
        else:
            prev = None
        if not abort_sleep(0.35):
            return []
    return account_rows(d)


def tab_strip_cells(d):
    """The 5-6 equal cells of the top tab strip, left to right (or [])."""
    width, height = safe_window_size(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        bw = x2 - x1
        if y1 > int(height * 0.25):
            continue
        if bw < int(width * 0.10) or bw > int(width * 0.25):
            continue
        rows.setdefault(round((y1 + y2) / 2 / 26), []).append(b)
    for _, row in sorted(rows.items()):
        row = sorted(row, key=lambda b: b[0])
        if (len(row) >= 5 and row[0][0] <= int(width * 0.06)
                and row[-1][2] >= int(width * 0.94)):
            return row
    return []


def reveal_top_bar(serial, d):
    """Bring the blue app bar - the one carrying the hamburger - back on screen.

    The feed hides it as soon as it is scrolled, and then the TAB STRIP is the
    topmost row. find_menu_button_geometric() bands to the topmost row on
    purpose, so it then returns that strip's right-most icon - which is
    MARKETPLACE. Measured on C9004018 and C9032476 under FB Lite 528: the
    switcher tap landed in Marketplace, account_rows() came back empty, and the
    run concluded "all accounts done (1 processed)" with seven accounts left
    untouched. Tapping Home both leaves wherever we are and scrolls the feed
    back to the top, which puts the app bar back.
    """
    # Restart the app rather than tapping a tab. The strip is NOT in the same
    # order on every phone - C9032476 has Reels first where C9004018 has Home -
    # so "tap the first cell" lands somewhere different per device. A fresh
    # launch always opens the feed at the top with the app bar showing.
    try:
        run(["shell", "am", "force-stop", FB_PACKAGE], serial=serial)
        time.sleep(1.5)
        run(["shell", "am", "start", "-n", FB_ACTIVITY], serial=serial)
    except Exception:
        return False
    time.sleep(9.0)
    if find_menu_button(d) is not None:
        return True
    # Facebook reopens on whatever tab it was last left on, so a restart can
    # land straight back in Marketplace - no app bar, no hamburger, and the
    # switcher can never open. Measured on C9032476, which sat in Marketplace
    # across restarts while the run reported "all accounts done" after one
    # account. BACK leaves the tab and returns to the feed.
    for _ in range(3):
        try:
            d.press("back")
        except Exception:
            break
        time.sleep(2.0)
        # BACK from the feed LEAVES Facebook, and then the dump is SystemUI and
        # every detector is blind. Stop the moment we are no longer in the app,
        # and put it back where we found it.
        if not fb_foreground(serial):
            try:
                run(["shell", "am", "start", "-n", FB_ACTIVITY], serial=serial)
            except Exception:
                pass
            time.sleep(8.0)
            return find_menu_button(d) is not None
        if find_menu_button(d) is not None:
            return True
    return False


def open_account_switcher(serial, d):
    width, height = safe_window_size(d)
    for attempt in range(3):
        open_fb_home(serial, d, force=(attempt > 0))
        if not is_logged_in(d):
            # Not "the switcher would not open" - there is no session at all.
            # This phone is sitting on Facebook's account picker and needs
            # someone to log in; retrying cannot fix it.
            print(f"    [{serial}] LOGGED OUT - Facebook is showing its login "
                  f"screen. Log this phone in; the run cannot switch accounts.")
            return False
        # Short: the switcher is read immediately after this, and a switcher
        # that opens is itself proof the account is fine. A blocked account
        # shows its screen at once, so waiting 5s for it cost ~10s per switch
        # on every healthy account.
        if wait_for_account_block(d, timeout=1.0):
            return False
        if account_rows(d):
            return True
        # open the menu by label when we can; the hard-coded corner tap is only
        # a fallback because it breaks whenever the top bar shifts
        # Put the app bar back before hunting for the hamburger, or the
        # geometric finder picks Marketplace off the tab strip (see
        # reveal_top_bar).
        # Put the app bar back FIRST. Checking find_menu_button() for None
        # first would not work: with the bar hidden the geometric finder does
        # not fail, it confidently returns Marketplace's coordinates.
        reveal_top_bar(serial, d)
        menu = find_menu_button(d)
        d.click(*(menu if menu else (int(width * 0.914), int(height * 0.074))))
        poll_until(lambda: find_switch_account_arrow(d) is not None or account_rows(d), 2)
        if account_rows(d):
            return True
        arrow = find_switch_account_arrow(d)
        if arrow is None:
            # The Menu screen carries no labels on some builds, so the row
            # cannot be found by text - locate it by shape instead. (The sheet
            # it opens DOES expose names, so only getting there was broken.)
            card = find_switch_account_card(d)
            if card is not None:
                d.click(*card)
                if settled_account_rows(d, 7.0):
                    return True
            # last resort: the profile row itself opens the switcher on some builds
            for b, attrs in clickable_items(d):
                desc = (attrs.get("content-desc") or attrs.get("text") or "").lower()
                if "profile" in desc or "view your profile" in desc:
                    d.click(*center(b))
                    if poll_until(lambda: bool(account_rows(d)), 2):
                        return True
                    break
            continue
        d.click(*arrow)
        if settled_account_rows(d, 7.0):
            return True
        d.press("back")
        time.sleep(0.6)
    return False

def switcher_is_open(d):
    """True only when the account-switcher sheet is really on screen.

    Two different sheet layouts exist in the wild and BOTH must be recognised:

      * multi-account phones show a titled sheet -> "Switch accounts" (plural;
        the menu row behind it is singular "Switch account", so the plural is
        what tells them apart)
      * a phone with a single logged-in account shows a compact, UNTITLED sheet
        -> only the account row, "Go to Accounts Center" and "Dismiss"

    Matching just the title made the compact sheet look closed, so switching
    reported "Could not open account switcher" on a phone whose switcher was
    open and working.
    """
    try:
        text = visible_text(d).lower()
    except Exception:
        return False
    # The sheet title is localised: Filipino phones show "Lumipat ng account"
    # (and "I-dismiss"), which matched none of the strings below - so a sheet
    # that was open and listing 8 accounts read as closed, and the phone gave
    # up after "Could not open account switcher" with a single account done.
    if any(m in text for m in ("switch accounts", "palitan ang account",
                              "mga account", "lumipat ng account",
                              "lumipat sa account", "magpalit ng account")):
        return True
    if ("accounts center" in text or "account center" in text) \
            and "dismiss" in text:
        return True                     # untitled compact sheet
    # Label-less build: there is no title to match, so fall back to the sheet's
    # shape - a stack of identical, full-width rows filling the lower screen.
    if not dump_has_labels(d):
        return len(_uniform_stack(d, min_rows=2, min_w=0.60)) >= 2
    return False


def account_rows(d):
    """Rows of the "Switch accounts" sheet, top first (top = current account).

    Returns [] unless the sheet is actually open. Without that guard a post
    screen yields one row ("Write a comment…"), which made
    open_account_switcher() believe the switcher was already showing — so it
    never opened it, treated that placeholder as the current account, found no
    others and ended the whole run after a single account.

    Identified by SHAPE, not screen position. The old code skipped anything in
    the upper 55% of the screen, which silently dropped the first accounts in
    the list — on a 9-account phone it hid three of them INCLUDING the one that
    is currently logged in, so the caller thought the wrong account was active.

    The sheet's rows all share one x-range, so the largest group of same-width
    rows is the account list; that also excludes the menu behind the sheet.
    """
    # ONE dump for the whole function. switcher_is_open(), dump_has_labels()
    # and clickable_items() each used to fetch their own, so a single
    # account_rows() cost 3-4 dumps - and settled_account_rows() polls it.
    # Measured on a real run: 1771 dumps / 389 s for only 10 switches.
    d = d if getattr(d, "_frozen", False) else Snapshot(d)
    if not switcher_is_open(d):
        return []
    width, height = safe_window_size(d)
    cands = []
    for b, attrs in clickable_items(d):
        desc = normalize_account_name(attrs.get("content-desc") or attrs.get("text"))
        if not desc:
            continue
        low = desc.lower()
        if any(skip in low for skip in ACCOUNT_SKIP_TEXT):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if w < int(width * 0.50) or h < 60 or h > 220:
            continue
        if b[1] < int(height * 0.12):        # keep out of the top app bar only
            continue
        cands.append({"name": desc, "bounds": b, "point": center(b)})

    # group by identical x-extent: the sheet's rows are all flush to one column
    groups = {}
    for r in cands:
        key = (r["bounds"][0] // 8, r["bounds"][2] // 8)
        groups.setdefault(key, []).append(r)
    if groups:
        best = max(groups.values(), key=len)
        if len(best) >= 2:                    # a real list, not a stray row
            cands = best

    deduped, seen = [], set()
    for row in sorted(cands, key=lambda r: (r["bounds"][1], r["bounds"][0])):
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    if deduped:
        note_labels_seen(getattr(d, "serial", None))
        return deduped
    # Nothing named, and deliberately no positional "#1/#2" fallback. The sheet
    # ALWAYS exposes names on every phone in this fleet - it is the Menu screen
    # behind it that is label-less, and that is solved geometrically by
    # find_switch_account_card(). An index-based identity was tried and only
    # ever fired on half-rendered sheets, black screens and the Menu's own
    # cards, silently mixing "#2" with real names in used_accounts - the same
    # corruption that makes a run reuse one account. Returning nothing makes
    # the caller poll again, which is what we actually want.
    return []

def count_available_accounts(serial, d):
    try:
        if not open_account_switcher(serial, d):
            return -1
        return len(account_rows(d))
    except Exception:
        return -1

def _verify_switched(serial, d, want, previous):
    """Confirm the logged-in account actually changed to `want`.

    Re-opens the switcher and reads the row it marks as current. Returns True
    when that is `want` (or at least no longer `previous`), so a tap that was
    swallowed mid-animation is caught instead of silently reusing one account.
    """
    for attempt in range(2):
        if stop_requested():
            return False
        if not open_account_switcher(serial, d):
            continue
        cur = current_account_name(d)
        try:
            d.press("back")               # leave the sheet as we found it
        except Exception:
            pass
        abort_sleep(0.6)
        if not cur:
            continue
        if cur.lower() == (want or "").lower():
            return True
        if previous and cur.lower() != previous.lower():
            print(f"    [{serial}] now on '{cur}' (wanted '{want}')")
            return True                   # moved off the old account: accept
        if attempt == 0 and not abort_sleep(1.0):
            return False
    return False


# "immediate" re-opens the switcher after every switch purely to read one name
# (~30s each). "lazy" checks the SAME thing for free on the next switch, which
# has to open the switcher anyway. Only the final switch of a run goes
# unchecked, and that costs at most one repeated account.
VERIFY_SWITCH = "lazy"


def switch_to_next_account(serial, d, used_accounts, state=None):
    """Switch to the next logged-in account, with retries and verification."""
    if stop_requested():
        return False, None, "stopped"
    state = state if state is not None else {}
    for retry in range(3):
        if stop_requested():
            return False, None, "stopped"
        if not open_account_switcher(serial, d):
            print(f"    [{serial}] Could not open account switcher (attempt {retry+1})")
            if not abort_sleep(2):
                return False, None, "stopped"
            continue
        rows = account_rows(d)
        if not rows:
            print(f"    [{serial}] No account rows found (attempt {retry+1})")
            # Try scrolling a bit
            for _ in range(3):
                if stop_requested():
                    return False, None, "stopped"
                d.swipe_ext("up", scale=0.3)
                if not abort_sleep(0.5):
                    return False, None, "stopped"
            rows = account_rows(d)
            if not rows:
                if not abort_sleep(2):
                    return False, None, "stopped"
                continue
        # The freshly-opened switcher shows the CURRENT account at the top.
        current = rows[0]["name"]

        # Free verification of the PREVIOUS switch: if the account we selected
        # last time is not the current one, that tap was swallowed. Put it back
        # in the pool so it still gets its turn.
        pending = state.pop("pending", None)
        if pending and pending.lower() != current.lower():
            # Re-queue it ONCE only. Unbounded re-queueing removes names from
            # used_accounts as fast as they are added, so the pool never
            # empties and the device loops forever - a 7-account phone was seen
            # reaching "account 154" under parallel load, where swallowed taps
            # are common.
            requeued = state.setdefault("requeued", {})
            key = pending.lower()
            if requeued.get(key, 0) < 1:
                requeued[key] = requeued.get(key, 0) + 1
                used_accounts.discard(key)
                print(f"    [{serial}] previous switch to {pending} did not take "
                      f"(now on {current}) - re-queueing it once")
            else:
                print(f"    [{serial}] switch to {pending} failed twice - "
                      f"giving up on that account")

        used_accounts.add(current.lower())

        # The switcher is a scrollable list and account_rows() only reports the
        # rows currently on screen. With 10-20 accounts the rest sit below the
        # fold, so walk the list downwards until an unused account shows up or
        # the list stops producing new names. Without this the run stopped after
        # the handful of accounts that happened to be visible.
        seen_names = set()
        rescan = False
        for _ in range(20):
            if stop_requested():
                return False, None, "stopped"
            rows = account_rows(d)
            picked = next((r for r in rows
                           if r["name"].lower() not in used_accounts), None)

            if picked is None:
                # Nothing unused on screen — scroll further down the switcher.
                names = {r["name"].lower() for r in rows}
                if names and names.issubset(seen_names):
                    break                       # end of the list
                seen_names |= names
                try:
                    d.swipe_ext("up", scale=0.35)
                except Exception:
                    break
                if not abort_sleep(0.6):
                    return False, None, "stopped"
                continue

            name = picked["name"]
            print(f"  [{serial}] switching account -> {name}")
            d.click(*picked["point"])
            if not abort_sleep(3):
                return False, None, "stopped"
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                if not abort_sleep(1):
                    return False, None, "stopped"
            # 3s shorter now that a bad verdict must be confirmed twice: the
            # block screen shows up as soon as the account loads, and a healthy
            # account used to burn the whole 6s on every single switch.
            block_status = wait_for_account_block(d, timeout=3)
            if block_status:
                print(f"  [{serial}] {name}: {block_status}")
                used_accounts.add(name.lower())
                if block_status == "suspended" and logout_current_account(serial, d):
                    rescan = True           # logged out — rescan from the top
                    break
                return False, None, block_status

            # Verify the switch REALLY happened. The tap can be swallowed while
            # the sheet animates; assuming success made the run treat several
            # "accounts" as the same logged-in user.
            if VERIFY_SWITCH == "immediate":
                if not _verify_switched(serial, d, name, current):
                    print(f"  [{serial}] switch to {name} did not take - retrying")
                    if not abort_sleep(1.5):
                        return False, None, "stopped"
                    rescan = True
                    break
            else:
                state["pending"] = name      # checked free on the next switch

            used_accounts.add(name.lower())
            return True, name, "switched"

        if rescan:
            continue
        print(f"    [{serial}] No unused accounts found, refreshing...")
        d.press("back")
        if not abort_sleep(1):
            return False, None, "stopped"
        if open_account_switcher(serial, d):
            rows2 = account_rows(d)
            if rows2 and len(rows2) > len(rows):
                continue
        return False, None, "no-available-account"
    return False, None, "no-available-account"

def find_reel_comment_point(d):
    stack = _reel_action_stack(d)
    return stack[1] if stack and len(stack) >= 2 else None

def find_feed_action_row(d):
    width, height = safe_window_size(d)
    rows = {}
    for b in clickable_bounds(d):
        x1, y1, x2, y2 = b
        w = x2-x1
        h = y2-y1
        if h < 40 or h > 150 or y1 < int(height * 0.18):
            continue
        if w < int(width * 0.20) or w > int(width * 0.55):
            continue
        rows.setdefault(y1 // 12, []).append(b)
    best = None
    for _, row in rows.items():
        row = sorted(row, key=lambda b: b[0])
        if len(row) < 2:
            continue
        same_band = max(b[1] for b in row) - min(b[1] for b in row) <= 24
        covers    = row[0][0] <= int(width*0.12) and row[-1][2] >= int(width*0.88)
        if same_band and covers and 2 <= len(row) <= 4:
            if best is None or row[0][1] > best[0][1]:
                best = row
    return best

def find_feed_comment_point(d):
    row = find_feed_action_row(d)
    if not row:
        return None
    if len(row) >= 2:
        return center(row[1])
    return None

def edit_nodes(d):
    root = hierarchy_root(d)
    if root is None:
        return []
    nodes = []
    for node in root.iter("node"):
        cls  = node.attrib.get("class", "")
        desc = (node.attrib.get("content-desc") or node.attrib.get("text") or "").lower()
        is_edit    = ("EditText" in cls or "AutoCompleteTextView" in cls or
                      node.attrib.get("editable") == "true")
        # Locale-tolerant: the composer reads "Sumulat ng comment..." on the
        # Filipino phones, which the three English strings never matched.
        is_comment = post_view_text_match(desc)
        if is_edit or is_comment:
            b = parse_bounds(node.attrib.get("bounds"))
            if b:
                nodes.append((b, node.attrib))
    return nodes

def wait_for_edit_box(d, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        edits = edit_nodes(d)
        if edits:
            edits.sort(key=lambda e: -e[0][1])
            try:
                d.click(*center(edits[0][0]))
                time.sleep(0.5)
            except Exception:
                pass
            return center(edits[0][0])
        time.sleep(0.5)
    return None

def try_open_comment_from_screen(serial, d):
    edits = edit_nodes(d)
    if edits:
        edits.sort(key=lambda e: -e[0][1])
        return center(edits[0][0])
    for b, attrs in clickable_items(d):
        label = (attrs.get("content-desc") or attrs.get("text") or "").lower()
        if "comment" in label or "write a" in label:
            d.click(*center(b))
            time.sleep(2)
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                time.sleep(2)
            box = wait_for_edit_box(d)
            if box is not None:
                return box
    for finder in (find_feed_comment_point, find_reel_comment_point):
        pt = finder(d)
        if pt is not None:
            d.click(*pt)
            time.sleep(2)
            if chooser_present(serial, d):
                dismiss_chooser(serial, d)
                time.sleep(2)
            box = wait_for_edit_box(d)
            if box is not None:
                return box
    return None

def find_enter_key(d):
    root = hierarchy_root(d)
    if root is None: return None
    width, height = safe_window_size(d)
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or "").lower()
        text = (node.attrib.get("text") or "").lower()
        if desc in ("enter", "send", "search", "done", "go", "next") or \
           text in ("enter", "send", "search", "done", "go", "next"):
            b = node.attrib.get("bounds", "")
            try:
                parts = b.replace("][", ",").replace("[","").replace("]","").split(",")
                if len(parts) == 4:
                    x1, y1, x2, y2 = map(int, parts)
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    if cy > height * 0.5:
                        return (cx, cy)
            except Exception:
                pass
    return None

def open_comment_composer(serial, d, max_scrolls=None):
    """Scroll to the comment box.

    Carries the same two long-caption fixes as _scroll_to_post_like:
      * bail only on positive evidence of the FEED. Testing "can I see a
        comment marker" meant a long caption - which hides those markers by
        definition - looked like drift and stopped the search at once.
      * no fixed 10-swipe ceiling (~4 screens). The budget now runs until the
        page stops moving, so a caption of any length is crossed.
    """
    width, height = safe_window_size(d)
    if stop_requested():
        return None
    box = try_open_comment_from_screen(serial, d)
    if box is not None:
        return box
    budget = SCROLL_BUDGET_MAX if max_scrolls is None else max_scrolls
    last_sig = None
    stalls = 0
    off_feed = 0
    for i in range(budget):
        if stop_requested():
            return None
        snap = Snapshot(d)
        if on_feed_view(snap) and not on_post_view(snap):
            off_feed += 1
            if off_feed >= 2:
                print("    [comment] drifted onto the news feed - stopping")
                return None
        else:
            off_feed = 0
        sig = _screen_signature(snap)
        if sig is not None and sig == last_sig:
            stalls += 1
            if stalls >= SCROLL_STALL_LIMIT:
                print("    [comment] reached the end of the post without a "
                      "comment box - stopping")
                return None
        else:
            stalls = 0
        last_sig = sig
        try:
            # never put the finger down on a caption link - see _swipe_path()
            x1, y1, x2, y2 = _swipe_path(TouchMap(snap), 0.72, 0.32, i)
            d.swipe(x1, y1, x2, y2, _drag_ms(y1, y2) / 1000.0)
        except Exception:
            pass
        if not abort_sleep(1.2):
            return None
        box = try_open_comment_from_screen(serial, d)
        if box is not None:
            print(f"    [comment] found composer after {i + 1} scroll(s)")
            return box
    return None

def find_send_button(d):
    width, height = safe_window_size(d)
    box_y = None
    try:
        root = hierarchy_root(d)
        if root is not None:
            import re
            for node in root.iter("node"):
                cls = node.attrib.get("class", "")
                if "MultiAutoCompleteTextView" in cls or "EditText" in cls:
                    b = node.attrib.get("bounds", "")
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', b)
                    if m:
                        box_y = (int(m.group(2)) + int(m.group(4))) // 2
                        break
    except Exception:
        pass

    candidates = []
    for b, attrs in clickable_items(d):
        x1, y1, x2, y2 = b
        w  = x2-x1
        h  = y2-y1
        cx, cy = center(b)
        label = (attrs.get("content-desc") or attrs.get("text") or "").lower()
        if x1 < int(width * 0.85) or x2 > width:
            continue
        if not (55 <= w <= 90 and 55 <= h <= 90):
            continue
        if y1 < int(height * 0.40):
            continue
        if box_y is not None and abs(cy - box_y) > 160:
            continue
        if cy > height - 100:
            continue
        if any(k in label for k in ("voice", "delete", "enter", "space", "search",
                                     "done", "next", "go", "previous")):
            continue
        if "back" in label or "home" in label or "overview" in label:
            continue
        if any(k in label for k in ("send", "post", "submit", "publish")):
            candidates.append((9, (cx, cy)))
            continue
        candidates.append((1 + cx/width, (cx, cy)))
    
    if not candidates:
        try:
            send_btn = d(className="android.widget.Button", 
                        textContains="Send") | d(className="android.widget.Button",
                        textContains="Post")
            if send_btn.exists:
                bounds = send_btn.info()["bounds"]
                cx = (bounds["left"] + bounds["right"]) // 2
                cy = (bounds["top"] + bounds["bottom"]) // 2
                return (cx, cy)
        except Exception:
            pass
        return None
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]

def type_text(serial, d, point, text):
    if stop_requested():
        return False
    d.click(*point)
    if not abort_sleep(1.2):
        return False
    try:
        run(["shell", "input", "keyevent", "KEYCODE_CTRL_A"], serial=serial)
        abort_sleep(0.2)
        run(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=serial)
        run(["shell", "input", "keyevent", "KEYCODE_MOVE_END"], serial=serial)
        for _ in range(40):
            if stop_requested():
                return False
            run(["shell", "input", "keyevent", "KEYCODE_DEL"], serial=serial)
        abort_sleep(0.2)
    except Exception:
        pass
    safe_text = (text
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace('"', '\\"')
                .replace("&", "\\&")
                .replace("<", "\\<")
                .replace(">", "\\>")
                .replace("|", "\\|")
                .replace(";", "\\;")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .replace("$", "\\$")
                .replace("`", "\\`")
                .replace("*", "\\*")
                .replace("?", "\\?")
                .replace("[", "\\[")
                .replace("]", "\\]")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("~", "\\~")
                .replace(" ", "%s"))
    typed = text.strip()[:15].lower()
    ok = False
    try:
        run(["shell", "input", "text", safe_text], serial=serial)
        time.sleep(0.8)
        ok = typed in visible_text(d).lower()
    except Exception as e:
        print(f"    [type] ADB text input failed: {e}")
    if not ok:
        try:
            el = d(className="android.widget.MultiAutoCompleteTextView")
            if not el.exists:
                el = d(className="android.widget.EditText")
            el.set_text(text)
            time.sleep(0.8)
            ok = typed in visible_text(d).lower()
        except Exception as e2:
            print(f"    [type] uiautomator2 fallback failed: {e2}")
    return ok

def post_comment(serial, d, text):
    if not text:
        return "comment-empty"
    if stop_requested():
        return "stopped"
    # Same target lock as react(): never comment on whatever happens to be on
    # screen — only on the post we were told to comment on.
    if not on_post_view(d, timeout=5.0):
        if stop_requested():
            return "stopped"
        print("    [comment] not on the target post - refusing to comment here")
        return "comment-not-on-target-post"
    box = open_comment_composer(serial, d)
    if box is None:
        return "stopped" if stop_requested() else "comment-no-box"

    try:
        type_text(serial, d, box, text)
    except Exception as e:
        return f"comment-type-failed:{e}"
    if not abort_sleep(1):
        return "stopped"

    typed = text.strip()[:15].lower()
    text_cleared = False
    old_ime = None

    try:
        current_ime = d.current_ime()
        if "adbkeyboard" in current_ime.lower() or "uiautomator" in current_ime.lower():
            gboard = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
            run(["shell", "settings", "put", "secure", "default_input_method", gboard],
                serial=serial)
            time.sleep(1)
            box = open_comment_composer(serial, d)
            if box:
                type_text(serial, d, box, text)
                time.sleep(1)
    except Exception:
        pass

    if typed not in visible_text(d).lower():
        retry_box = open_comment_composer(serial, d)
        if retry_box:
            try:
                type_text(serial, d, retry_box, text)
                time.sleep(1)
            except Exception:
                pass
    if typed not in visible_text(d).lower():
        return "comment-not-typed"

    # Abort between typing and sending must leave the comment UNSENT.
    if stop_requested():
        return "stopped"

    try:
        enter = find_enter_key(d)
        if enter is not None:
            run(["shell", "input", "tap", str(enter[0]), str(enter[1])], serial=serial)
            time.sleep(2)
            if typed not in visible_text(d).lower():
                text_cleared = True
    except Exception:
        pass

    if not text_cleared and stop_requested():
        return "stopped"

    if not text_cleared:
        try:
            send = find_send_button(d)
            if send is not None:
                run(["shell", "input", "tap", str(send[0]), str(send[1])], serial=serial)
                time.sleep(2)
                if typed not in visible_text(d).lower():
                    text_cleared = True
        except Exception:
            pass

    if not text_cleared and stop_requested():
        return "stopped"

    if not text_cleared:
        try:
            old_ime = d.current_ime()
            d.set_input_ime()
            time.sleep(0.3)
            run(["shell", "am", "broadcast", "-a", "send.mock",
                 "--ei", "type", "2", "--ei", "code", "4"], serial=serial)
            time.sleep(2)
            if typed not in visible_text(d).lower():
                text_cleared = True
        except Exception:
            pass

    if not text_cleared and stop_requested():
        return "stopped"

    if not text_cleared:
        try:
            width, height = safe_window_size(d)
            send_x = int(width * 0.90)
            send_y = int(height * 0.582)
            run(["shell", "input", "tap", str(send_x), str(send_y)], serial=serial)
            time.sleep(2)
            if typed not in visible_text(d).lower():
                text_cleared = True
            elif typed in visible_text(d).lower():
                run(["shell", "input", "tap", str(send_x), str(send_y)], serial=serial)
                time.sleep(1)
                run(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=serial)
                time.sleep(1)
                if typed not in visible_text(d).lower():
                    text_cleared = True
        except Exception:
            pass

    if old_ime:
        try:
            run(["shell", "settings", "put", "secure", "default_input_method", old_ime],
                serial=serial)
        except Exception:
            pass

    if text_cleared:
        return f"commented:{text}"
    return "comment-send-failed"

def print_unready_help(serial, state):
    if state == "unauthorized":
        print(f"  [!] {serial} UNAUTHORIZED - on the phone: keep it unlocked, "
              "Developer Options -> Revoke USB debugging authorizations, "
              "toggle USB debugging OFF/ON, replug, tap Allow (Always allow).")
    else:
        print(f"  [!] {serial} {state.upper()} - not reachable. "
              "Use a data USB cable, set USB mode to File transfer, "
              "enable USB debugging, try another port/driver.")

# ── Updated react_all_saved_accounts (calls screenshot with d) ──
class RunResults(list):
    """The usual list of status strings, plus a per-link tally.

    Subclassing list keeps every existing caller working unchanged while
    carrying `link_tally` = {link_no: {"reacted": n, "commented": n,
    "failed": n, "url": str}} so the UI can report "link 1 -> 169 reacts"
    instead of trying to parse it back out of the status strings.
    """

    def __init__(self, *a):
        super().__init__(*a)
        self.link_tally = {}

    def bump(self, link_no, url, key):
        row = self.link_tally.setdefault(
            link_no, {"reacted": 0, "commented": 0, "failed": 0, "url": url})
        row["url"] = url
        row[key] = row.get(key, 0) + 1


def _tally(results, link_no, url, key):
    if isinstance(results, RunResults):
        results.bump(link_no, url, key)


def _process_target(serial, d, tgt, account_num, link_no, n_links,
                    take_screenshots, results):
    """React and/or comment on ONE link with the CURRENT account.

    The post is already on screen. Returns one of:
      "ok"      - done with this link
      "abort"   - Abort was pressed, unwind everything
      "blocked" - Facebook is rate-limiting this account; its remaining links
                  are pointless, move on to the next account
    Screenshots are tagged per link so each account/link pair is its own file.
    """
    url = tgt["url"]
    set_current_post_url(url)            # this link, this thread
    reaction = tgt.get("reaction", "like")
    comment_example = tgt.get("comment", "")
    do_react = tgt.get("do_react", True)
    do_comment = tgt.get("do_comment", False)
    tag = f"link{link_no}_" if n_links > 1 else ""
    where = f"account {account_num} link {link_no}/{n_links}"

    # ── reaction ──
    react_ok = False
    if do_react:
        last_fail, repeats = None, 0
        for attempt in range(10):
            if stop_requested():
                results.append("stopped")
                return "abort"
            try:
                react_status = react(d, reaction)
            except Exception as e:
                # A detector bug must cost this LINK, never the whole phone.
                # Left unhandled, an exception here unwinds through
                # _react_all_saved_accounts up to gui.py's device-level catch,
                # and the run silently loses every remaining account and link
                # on this device - which is what "it just skips the task"
                # looked like from outside.
                print(f"    [{serial}] {where}: react crashed "
                      f"({type(e).__name__}: {e}) - failing this link only")
                react_status = f"react-failed:crash:{type(e).__name__}"
            if react_status == "stopped" or stop_requested():
                results.append("stopped")
                return "abort"
            # Rate limited: no amount of retrying helps, and every retry costs
            # ~20 swipes. Stop this account immediately.
            if react_status == "action-blocked":
                results.append("action-blocked")
                print(f"    [{serial}] {where}: ACTION BLOCKED by Facebook "
                      f"- skipping this account")
                dump_debug(serial, d, "action-blocked")
                return "blocked"
            results.append(react_status)
            print(f"    [{serial}] {where}: {react_status}")
            # Identical failure three times running means the screen is not
            # what we think it is; keep the evidence and stop wasting swipes.
            if not react_status.startswith("reacted"):
                repeats = repeats + 1 if react_status == last_fail else 1
                last_fail = react_status
                if repeats >= 3:
                    print(f"    [{serial}] {where}: '{react_status}' 3x in a row "
                          f"- giving up on this link")
                    dump_debug(serial, d, f"react_stuck_{react_status[:24]}")
                    break
            if react_status.startswith("reacted"):
                react_ok = True
                _tally(results, link_no, url, "reacted")
                # when we also comment, hold the shot so ONE screenshot shows
                # both the reaction and the comment
                if take_screenshots and not do_comment:
                    take_screenshot(serial, account_num, f"{tag}react", d)
                # a human does not react and start typing in the same instant
                if not pace(PACE_ACTION):
                    results.append("stopped")
                    return "abort"
                break
            print(f"    [{serial}] reaction failed, retrying...")
            if not abort_sleep(1):
                results.append("stopped")
                return "abort"
            wake_and_unlock(serial)
            for _ in range(5):
                if stop_requested():
                    results.append("stopped")
                    return "abort"
                try:
                    # fresh: a warm reopen returns to the scroll position the
                    # failed attempt left behind - often past the Like row -
                    # so every retry would fail exactly the same way
                    if open_target_post(serial, d, url, fresh=True):
                        break
                except Exception:
                    pass
                print(f"    [{serial}] open post failed during react retry, retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return "abort"
            try:
                dismiss_chooser(serial, d)
            except Exception:
                pass
            poll_until(lambda: on_post_view(d), 6)
        else:
            print(f"    [{serial}] reaction failed after 10 attempts")
            results.append("react-failed")
            _tally(results, link_no, url, "failed")

    # ── comment ──
    if do_comment:
        text = humanize_typo(unique_comment(comment_example))
        print(f"    [{serial}] {where} comment > {text}")
        for _ in range(2):
            if stop_requested():
                results.append("stopped")
                return "abort"
            if is_action_blocked(d):
                results.append("action-blocked")
                print(f"    [{serial}] {where}: ACTION BLOCKED - not commenting")
                dump_debug(serial, d, "action-blocked-comment")
                return "blocked"
            comment_status = post_comment(serial, d, text)
            if comment_status == "stopped" or stop_requested():
                results.append("stopped")
                return "abort"
            results.append(comment_status)
            print(f"    [{serial}] {where}: {comment_status}")
            if comment_status.startswith("commented"):
                _tally(results, link_no, url, "commented")
                if not pace(PACE_ACTION):
                    results.append("stopped")
                    return "abort"
                if take_screenshots:
                    # drop the keyboard so the reaction AND the new comment
                    # both land in the same screenshot
                    prepare_post_view_for_shot(serial, d)
                    action = f"{tag}react_comment" if do_react else f"{tag}comment"
                    take_screenshot(serial, account_num, action, d)
                break
            print(f"    [{serial}] comment failed, retrying...")
            if not abort_sleep(1):
                results.append("stopped")
                return "abort"
            wake_and_unlock(serial)
            for _ in range(3):
                if stop_requested():
                    results.append("stopped")
                    return "abort"
                try:
                    if open_target_post(serial, d, url):
                        break
                except Exception:
                    pass
                print(f"    [{serial}] open post failed during comment retry, retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return "abort"
            try:
                dismiss_chooser(serial, d)
            except Exception:
                pass
            poll_until(lambda: on_post_view(d), 6)
        else:
            print(f"    [{serial}] comment could not be submitted after 2 attempts")
            results.append("comment-failed")
            _tally(results, link_no, url, "failed")
            # comment failed but the reaction landed — still capture proof
            if take_screenshots and react_ok:
                prepare_post_view_for_shot(serial, d)
                take_screenshot(serial, account_num, f"{tag}react", d)
    return "ok"


def make_target(url, reaction="like", comment="", do_react=True, do_comment=False):
    """One link and what to do on it."""
    return {"url": normalize_url(url), "reaction": reaction or "like",
            "comment": comment or "", "do_react": bool(do_react),
            "do_comment": bool(do_comment)}


def normalize_targets_for_phones(targets):
    """Resolve every target once before device workers receive it.

    A /share/v/ token has no post id of its own. Resolve it to Facebook's
    canonical permalink before the fleet fans out, so FBPoison and FBVenom use
    the same target and do not make every phone rediscover the same redirect.
    If Facebook cannot expose a canonical destination, the cleaned share URL
    remains in the list and the phone-side opener fallbacks still get a chance.
    """
    items = [dict(t) for t in (targets or [])]
    if not items:
        return []
    if len(items) > 1:
        pool = min(len(items), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=pool) as ex:
            resolved = list(ex.map(
                lambda it: normalize_phone_url(it.get("url", "")), items))
    else:
        resolved = [normalize_phone_url(items[0].get("url", ""))]
    out = []
    for item, after in zip(items, resolved):
        before = item.get("url", "")
        if not after:
            print(f"  [link] rejected non-Facebook or invalid URL: {before!r}")
            continue
        item["url"] = after
        if after != before:
            print(f"  [link] normalized for phones -> {after[:100]}")
        out.append(item)
    return out


def dedupe_targets(targets, serial=None):
    """Collapse repeated links, comparing by NORMALISED url.

    Pasted lists repeat constantly - a real one came in as 22 links that were
    only 10 unique, every entry twice and one of them four times. Reacting
    again is harmless (picking "like" from the tray is idempotent), but it is
    pure cost: on that list more than half of every account's runtime bought
    nothing. Comparing normalised urls also collapses the same post written
    with and without a trailing slash or with tracking parameters attached.
    """
    seen, uniq, dropped = set(), [], 0
    for t in targets:
        raw = t.get("url", "")
        try:
            key = normalize_url(raw) or raw
        except Exception:
            key = raw
        # The URL itself is already canonicalized for the phone, but trim the
        # key as an extra guard for targets supplied by older callers.
        key = key.rstrip("/")
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        uniq.append(t)
    if dropped:
        who = f"[{serial}] " if serial else ""
        print(f"    {who}{dropped} duplicate link(s) collapsed - "
              f"{len(uniq)} unique of {len(targets)}")
    return uniq


def react_all_saved_accounts(serial, url_or_targets, reaction="like",
                             comment_example="", do_react=True, do_comment=False,
                             take_screenshots=False):
    """Public entry point: guarantees Facebook Lite is closed on Abort,
    whichever path the inner loop exits through.

    `url_or_targets` is either a single URL (legacy call) or a list of target
    dicts from make_target() — the mass-links mode. Each saved account handles
    EVERY link before the run switches to the next account, because switching
    accounts is by far the slowest and most failure-prone step: doing it once
    per account instead of once per account-per-link cuts the switch count by
    the number of links.
    """
    if isinstance(url_or_targets, (list, tuple)):
        targets = [dict(t) for t in url_or_targets]
    else:
        targets = [make_target(url_or_targets, reaction, comment_example,
                               do_react, do_comment)]
    # GUI callers do this before fan-out; keep the core entry point safe for
    # direct/CLI callers as well.
    targets = normalize_targets_for_phones(targets)
    targets = [t for t in targets if t.get("url")]
    targets = dedupe_targets(targets, serial)
    if not targets:
        return ["invalid-url"]
    try:
        results = _react_all_saved_accounts(serial, targets, take_screenshots)
    except Exception:
        if stop_requested():
            close_fb(serial)
        raise
    # Close Facebook Lite on the way out either way: on Abort, and just the same
    # once every account has been processed — the run is finished, so the app
    # should not be left sitting open on the phone.
    if stop_requested():
        print(f"    [{serial}] aborted - closing Facebook Lite.")
    elif any(str(r) == "all-accounts-done" for r in (results or [])):
        print(f"    [{serial}] all accounts done - closing Facebook Lite.")
    else:
        print(f"    [{serial}] finished - closing Facebook Lite.")
    close_fb(serial)
    return results

def _react_all_saved_accounts(serial, targets, take_screenshots=False):
    url = targets[0]["url"]              # the first link gates the account check
    set_current_post_url(url)
    n_links = len(targets)
    d = connect_resilient(serial)
    if d is None:
        if stop_requested():
            return ["stopped"]
        if device_state(serial) == "unauthorized":
            return ["unauthorized"]
        return ["u2-connect-failed"]

    wake_and_unlock(serial)
    run(["shell", "svc", "power", "stayon", "true", "usb"], serial=serial)

    results        = RunResults()
    used_accounts  = set()
    switch_state   = {}          # carries the lazy switch check
    account_num    = 1
    transient_errors = 0
    # Hard ceiling. No phone has anywhere near this many logins, so if the
    # account loop ever stops terminating on its own this stops the run instead
    # of grinding for hours. It has happened: a re-queue path that could fire
    # repeatedly kept a 7-account phone going past "account 154".
    MAX_ACCOUNTS = 60
    # Each try is a full open_target_post (3 attempts, cold restarts included),
    # so this is minutes of honest effort before the phone is written off.
    OPEN_POST_TRIES = 6

    while True:
        if account_num > MAX_ACCOUNTS:
            print(f"[{serial}] SAFETY STOP: passed {MAX_ACCOUNTS} accounts on a "
                  f"phone that cannot have that many - ending this device.")
            results.append("safety-stop-too-many-accounts")
            break
        if stop_requested():
            results.append("stopped")
            print(f"[{serial}] stop requested - halting before account {account_num}.")
            break
        d = ensure_device(serial, d)
        if stop_requested():
            results.append("stopped")
            break

        # Bounded. This was `while True`, so a phone that could never reach
        # the post - one unknown locale string in the detectors was enough -
        # spun here forever printing "retrying in 5s", never reacted, and kept
        # the whole run from ending, because these device threads are joined.
        opened = False
        blocked = None
        for open_try in range(1, OPEN_POST_TRIES + 1):
            if stop_requested():
                results.append("stopped")
                return results
            try:
                wake_and_unlock(serial)
                print(f"[{serial}] opening post for account {account_num}: {url}")
                # Verified open: lands on the post itself, not the feed.
                if open_target_post(serial, d, url):
                    opened = True
                    break
                if wait_for_account_block(d, timeout=0):
                    opened = True
                    break          # login/suspension screen — handled below
            except Exception:
                pass
            if stop_requested():
                results.append("stopped")
                return results
            # Ask once why the open failed. A checkpoint, a logged-out session
            # or a suspended account will answer the same way to all six
            # attempts, so there is no point spending four minutes finding out.
            try:
                why = blocked_reason(d)
            except Exception:
                why = None
            if why:
                print(f"    [!] {serial} {why} - this account cannot open the "
                      f"link, moving on instead of retrying")
                blocked = why
                break
            print(f"    [{serial}] open post failed, retrying in 5s "
                  f"({open_try}/{OPEN_POST_TRIES})...")
            if not abort_sleep(5):
                results.append("stopped")
                return results
        if not opened:
            if blocked:
                # Hand it to the account handling below, which knows how to
                # log out of a dead account and move to the next one.
                results.append(blocked)
                status = "suspended" if blocked == "suspended" else "not-successful"
            else:
                print(f"    [!] {serial} could not reach the post after "
                      f"{OPEN_POST_TRIES} tries - ending this device.")
                results.append("open-post-failed")
                break
        else:
            status = wait_for_account_block(d)

        if status == "not-successful":
            results.append(status)
            print(f"    [!] {serial} {status}")
            for _ in range(10):
                switched, _, switch_status = switch_to_next_account(
                    serial, d, used_accounts, switch_state)
                if switched:
                    transient_errors = 0
                    account_num += 1
                    break
                print(f"    [{serial}] switch failed ({switch_status}), retrying...")
                time.sleep(3)
            else:
                results.append(switch_status)
                print(f"    [{serial}] {switch_status} - no more accounts")
                break
            continue
        if status == "suspended":
            results.append(status)
            print(f"    [!]{serial} {status}")
            logged_out    = logout_current_account(serial, d)
            logout_status = ("suspended-logged-out" if logged_out
                             else "suspended-logout-failed")
            results.append(logout_status)
            print(f"    [{serial}] {logout_status}")
            if logged_out:
                rows     = login_screen_account_rows(d)
                switched = False
                switch_status = "no-available-account"
                for row in rows:
                    name = row["name"]
                    if name.lower() in used_accounts:
                        continue
                    print(f"    [{serial}] logging into > {name}")
                    d.click(*row["point"])
                    time.sleep(5)
                    if chooser_present(serial, d):
                        dismiss_chooser(serial, d)
                        time.sleep(2)
                    block_status = wait_for_account_block(d, timeout=8)
                    if block_status:
                        results.append(f"{block_status}:{name}")
                        print(f"    [{serial}] {name}: {block_status}")
                        used_accounts.add(name.lower())
                        if block_status == "suspended":
                            logged_out = logout_current_account(serial, d)
                            if not logged_out:
                                switch_status = "suspended-logout-failed"
                                break
                            continue
                        switch_status = block_status
                        continue
                    used_accounts.add(name.lower())
                    switched      = True
                    switch_status = "switched"
                    break
                if switched:
                    transient_errors = 0
                    account_num += 1
                    continue
                else:
                    results.append(switch_status)
                    print(f"    [{serial}] {switch_status}")
                    break
            else:
                break
        else:
            # ── This account works through EVERY link, one by one ──
            for link_no, tgt in enumerate(targets, start=1):
                if stop_requested():
                    results.append("stopped")
                    return results
                if link_no > 1:
                    # first link is already open (it gated the account check)
                    # Set the url BEFORE opening it. _process_target sets it
                    # too, but that runs AFTER the open, so the open used to
                    # run with the previous link still current - which made
                    # _is_reel_context() answer for the wrong link at exactly
                    # the moment a reel was being opened.
                    set_current_post_url(tgt["url"])
                    opened = False
                    for _ in range(5):
                        if stop_requested():
                            results.append("stopped")
                            return results
                        try:
                            if open_target_post(serial, d, tgt["url"]):
                                opened = True
                                break
                        except Exception:
                            pass
                        print(f"    [{serial}] link {link_no}: open failed, retrying...")
                        if not abort_sleep(3):
                            results.append("stopped")
                            return results
                    if not opened:
                        results.append(f"link{link_no}-open-failed")
                        _tally(results, link_no, tgt["url"], "failed")
                        print(f"    [{serial}] link {link_no}: could not open - skipping")
                        continue
                outcome = _process_target(serial, d, tgt, account_num, link_no,
                                          n_links, take_screenshots, results)
                if outcome == "abort":
                    return results
                if outcome == "blocked":
                    # rate-limited: its other links would fail the same way
                    print(f"    [{serial}] account {account_num} is action-blocked "
                          f"- skipping its remaining links")
                    break
                if link_no < n_links and not pace(PACE_LINK, "before the next link"):
                    results.append("stopped")
                    return results

            # ── Switch to next account ──
            # Breathe before hopping accounts; back-to-back switching is the
            # fastest way to get every account on the phone throttled.
            if not pace(PACE_ACCOUNT, "before switching account"):
                results.append("stopped")
                return results
            switched = False
            for _ in range(3):
                if stop_requested():
                    results.append("stopped")
                    return results
                switched, _, switch_status = switch_to_next_account(
                    serial, d, used_accounts, switch_state)
                if switched:
                    break
                # "no more accounts" is a final answer, not a transient glitch —
                # retrying it just burns ~9s at the end of every device run.
                if switch_status in ("no-available-account", "stopped"):
                    break
                print(f"    [{serial}] switch failed ({switch_status}), retrying...")
                if not abort_sleep(3):
                    results.append("stopped")
                    return results
            if not switched:
                # Every logged-in account has now been handled on this post.
                # That is a successful finish, not a failure.
                if switch_status == "no-available-account":
                    results.append("all-accounts-done")
                    print(f"    [{serial}] all accounts done "
                          f"({account_num} processed) - finishing up.")
                else:
                    results.append(switch_status)
                    print(f"    [{serial}] {switch_status} - no more accounts")
                break
            transient_errors = 0
            account_num += 1
    return results

def cli_main():
    # Original CLI mode (called from main.py if --cli)
    url = normalize_url(input("Post URL to react to: "))
    if not url:
        print("No URL given, aborting.")
        return
    mode = input("Action - (r)eact, (c)omment, or (b)oth / react+comment? [r]: ")
    do_react, do_comment = parse_action_mode(mode)
    default = ""
    if do_react:
        default = input(
            "Default reaction for all (like/love/care/haha/wow/sad/angry), "
            "or blank to choose per phone: ").strip().lower()
        if default and default not in REACTIONS:
            print(f"  ignoring invalid default '{default}', will ask per phone")
            default = ""
    comment_example = ""
    if do_comment:
        comment_example = input(
            "Paste an example comment (each account posts a unique variation): ").strip()
        if not comment_example:
            print("No example comment given, disabling comments.")
            do_comment = False
    if not do_react and not do_comment:
        print("Nothing to do, aborting.")
        return
    if do_comment and not start_llama():
        print("Could not reach Pollinations AI, disabling comments.")
        do_comment = False
        if not do_react:
            return
    take_screenshots = input("Take screenshots after each action? (y/n): ").strip().lower() == 'y'
    devices = ensure_online()
    if not devices:
        print("No phones detected. Check cables, USB mode (File transfer), and USB debugging.")
        return
    ready = [s for s, st in devices.items() if st == "device"]
    apk   = find_apk()
    results = {}
    for serial, st in devices.items():
        if st != "device":
            print_unready_help(serial, st)
            results[serial] = st
    reset_comment_pool()
    device_reactions = {}
    for serial in ready:
        if do_react:
            device_reactions[serial] = default if default else ask_reaction(f"Reaction for {serial}: ")
        else:
            device_reactions[serial] = ""
    for serial in ready:
        wake_and_unlock(serial)
    results_lock = threading.Lock()
    def run_device(serial):
        try:
            res = react_all_saved_accounts(
                serial, url, device_reactions.get(serial, ""),
                comment_example, do_react, do_comment, take_screenshots)
            with results_lock:
                results[serial] = res
        except Exception as e:
            with results_lock:
                results[serial] = f"thread-error:{e}"
    threads = []
    for serial in ready:
        if not has_fb(serial):
            if apk and install_fb(serial, apk):
                pass
            elif apk:
                results[serial] = "install-failed"
                continue
            else:
                results[serial] = "missing-app-no-apk"
                print(f"  [!] {serial} Facebook Lite not installed and no APK found in {HERE}")
                continue
        t = threading.Thread(target=run_device, args=(serial,), daemon=True)
        t.start()
        threads.append((serial, t))
    for serial, t in threads:
        t.join()
    stop_llama()
    print("Summary:")
    reacted  = 0
    commented = 0
    for serial, status in results.items():
        statuses  = status if isinstance(status, list) else [status]
        reacted  += sum(1 for s in statuses if str(s).startswith("reacted:"))
        commented += sum(1 for s in statuses if str(s).startswith("commented:"))
        print(f"  {serial}: {', '.join(map(str, statuses))}")
    print(f"{reacted} reaction(s), {commented} comment(s) across {len(devices)} phone(s).")
