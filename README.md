# Awaaz, the Campus Voice Desk

**Speak it. We'll raise it.**

Awaaz is a voice-first grievance desk for a college campus. A student speaks a complaint in English, Hindi or Hinglish. Awaaz understands it, asks a spoken follow-up if something essential is missing, raises a ticket with the right department (or merges it into an existing ticket), emails the department, and answers back by voice. A live dashboard shows every ticket with urgency colours.

Built solo during the Voice AI Hackathon (Vocal Labs, 6 hours).

- **Live demo:** _add your live URL here_
- **Repository:** _add your GitHub URL here_

---

## What it does

1. **Voice in.** The student presses the mic (or Space) and speaks.
2. **Speech to text.** Groq Whisper large-v3, prompted with example Hinglish sentences.
3. **Understanding.** An LLM works out the department, location, urgency and deadline, and decides whether to ask a follow-up, create a ticket, merge into an existing one, or answer a status question.
4. **Real action.** A ticket is written to the database and appears live on the dashboard. When email is configured, the department is emailed.
5. **Voice out.** The reply is spoken back in the student's language.

### Example

> "My wifi is not working." → Awaaz: "Which hostel?" → "Hostel 2." → "Done. Ticket 8 raised for IT WiFi, medium priority." and a new card appears on the dashboard.

---

## Key features

- **Hindi, Hinglish and English**, in voice and text. The reply comes back in the language the student used.
- **Spoken follow-ups** only when the ticket would be useless without the missing detail (at most two).
- **Duplicate detection.** The same issue at the same place is merged into one ticket and its report count goes up. Five reports escalate the ticket to high urgency. A different hostel is never merged by mistake.
- **Urgency colours.** Critical (red, pulsing), high, medium, low. Nearby deadlines raise urgency.
- **Critical emergencies** (for example "someone fainted") are ticketed immediately with no follow-up.
- **Confidential cases** (ragging, harassment). Details are masked on the dashboard and in the API, shown as "Confidential case" with a lock icon. A location is required first.
- **Status by voice**: "What is the status of ticket 7?"
- **Resolve button**, live stats and a **Reset button** (deletes all tickets, optionally behind a PIN) on the dashboard.
- **Typed fallback** if the microphone is unavailable.
- **Ten departments:** Hostel, IT/WiFi, Mess/Canteen, Academics, Transport, Library, Sports, Events/Clubs, Health/Medical, Safety/Admin.

---

## How it works

```
 Browser (mic or typed text)
        |  audio
        v
 /api/voice -> Groq Whisper (speech to text)
        |  text
        v
 process_text()
   1. detect language (English / Hinglish / Hindi)
   2. LLM returns a JSON plan (Groq, Gemini as backup)
   3. server rules: duplicates, confidential, templates
        |                          |
        v                          v
 SQLite ticket            reply text (+ optional email)
        |                          |
        v                          v
 Dashboard (polls          Browser shows text at once,
 /api/tickets every 2s)    then /api/tts speaks it
```

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI |
| Database | PostgreSQL when `DATABASE_URL` is set (hosted demo), SQLite file locally |
| Speech to text | Groq Whisper large-v3 |
| LLM | Groq `openai/gpt-oss-120b` then `llama-3.3-70b-versatile` (or `GROQ_MODEL`), Gemini flash models as backup, availability checked at startup |
| Text to speech | edge-tts (`en-IN-NeerjaNeural`, `hi-IN-SwaraNeural`), browser `speechSynthesis` as fallback |
| Frontend | One file, vanilla JavaScript (`static/index.html`) |
| Email (optional) | Gmail SMTP |

---

## Technical decisions

1. **The server decides the reply language, not the LLM.** The server detects English, Hinglish or Hindi, tells the LLM which one to use, checks the script of the answer and retries once if it is wrong. Short answers to a follow-up keep the follow-up's language.
2. **Replies for create, merge and status are fixed templates in all three languages.** The LLM only writes follow-up questions. This stops the model from inventing urgency, report counts or ticket numbers.
3. **Duplicate detection is code, not LLM judgement.** Same department, same place and a similar issue. It overrides the LLM and blocks wrong merges such as Hostel 5 into Hostel 3.
4. **Confidential masking happens at the API level**, so details never reach the dashboard.
5. **Text first, audio second.** The text reply shows immediately and the audio is fetched separately, so a slow voice never blocks the interface. The browser voice takes over if edge-tts fails or times out (6 seconds).
6. **Deterministic model fallback chain.** Models are discovered at startup, but the order is fixed: `GROQ_MODEL` if set, then the preferred Groq models, then Gemini. A 429 or 503 puts a model on a five-minute cooldown, a retired model on a one-hour cooldown, and the next one answers.
7. **Seeded demo tickets are labelled `[demo]`** and shown with a "demo data" tag on the dashboard.
8. **Timestamps are stored in UTC and "today" is computed in IST**, so "2 minutes ago" and "kal" are right whatever timezone the server runs in.
9. **The LLM's output is cleaned before it touches the database.** Department names are mapped to the ten official ones, and invalid dates and placeholder locations are dropped. A merge into an already resolved ticket is refused.
10. **Storage that survives restarts.** One small wrapper lets the same code run on PostgreSQL (hosted) and SQLite (local). Tickets are only deleted by the Reset button.

