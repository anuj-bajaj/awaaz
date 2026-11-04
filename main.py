import os, json, base64, sqlite3, asyncio, re, time, smtplib, ssl, hmac, csv, io
import urllib.request, urllib.parse
from email.message import EmailMessage
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import edge_tts
from groq import Groq
from google import genai
from google.genai import types
from prompt import build_system_prompt, DEPARTMENTS

load_dotenv()
groq = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=20, max_retries=1)
gem = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
DB = "tickets.db"
VOICES = {"en": "en-IN-NeerjaNeural", "hinglish": "en-IN-NeerjaNeural", "hi": "hi-IN-SwaraNeural"}
URGENCY_RANK = ["low", "medium", "high", "critical"]
LANG_NAME = {"en": "ENGLISH", "hinglish": "HINGLISH (Hindi in Roman letters)", "hi": "HINDI (Devanagari script)"}

app = FastAPI()
SESSIONS: dict[str, list[str]] = {}     # session_id -> conversation lines
SESSION_LANG: dict[str, str] = {}       # session_id -> language of the pending follow-up
SESSION_KIND: dict[str, str] = {}       # session_id -> "ask_followup" (complaint) or "status_ask" (ticket number)
STATUS_WORDS = re.compile(r"status|ticket|kya hua|kya hoga|update|progress|kab tak|स्टेटस|टिकट|क्या हुआ", re.I)


# ---------------------------------------------------------------- time: store UTC, think in IST
IST = ZoneInfo("Asia/Kolkata")