---

## Challenges and how they were solved

- **Whisper turns Hinglish into Urdu script.** If the transcript comes back in Urdu script, the server retries with `language="hi"`.
- **Retired and rate-limited models.** Two Gemini models were retired for the key and others had no quota, so models are auto-discovered and put on cooldown after errors instead of hard-coded.
- **The LLM invented details.** Moving create/merge/status replies into templates removed that class of error.
- **Wrong merges.** The LLM merged complaints from different hostels. Duplicate detection moved into code with a place-matching rule ("hostel 3" matches "hostel 3 room 5", never "hostel 33").
- **Whisper parrots its own prompt on silence.** A short sentinel phrase now ends the prompt, and a transcript that is just that phrase, a prompt echo or a tiny audio clip is ignored instead of becoming a ticket.
- **A follow-up answer read as a status question.** An answer such as "number 12" to "which bus number?" was taken as a request for ticket 12's status. The server now remembers which kind of question is pending and re-asks the model once.
- **Slow text to speech.** Decoupled from the text reply, with a timeout and a browser-voice fallback.
- **"5 h ago" on a brand-new ticket.** The server saved local time without a timezone marker and the browser read it as Indian time. Timestamps are now saved in UTC with a `Z` marker.
- **Free hosting wipes local files.** The tickets moved to a hosted PostgreSQL database, and the Reset button became the only way to clear them.
- **Free hosting sleeps.** A `/healthz` endpoint lets an uptime pinger keep the demo awake.

---

## Honest disclosure: what is real and what is mocked

**Real:** microphone capture, Whisper transcription, LLM understanding, ticket creation, duplicate merging, escalation, confidential masking, status by voice, spoken replies, the live dashboard and (when configured) real email through Gmail SMTP.

**Mocked or limited:**
- The tickets labelled **demo data** are seeded sample tickets (Ctrl+Shift+D) so the dashboard is not empty.
- There is no real campus department system behind Awaaz. The "department inbox" for email is a single address set in `NOTIFY_TO`, which in the demo is the author's own inbox.
- The hosted live demo runs without email: the free hosting plan blocks outbound SMTP ports.
- The Reset button is available to anyone with the link unless a `RESET_PIN` is set.
- There is no login. Anyone with the link can create or resolve tickets.

---

## My contribution

I built this project solo during the hackathon. I chose the problem and scope, designed the architecture and the key decisions above, wrote and tested the system, and ran the demo rehearsals. I used Claude as an AI coding assistant for generating and debugging code, which the hackathon rules allow. The Antigravity IDE was used as an editor only. Every behaviour listed here was tested by hand with voice and typed input before submission.

---

## Run it locally

Requirements: Python 3.11 or newer, a free Groq API key and a free Gemini API key.

```powershell
git clone <your-repo-url>
cd Awaaz
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then open .env and paste your keys
uvicorn main:app --reload
```

Open http://127.0.0.1:8000 and press **Ctrl+Shift+D** once to load demo tickets.

On macOS or Linux, activate with `source venv/bin/activate` and copy with `cp .env.example .env`.

### Optional: persistent database, reset PIN, model

Set `DATABASE_URL` (a PostgreSQL connection string) to keep tickets across restarts. Without it a local `tickets.db` file is used. `RESET_PIN` protects the Reset button and `GROQ_MODEL` pins the Groq model. See `.env.example`.

### Optional: email each new ticket

Add `SMTP_USER`, `SMTP_PASS` (a Gmail app password) and `NOTIFY_TO` to `.env` and restart. Without them, no email is sent.

### Try it

| Say | Expected |
|---|---|
| "My wifi is not working", then "Hostel 2" | Follow-up, then a ticket |
| "Someone fainted at the ground" | Critical ticket, no follow-up |
| "Hostel 5 ka wifi band hai" | Hinglish reply and a new ticket |
| "What is the status of ticket 7?" | Status spoken back |
| "Someone is ragging me", then "Hostel 6" | Private confidential ticket, details masked |

---

## Project structure

```
main.py            API endpoints, speech to text, LLM routing, language detection, duplicate detection, templates
prompt.py          System prompt, department list and JSON schema
static/index.html  Dashboard and voice interface
requirements.txt   Python dependencies
.env.example       Environment variable template
```

## Limitations and next steps

- Authentication and per-department logins for staff.
- Real department routing instead of a single inbox.
- Streaming speech for lower latency.