def utc_now() -> str:
    """Timestamp saved in the database. The trailing Z tells the browser it is UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_ist() -> date:
    """'Today' for the students, whatever timezone the server runs in."""
    return datetime.now(IST).date()


def as_utc(s):
    """Rows written by older versions have no timezone marker (they were saved on a UTC server). Mark them as UTC."""
    if s and not (s.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", s)):
        return s + "Z"
    return s


# ---------------------------------------------------------------- database
# DATABASE_URL set  -> PostgreSQL (Neon, Supabase, ...): data survives restarts and redeploys.
# DATABASE_URL empty -> local SQLite file (fine on a laptop, wiped on Render's free plan).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PG = DATABASE_URL.startswith("postgres")
DB = "tickets.db"

if PG:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    POOL = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=True, timeout=20,
                          kwargs={"row_factory": dict_row, "prepare_threshold": None},  # None: works behind pgbouncer
                          check=ConnectionPool.check_connection)                       # drops connections the host closed


class _Cur:
    def __init__(self, cur, lastrowid=None):
        self.cur, self.lastrowid = cur, lastrowid

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()


class _Conn:
    """One tiny wrapper so the rest of the code is the same for SQLite and PostgreSQL:
    '?' placeholders, rows readable by column name, cursor.lastrowid after an INSERT."""

    def __enter__(self):
        if PG:
            self._cm = POOL.connection()
            self.raw = self._cm.__enter__()
        else:
            self.raw = sqlite3.connect(DB)
            self.raw.row_factory = sqlite3.Row
        return self

    def __exit__(self, et, ev, tb):
        if PG:
            return self._cm.__exit__(et, ev, tb)       # commits, or rolls back on error, then returns it to the pool
        try:
            self.raw.commit() if et is None else self.raw.rollback()
        finally:
            self.raw.close()
        return False

    def execute(self, sql, params=()):
        if not PG:
            cur = self.raw.execute(sql, params)
            return _Cur(cur, cur.lastrowid)
        sql = sql.replace("?", "%s")
        is_insert = sql.lstrip().upper().startswith("INSERT")
        if is_insert and "RETURNING" not in sql.upper():
            sql += " RETURNING id"
        cur = self.raw.execute(sql, params or None)
        return _Cur(cur, cur.fetchone()["id"] if is_insert else None)


def db():
    return _Conn()


COLUMNS = """department TEXT, category TEXT, summary TEXT, location TEXT,
            urgency TEXT, deadline TEXT, confidential INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open', report_count INTEGER DEFAULT 1,
            created_at TEXT, updated_at TEXT"""


def init_db():
    with db() as c:
        if PG:
            c.execute(f"CREATE TABLE IF NOT EXISTS tickets (id SERIAL PRIMARY KEY, {COLUMNS}, emailed INTEGER DEFAULT 0)")
        else:
            c.execute(f"CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, {COLUMNS})")
            try:                          # add the column to an existing tickets.db
                c.execute("ALTER TABLE tickets ADD COLUMN emailed INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass


def wipe_tickets():
    """Used only by the Reset button. Ticket numbers start again from 1."""
    with db() as c:
        if PG:
            c.execute("TRUNCATE TABLE tickets RESTART IDENTITY")
        else:
            c.execute("DELETE FROM tickets")
            c.execute("DELETE FROM sqlite_sequence WHERE name='tickets'")


init_db()
print("Database:", "PostgreSQL (persistent)" if PG else "SQLite file (wiped when a free Render instance restarts)")


# ---------------------------------------------------------------- real action: email the department
# Optional. Set SMTP_USER, SMTP_PASS (Gmail app password) and NOTIFY_TO in .env to enable.
# Without them nothing is sent and the dashboard shows no "emailed" tag.
def send_email(ticket_id: int, dep: str, urgency: str, summary: str, location, deadline, confidential: bool):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("NOTIFY_TO")
    if not (user and pw and to):
        return
    try:
        msg = EmailMessage()
        msg["From"], msg["To"] = user, to
        msg["Subject"] = f"[Awaaz] {'CONFIDENTIAL ' if confidential else ''}Ticket #{ticket_id} - {dep} ({urgency.upper()})"
        msg.set_content(
            f"New ticket raised by voice through Awaaz.\n\nTicket: #{ticket_id}\nDepartment: {dep}\n"
            f"Urgency: {urgency}\nLocation: {location or 'not given'}\nDeadline: {deadline or 'none'}\n\n"
            f"Issue: {summary}\n")
        pw = re.sub(r"\s+", "", pw)       # Google shows app passwords with spaces; strip them
        ctx = ssl.create_default_context()
        try:                              # port 465 first, then 587 (some networks block one of them)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=10) as s:
                s.login(user, pw)
                s.send_message(msg)
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            print("Port 465 failed, trying 587:", repr(e)[:80])
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
        with db() as c:
            c.execute("UPDATE tickets SET emailed=1 WHERE id=?", (ticket_id,))
        print(f"Email sent for ticket {ticket_id}")
    except Exception as e:
        print("Email failed:", repr(e)[:120])


# ---------------------------------------------------------------- real action: Telegram message to the department
# Optional. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Uses HTTPS (port 443), so it works on Render's free plan
# where SMTP ports are blocked. Confidential cases never put their details into the chat.
def send_telegram(ticket_id: int, dep: str, urgency: str, summary: str, location, deadline, confidential: bool):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return
    try:
        if confidential:
            text = f"CONFIDENTIAL ticket #{ticket_id} for {dep} ({urgency.upper()}). Details withheld, see the dashboard."
        else:
            text = (f"New Awaaz ticket #{ticket_id}\nDepartment: {dep}\nUrgency: {urgency.upper()}\n"
                    f"Location: {location or 'not given'}\nDeadline: {deadline or 'none'}\n\nIssue: {summary}")
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= r.status < 300
        if ok:
            with db() as c:
                c.execute("UPDATE tickets SET emailed=1 WHERE id=?", (ticket_id,))   # "notified" tag on the dashboard
            print(f"Telegram sent for ticket {ticket_id}")
    except Exception as e:
        print("Telegram failed:", type(e).__name__, str(e)[:80].replace(token, "***"))


# ---------------------------------------------------------------- language detection + reply templates
SCRIPT = re.compile(r"[\u0900-\u097F\u0600-\u06FF]")   # Devanagari or Arabic/Urdu script
LATIN = re.compile(r"[A-Za-z]")
HINGLISH_MARKERS = {
    "hai", "hain", "nahi", "nahin", "nhi", "mein", "ka", "ki", "ke", "ko", "se", "kal", "aaj",
    "bahut", "kharab", "toot", "gaya", "gayi", "gaye", "raha", "rahi", "rahe", "hua", "hui",
    "kya", "kaise", "kyun", "mera", "meri", "mere", "humara", "hamara", "koi", "kuch", "abhi",
    "bhi", "chahiye", "wala", "wali", "aur", "lekin", "tha", "thi", "hoga", "hogi", "dikkat",
}


def detect_lang(text: str, locked: str | None = None) -> str:
    if SCRIPT.search(text):
        return "hinglish" if LATIN.search(text) else "hi"   # mixed script = code-mixed Hinglish
    words = re.findall(r"[a-z]+", text.lower())
    if any(w in HINGLISH_MARKERS for w in words):
        return "hinglish"
    if locked and len(words) <= 6:      # short answer to our own follow-up keeps its language
        return locked
    return "en"


T = {
    "create": {
        "en": "Done. Ticket {id} raised for {department}, {urgency} priority.",
        "hinglish": "Ticket {id} {department} ke liye bana diya gaya hai, {urgency} priority.",
        "hi": "टिकट {id} {department} के लिए बना दिया गया है, प्राथमिकता {urgency}।",
    },
    "create_conf": {
        "en": "I'm sorry this happened. Ticket {id} has been raised privately with {department}.",
        "hinglish": "Mujhe afsos hai. Ticket {id} {department} ko private tareeke se bhej diya gaya hai.",
        "hi": "मुझे खेद है। टिकट {id} निजी तौर पर {department} को भेज दिया गया है।",
    },
    "merge": {
        "en": "Added to ticket {id}. It now has {count} reports, {urgency} priority.",
        "hinglish": "Ticket {id} mein jod diya gaya hai. Ab {count} reports hain, {urgency} priority.",
        "hi": "टिकट {id} में जोड़ दिया गया है। अब {count} रिपोर्ट हैं, प्राथमिकता {urgency}।",
    },
    "status": {
        "en": "Ticket {id} with {department} is currently {status}.",
        "hinglish": "Ticket {id} abhi {status} hai, {department} ke paas.",
        "hi": "टिकट {id} अभी {status} है, {department} विभाग में।",
    },
    "notfound": {
        "en": "I couldn't find that ticket number.",
        "hinglish": "Mujhe woh ticket number nahi mila.",
        "hi": "मुझे वह टिकट नंबर नहीं मिला।",
    },
    "ask_location": {
        "en": "I'm sorry to hear that. Which hostel or place did this happen?",
        "hinglish": "Yeh sunkar afsos hua. Yeh kis hostel ya jagah par hua?",
        "hi": "यह सुनकर दुख हुआ। यह किस हॉस्टल या जगह पर हुआ?",
    },
    "fallback": {
        "en": "Sorry, could you say that again?",
        "hinglish": "Maaf kijiye, kya aap dobara bol sakte hain?",
        "hi": "माफ़ कीजिए, क्या आप दोबारा बोल सकते हैं?",
    },
}


def fmt(kind: str, lang: str, **vals) -> str:
    return T[kind][lang].format(**vals)


def sp(dep) -> str:
    return (dep or "").replace("/", " ")      # "IT/WiFi" is spoken as "IT WiFi"


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def rank(u: str) -> int:
    return URGENCY_RANK.index(u) if u in URGENCY_RANK else 1


# ---------------------------------------------------------------- clean what the LLM returns
DEPT_KEYS = {re.sub(r"[^a-z]", "", d.lower()): d for d in DEPARTMENTS}
DEPT_ALIAS = {"it": "IT/WiFi", "wifi": "IT/WiFi", "internet": "IT/WiFi", "mess": "Mess/Canteen",
              "canteen": "Mess/Canteen", "food": "Mess/Canteen", "health": "Health/Medical",
              "medical": "Health/Medical", "safety": "Safety/Admin", "admin": "Safety/Admin",
              "event": "Events/Clubs", "events": "Events/Clubs", "club": "Events/Clubs", "clubs": "Events/Clubs"}


def norm_department(d):
    """The LLM sometimes writes 'IT WiFi' or 'hostel maintenance'. Map it to one of our ten names
    so duplicate detection (which compares departments exactly) keeps working."""
    if d is None:
        return None
    key = re.sub(r"[^a-z]", "", str(d).lower())
    if key in ("", "null", "none"):
        return None
    if key in DEPT_KEYS:
        return DEPT_KEYS[key]
    if key in DEPT_ALIAS:
        return DEPT_ALIAS[key]
    for k, v in DEPT_KEYS.items():
        if k in key or (len(key) >= 4 and key in k):
            return v
    for w in re.findall(r"[a-z]+", str(d).lower()):      # "canteen food" -> Mess/Canteen
        if w in DEPT_ALIAS:
            return DEPT_ALIAS[w]
    return "Other"


def clean_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s


def clean_date(v):
    s = clean_text(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- duplicate detection (done by code, not by the LLM)
GENERIC_PLACE = {"hostel", "block", "room", "court", "the", "in", "at", "of", "main", "campus"}
STOP = {"the", "and", "for", "not", "demo", "working", "issue", "problem", "with", "from", "this", "that"}


def _tokens(s) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def same_place(a, b) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    core = small - GENERIC_PLACE
    return (small <= big) if core else (small == big)    # "hostel 3" matches "hostel 3 room 5", not "hostel 33"


def similar_issue(cat_a, sum_a, cat_b, sum_b) -> bool:
    def toks(*parts):
        return {w for p in parts for w in re.findall(r"[a-z]{3,}", (p or "").lower())} - STOP
    if toks(cat_a) & toks(cat_b):
        return True
    return len(toks(cat_a, sum_a) & toks(cat_b, sum_b)) >= 2


def find_duplicate(c, plan):
    if not plan["department"] or not plan["location"]:
        return None
    rows = c.execute("SELECT * FROM tickets WHERE status != 'resolved' AND department = ? "
                     "AND COALESCE(confidential, 0) = ? ORDER BY id DESC",
                     (plan["department"], int(bool(plan["confidential"])))).fetchall()
    for r in rows:
        if same_place(r["location"], plan["location"]) and \
                similar_issue(r["category"], r["summary"], plan["category"], plan["summary"]):
            return r
    return None


def valid_merge_target(row, plan) -> bool:
    if row is None or row["status"] == "resolved":
        return False
    if plan["department"] and row["department"] != plan["department"]:
        return False
    if bool(row["confidential"]) != bool(plan["confidential"]):
        return False                      # a private case is never merged into a public ticket
    if plan["location"] and not same_place(row["location"], plan["location"]):
        return False
    return True


# ---------------------------------------------------------------- speech to text
ARABIC = re.compile(r"[\u0600-\u06FF]")
PROMPT_EN = "Hostel 5 ka wifi band hai. Mess ka khana kharab hai. Library mein seat nahi hai. Court 2 ka net toot gaya hai. Bus late aa rahi hai. Awaaz Campus Voice Desk."
PROMPT_HI = "हॉस्टल 5 का wifi बंद है। मेस का खाना खराब है। लाइब्रेरी में सीट नहीं है। Court 2 का net टूट गया है। Awaaz Campus Voice Desk."


def _whisper(audio: bytes, filename: str, language: str | None = None) -> str:
    kwargs = dict(file=(filename, audio), model="whisper-large-v3", temperature=0,
                  prompt=PROMPT_HI if language == "hi" else PROMPT_EN,
                  response_format="text")
    if language:
        kwargs["language"] = language
    r = groq.audio.transcriptions.create(**kwargs)
    return (r if isinstance(r, str) else r.text).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


STOCK_HALLUCINATION = re.compile(
    r"thanks for watching|please subscribe|like and subscribe|subtitles by|amara org|www ")


def is_stock_hallucination(text: str) -> bool:
    """Phrases Whisper invents on silence or room noise. Only dropped when the clip is short."""
    t = _norm(text)
    return len(t.split()) <= 10 and bool(STOCK_HALLUCINATION.search(t))


SENTINEL = "Awaaz Campus Voice Desk"   # last words of both Whisper prompts: what it parrots on silence


def is_prompt_echo(text: str) -> bool:
    """On silence or noise, Whisper sometimes parrots its own prompt. Never turn that into a ticket."""
    t = _norm(text)
    if not t:                      # nothing but punctuation. Short real answers (yes, no, ok, 2) are kept
        return True
    sn = _norm(SENTINEL)
    if t in sn or (sn in t and len(t.split()) <= 12):
        return True
    for p in (PROMPT_EN, PROMPT_HI):
        n = _norm(p)
        # echo = the whole prompt (or most of it), never a single sentence like the real demo line
        if n in t or (t in n and len(t) >= 0.6 * len(n)):
            return True
    return False


def transcribe(audio: bytes, filename: str) -> str:
    if len(audio) < 4000:                # a click or an accidental tap, not speech
        return ""
    text = _whisper(audio, filename)
    if ARABIC.search(text):              # Hinglish misdetected as Urdu: redo as Hindi
        print("Urdu script detected, retrying as Hindi")
        text = _whisper(audio, filename, "hi")
    if is_prompt_echo(text) or is_stock_hallucination(text):
        print("Whisper returned its own prompt or nothing, ignoring:", text[:60])
        return ""
    return text


# ---------------------------------------------------------------- LLM (auto-discovered models)
def discover_models():
    skip = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "omni", "preview")
    gem_models, groq_models = [], []
    try:
        for m in gem.models.list():
            name = m.name.replace("models/", "")
            if "generateContent" in (m.supported_actions or []) and "flash" in name \
                    and not any(k in name for k in skip):
                gem_models.append(name)
    except Exception as e:
        print("Could not list Gemini models:", str(e)[:150])
    try:
        for m in groq.models.list().data:
            if not any(k in m.id for k in ("whisper", "guard", "tts", "playai", "orpheus", "safeguard", "compound")):
                groq_models.append(m.id)
    except Exception as e:
        print("Could not list Groq models:", str(e)[:150])
    gem_models = sorted(set(gem_models), reverse=True)
    groq_models.sort(key=lambda i: not any(k in i for k in ("70b", "120b", "versatile")))
    return gem_models, groq_models


PREFERRED_GROQ = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"]


def pick_groq(found: list[str]) -> list[str]:
    """Deterministic choice: GROQ_MODEL from the environment first, then our preferred models
    (when the account has them), then whatever else was discovered. Two are kept so a rate limit
    on the first falls to the second before Gemini is tried."""
    pinned = os.getenv("GROQ_MODEL", "").strip()
    pool = found or PREFERRED_GROQ
    order = ([pinned] if pinned else []) + [m for m in PREFERRED_GROQ if m in pool]
    order += [m for m in pool if m not in order]
    return order[:2]


_g, _q = discover_models()
GEMINI_MODELS = _g[:3]
GROQ_LLMS = pick_groq(_q)
print("Groq models in use:", GROQ_LLMS)
print("Gemini models in use:", GEMINI_MODELS)

COOLDOWN: dict[str, float] = {}   # model -> skip it until this timestamp
TEXT_ACTIONS = ("ask_followup", "other", "status_query")
DEFAULTS = {"action": "other", "reply_text": "", "department": None, "category": None, "summary": None,
            "location": None, "urgency": "medium", "deadline": None, "confidential": False,
            "merge_into_id": None, "ticket_id": None}


def _ask_groq(model, system, prompt):
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    if "gpt-oss" in model:
        kwargs["extra_body"] = {"reasoning_effort": "low"}
    r = groq.chat.completions.create(**kwargs)
    return json.loads(r.choices[0].message.content)


def _ask_gemini(model, system, prompt):
    resp = gem.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return json.loads(resp.text)


def wrong_script(plan: dict, lang: str) -> bool:
    r = plan.get("reply_text") or ""
    if not r:
        return False
    has_script = bool(SCRIPT.search(r))
    return has_script if lang in ("en", "hinglish") else not has_script


def think(history: list[str], open_tickets: list[dict], lang: str) -> dict:
    system = build_system_prompt(open_tickets, today_ist().isoformat())
    base = "\n".join(history) + f"\n[REQUIRED REPLY LANGUAGE: {LANG_NAME[lang]}]"

    order = [("groq", m) for m in GROQ_LLMS] + [("gemini", m) for m in GEMINI_MODELS]

    for kind, model in order:
        if COOLDOWN.get(model, 0) > time.time():
            continue
        try:
            note = ""
            for attempt in range(2):
                plan = (_ask_groq if kind == "groq" else _ask_gemini)(model, system, base + note)
                for k, v in DEFAULTS.items():
                    if plan.get(k) is None:
                        plan[k] = v
                plan["department"] = norm_department(plan["department"])
                plan["location"] = clean_text(plan["location"])
                plan["deadline"] = clean_date(plan["deadline"])
                plan["urgency"] = str(plan["urgency"]).lower()
                if plan["urgency"] not in URGENCY_RANK:
                    plan["urgency"] = "medium"
                if plan["action"] in TEXT_ACTIONS and wrong_script(plan, lang):
                    note = f"\n[Your reply_text used the wrong language or script. Rewrite it strictly as {LANG_NAME[lang]}.]"
                    continue
                break
            print("Answered by:", model)
            return plan
        except Exception as e:
            msg = str(e)
            print(f"{model} failed: {msg[:120]}")
            if "429" in msg or "503" in msg:
                COOLDOWN[model] = time.time() + 300   # skip this model for 5 minutes
            elif "decommissioned" in msg or "404" in msg or "does not exist" in msg:
                COOLDOWN[model] = time.time() + 3600  # retired or unavailable model: stop trying it for an hour
    raise RuntimeError("All LLMs failed")


# ---------------------------------------------------------------- text to speech
async def speak(text: str, lang: str) -> str:
    try:
        async def _run():
            comm = edge_tts.Communicate(text, VOICES.get(lang, VOICES["en"]))
            audio = b""
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            return audio
        audio = await asyncio.wait_for(_run(), timeout=6)
        return base64.b64encode(audio).decode() if audio else ""
    except Exception as e:
        print("TTS failed:", repr(e)[:120])
        return ""


# ---------------------------------------------------------------- core logic
class TextIn(BaseModel):
    text: str
    session_id: str


class TTSIn(BaseModel):
    text: str
    lang: str = "en"


async def process_text(text: str, session_id: str) -> dict:
    history = SESSIONS.setdefault(session_id, [])
    history.append(f"User: {text}")
    lang = detect_lang(text, SESSION_LANG.get(session_id))
    pending = SESSION_KIND.get(session_id)

    with db() as c:
        rows = c.execute("""SELECT id, department, category, summary, location, status FROM tickets
                            WHERE status != 'resolved'
                            ORDER BY id DESC LIMIT 30""").fetchall()
    open_tickets = [dict(r) for r in rows]

    try:
        t0 = time.time()
        plan = await asyncio.to_thread(think, history, open_tickets, lang)
        print(f"LLM took {time.time() - t0:.1f}s | language: {lang}")
    except Exception as e:
        print("LLM error:", e)
        SESSIONS.pop(session_id, None)
        SESSION_LANG.pop(session_id, None)
        SESSION_KIND.pop(session_id, None)
        return {"transcript": text, "action": "none", "lang": lang, "reply": T["fallback"][lang]}

    action = plan["action"]
    reply = (plan.get("reply_text") or "").strip()
    # The model sometimes answers a follow-up reply with an empty reply_text (e.g. a hedged "number 12 maybe").
    # That used to fall through to the generic "say that again". Give it one nudge first.
    if action in TEXT_ACTIONS and not reply and not (action == "status_query" and to_int(plan.get("ticket_id"))):
        print("Empty reply_text from LLM, retrying once. Plan was:", {k: plan.get(k) for k in ("action", "department", "location")})
        nudge = ("[Your last answer had an empty reply_text. If the user's latest message answers your question "
                 "and the complaint is now clear enough, use create_ticket. Otherwise ask one short question.]")
        try:
            plan = await asyncio.to_thread(think, history + [nudge], open_tickets, lang)
            action, reply = plan["action"], (plan.get("reply_text") or "").strip()
        except Exception as e:
            print("Retry failed:", e)
    if pending == "ask_followup" and action == "status_query" and not STATUS_WORDS.search(text):
        print("Status query during a complaint follow-up, retrying once")
        nudge2 = ("[The user is ANSWERING your follow-up question about their complaint. They are not asking for "
                  "a ticket status. Use create_ticket (or merge_ticket), or ask one more short question.]")
        try:
            plan = await asyncio.to_thread(think, history + [nudge2], open_tickets, lang)
            action, reply = plan["action"], (plan.get("reply_text") or "").strip()
        except Exception as e:
            print("Retry failed:", e)
    now = utc_now()
    asked = sum(1 for h in history if h.startswith("Assistant:"))

    # safety reports need a location: ask (at most twice) before creating the ticket
    if action == "create_ticket" and plan["confidential"] and not plan["location"] and asked < 2:
        action, reply = "ask_followup", T["ask_location"][lang]

    with db() as c:
        if action in ("create_ticket", "merge_ticket"):
            target = None
            mid = to_int(plan.get("merge_into_id")) if action == "merge_ticket" else None
            if mid:
                r = c.execute("SELECT * FROM tickets WHERE id=?", (mid,)).fetchone()
                if valid_merge_target(r, plan):
                    target = r
            if target is None:
                target = find_duplicate(c, plan)       # the server catches duplicates the LLM missed

            if target is not None:
                count = target["report_count"] + 1
                urgency = target["urgency"]
                if count >= 5 and rank(urgency) < rank("high"):
                    urgency = "high"
                if rank(plan["urgency"]) > rank(urgency):
                    urgency = plan["urgency"]
                deadline = target["deadline"] or plan["deadline"]
                c.execute("UPDATE tickets SET report_count=?, urgency=?, deadline=?, updated_at=? WHERE id=?",
                          (count, urgency, deadline, now, target["id"]))
                reply = fmt("merge", lang, id=target["id"], count=count, urgency=urgency)
                action = "merge_ticket"
            else:
                cur = c.execute("""INSERT INTO tickets (department, category, summary, location, urgency,
                                   deadline, confidential, created_at, updated_at)
                                   VALUES (?,?,?,?,?,?,?,?,?)""",
                                (plan["department"] or "Other", plan["category"] or "general",
                                 plan["summary"] or text, plan["location"], plan["urgency"],
                                 plan["deadline"], int(plan["confidential"]), now, now))
                new_id = cur.lastrowid
                for notify in (send_email, send_telegram):  # fire and forget: never delays the reply
                    asyncio.create_task(asyncio.to_thread(
                        notify, new_id, plan["department"] or "Other", plan["urgency"],
                        plan["summary"] or text, plan["location"], plan["deadline"], bool(plan["confidential"])))
                kind = "create_conf" if plan["confidential"] else "create"
                reply = fmt(kind, lang, id=cur.lastrowid, department=sp(plan["department"] or "Other"),
                            urgency=plan["urgency"])
                action = "create_ticket"

        elif action == "status_query" and to_int(plan.get("ticket_id")):
            row = c.execute("SELECT * FROM tickets WHERE id=?", (to_int(plan["ticket_id"]),)).fetchone()
            reply = (fmt("status", lang, id=row["id"], department=sp(row["department"]), status=row["status"])
                     if row else T["notfound"][lang])

    if not reply:
        print("Using generic fallback reply. action:", action, "| plan:", {k: plan.get(k) for k in ("department", "location", "reply_text")})
        reply = T["fallback"][lang]

    # keep the conversation only while the assistant is waiting for an answer
    if action == "ask_followup" or (action == "status_query" and not to_int(plan.get("ticket_id"))):
        history.append(f"Assistant: {reply}")
        del history[:-8]
        SESSION_LANG[session_id] = lang
        SESSION_KIND[session_id] = "ask_followup" if action == "ask_followup" else "status_ask"
    else:
        SESSIONS.pop(session_id, None)
        SESSION_LANG.pop(session_id, None)
        SESSION_KIND.pop(session_id, None)

    return {"transcript": text, "action": action, "reply": reply, "lang": lang}


# ---------------------------------------------------------------- endpoints
@app.post("/api/voice")
async def voice(audio: UploadFile = File(...), session_id: str = Form(...)):
    data = await audio.read()
    try:
        text = await asyncio.to_thread(transcribe, data, audio.filename or "speech.webm")
    except Exception as e:
        print("Whisper error:", e)
        text = ""
    if not text:
        msg = "Sorry, I didn't catch that. Please try again."
        return {"transcript": "", "action": "none", "reply": msg, "lang": "en",
                "audio_b64": await speak(msg, "en")}
    return await process_text(text, session_id)


@app.post("/api/text")
async def text_in(body: TextIn):
    return await process_text(body.text, body.session_id)


@app.post("/api/tts")
async def tts(body: TTSIn):
    t0 = time.time()
    audio = await speak(body.text, body.lang)
    print(f"TTS took {time.time() - t0:.1f}s, ok={bool(audio)}")
    return {"audio_b64": audio}


@app.get("/api/tickets")
def tickets():
    with db() as c:
        rows = c.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        t = dict(r)
        t["created_at"], t["updated_at"] = as_utc(t["created_at"]), as_utc(t["updated_at"])
        if t["confidential"]:   # never show details on the public dashboard
            demo = (t["category"] or "").startswith("[demo]")
            t.update(summary="Confidential case", location=None,
                     category="[demo] Confidential" if demo else "Confidential")
        out.append(t)
    return out


def _safe_cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s       # stops spreadsheet formula injection


@app.get("/api/export.csv")
def export_csv():
    """Download every ticket as a CSV. Reuses tickets(), so confidential details stay masked."""
    cols = ["id", "department", "category", "summary", "location", "urgency", "deadline",
            "status", "report_count", "created_at", "updated_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for t in tickets():
        w.writerow([_safe_cell(t.get(k)) for k in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=awaaz_tickets.csv"})


@app.post("/api/tickets/{tid}/resolve")
def resolve(tid: int):
    with db() as c:
        c.execute("UPDATE tickets SET status='resolved', updated_at=? WHERE id=?",
                  (utc_now(), tid))
    return {"ok": True}


@app.post("/api/seed")
def seed():
    now = utc_now()
    tomorrow = (today_ist() + timedelta(days=1)).isoformat()
    rows = [
        ("IT/WiFi", "[demo] wifi outage", "Wifi down in Hostel 3 for three days", "Hostel 3", "high", None, 0, 5),
        ("Sports", "[demo] equipment", "Badminton court 2 net is broken", "Court 2", "high", tomorrow, 0, 1),
        ("Mess/Canteen", "[demo] hygiene", "Dinner served cold with insects in the dal", "Main mess", "medium", None, 0, 2),
        ("Library", "[demo] seating", "No seats available during exam week", "Central library", "low", None, 0, 1),
        ("Transport", "[demo] timing", "Route 4 bus running 20 minutes late daily", "Route 4", "medium", None, 0, 3),
        ("Safety/Admin", "[demo] confidential", "Confidential", None, "high", None, 1, 1),
    ]
    with db() as c:
        if c.execute("SELECT 1 FROM tickets WHERE category LIKE ?", ("[demo]%",)).fetchone():
            return {"ok": True, "skipped": "demo tickets already seeded"}
        for d, cat, s, loc, u, dl, conf, n in rows:
            c.execute("""INSERT INTO tickets (department, category, summary, location, urgency, deadline,
                         confidential, report_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (d, cat, s, loc, u, dl, conf, n, now, now))
    return {"ok": True}


class ResetIn(BaseModel):
    pin: str = ""


@app.post("/api/reset")
def reset(body: ResetIn):
    """Deletes every ticket. If RESET_PIN is set in the environment, the right PIN is required."""
    need = os.getenv("RESET_PIN", "")
    if need and not hmac.compare_digest(body.pin.encode(), need.encode()):
        raise HTTPException(status_code=403, detail="Wrong PIN")
    wipe_tickets()
    print("All tickets deleted by the Reset button")
    return {"ok": True}


@app.get("/healthz")
def healthz():
    """Cheap check for an uptime pinger. It does not touch the database or any LLM."""
    return {"ok": True, "db": "postgres" if PG else "sqlite", "groq": GROQ_LLMS}


os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")