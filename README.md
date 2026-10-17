# Elite Homes USA — Facebook Automation POC

A proof-of-concept automation stack for **Elite Homes USA** (Jacksonville, FL):
scheduled Facebook Page posting through the official Graph API, post analytics
collection, an AI-powered Messenger agent, a comment auto-reply system, a
supporting FAQ/booking website, an outreach prospect pipeline, and a Streamlit
dashboard over all of it. Deployed in production on Render, backing the real
Elite Homes USA Facebook Page.

---

## Status of each component

| Component | State | Notes |
|---|---|---|
| Database (SQLite + SQLAlchemy) | **Working** | 6 tables, created by `run.py init` |
| Graph API posting (`app/facebook/poster.py`) | **Working** | Immediate + Facebook-side scheduled posts |
| Token management (`app/facebook/auth.py`) | **Working** | Long-lived exchange, Page token, validation |
| Scheduler (`app/scheduler/tasks.py`) | **Working** | Publishing with retries, hourly analytics |
| Dashboard (`app/dashboard/app.py`) | **Working** | 4 pages, login-gated |
| Outreach: classification, templating, throttling, logging | **Working** | Dry-run campaigns run end to end |
| Messenger auto-reply, scripted flow (inbound only) | **Working** | `pages_messaging` is granted and live; works for everyone once Meta's App Review finishes (currently active for Page roles) |
| **AI Messenger agent** (`app/messenger/ai_agent.py`) | **Working** | Replaces the scripted flow with a real conversation; see [AI Messenger agent](#ai-messenger-agent) |
| Comment auto-reply (`app/messenger/comment_responder.py`) | **Working** | Keyword-triggered public reply + private DM, feeds into the same agent |
| Explainer / FAQ / booking website (`app/pages/`) | **Working** | FastAPI site covering what Facebook's own Page tabs can't (deployed on Render) |
| Outreach: credentialed login, group scraping, DM sending | **Not implemented** | See [Outreach: compliant alternatives](#outreach-compliant-alternatives) |

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Requires Python 3.11+. On Python 3.14 you need `SQLAlchemy>=2.0.44`; the pin in
`requirements.txt` already handles this.

### 2. Configure

```bash
cp config/.env.example config/.env
```

Fill in `config/.env`:

| Variable | Where to get it |
|---|---|
| `FB_APP_ID`, `FB_APP_SECRET` | [developers.facebook.com](https://developers.facebook.com) → your App → Settings → Basic |
| `FB_PAGE_ID` | The Elite Homes USA Page → About → Page transparency |
| `FB_PAGE_ACCESS_TOKEN` | Generated in step 3 below |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | Your choice (defaults `admin` / `elite2024`) |
| `DATABASE_URL` | Leave as `sqlite:///./elite_homes.db` for the POC |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) → API keys. Only needed for the AI Messenger agent |

Your Facebook App needs these permissions, and the Page must be administered by
the user generating the token:

- `pages_manage_posts` — publish and schedule posts
- `pages_read_engagement` — read post insights
- `pages_show_list` — enumerate the Pages the user administers
- `pages_messaging` — send/receive Messenger conversations (needs App Review for general public use; works immediately for anyone with a role on the app)
- `pages_manage_engagement`, `pages_read_user_content` — comment auto-reply
- `pages_manage_metadata` — webhook field subscriptions, Page info updates

**The app itself must be switched to Live mode** (App Dashboard → Publish) —
while in Development mode, everything posted or sent is visible only to people
with a role on the app, never real visitors. This tripped up this exact project
for a while; see `app/messenger/webhook.py`'s `/debug/*` routes if posts or
replies ever go quiet again.

### 3. Get a Page access token

Grab a short-lived **user** token from the
[Graph API Explorer](https://developers.facebook.com/tools/explorer/), then:

```bash
python run.py auth --token <short-lived-user-token>
```

This exchanges it for a long-lived (~60-day) user token, derives the Page token
(which does not expire), and writes it into `config/.env`.

Verify it any time:

```bash
python run.py verify
```

### 4. Initialise and seed

```bash
python run.py init     # create tables
python run.py seed     # queue the acquisition posts, 24h apart starting in 1 hour
python run.py status   # summary of the queue
```

---

## Usage

```bash
python run.py scheduler                    # run the posting loop (Ctrl+C to stop)
python run.py dashboard                    # launch the Streamlit dashboard
python run.py post --message "Hello Jax"   # publish one post immediately
python run.py post --message "Hi" --at 2026-10-01T09:00:00   # schedule one post
python run.py outreach --dry-run           # render + log messages, send nothing
python run.py webhook                      # Messenger auto-reply receiver
python run.py inbox                        # conversations awaiting a human
python run.py doctor --token "<token>"     # diagnose token / Page access problems
python run.py cancel --post-type buyer     # pull posts from the queue
python run.py reschedule --at 2026-09-24T09:00   # re-time pending posts, local tz
```

The dashboard is also launchable directly:

```bash
streamlit run app/dashboard/app.py
```

### How scheduling works

There are two independent layers, and you can use either:

1. **Facebook-side scheduling** — `publish_post(..., scheduled_time=...)` creates
   the post with `published=false` and a `scheduled_publish_time`. Facebook
   publishes it server-side even if this app is not running. Must be 10 minutes
   to 6 months out.
2. **App-side scheduling** — rows in `scheduled_posts` with a `scheduled_time`.
   The APScheduler job `publish_due_posts` runs every minute and publishes the
   ones that are due. This is what `run.py seed` populates, and it gives you
   retry handling and a local audit trail.

Failed posts retry up to 3 times, but only for transient Graph errors (rate
limits, service errors). Auth and permission errors fail immediately rather than
burning quota against a broken token.

---

## Project layout

```
elite-homes-poc/
├── app/
│   ├── config.py              # env loading, Settings, logging setup
│   ├── database/
│   │   ├── models.py          # ScheduledPost, OutreachProspect, OutreachLog, PostAnalytics
│   │   └── db.py              # engine, session_scope(), init_db()
│   ├── facebook/
│   │   ├── auth.py            # token exchange, Page token, /debug_token validation
│   │   └── poster.py          # publish_post(), get_post_metrics(), delete_post()
│   ├── scheduler/
│   │   └── tasks.py           # publish_due_posts(), refresh_post_analytics(), seeding
│   ├── outreach/
│   │   ├── session.py         # Playwright lifecycle + cookie persistence
│   │   ├── scraper.py         # segment classification, CSV import, dedupe
│   │   ├── messenger.py       # message templating
│   │   └── worker.py          # throttling, daily quota, dry-run campaigns
│   ├── messenger/
│   │   ├── api.py             # Messenger send API + sender actions
│   │   ├── responder.py       # opt-out, intent, humanised delay, lead saving, scripted fallback
│   │   ├── ai_agent.py        # the AI Messenger agent - see below
│   │   ├── florida_zips.py    # ZIP code detection + lookup against the Florida reference list
│   │   ├── comment_responder.py  # keyword-triggered comment reply + private DM
│   │   └── webhook.py         # FastAPI receiver, signature-verified, /debug/* routes
│   ├── pages/
│   │   └── explainers.py      # FAQ hub, booking redirect, privacy policy
│   └── dashboard/app.py       # Streamlit: Overview, Posts, Analytics, Outreach
├── data/
│   ├── posts.json             # the acquisition posts
│   └── florida_zip_codes.csv  # client's Florida ZIP/city/county reference (1,218 ZIPs)
├── config/
│   ├── .env.example
│   └── message_templates.json
├── scripts/evaluate_agent.py  # runs the client's test messages through the live AI agent
├── tests/                     # 316 tests, Graph API and OpenAI both fully mocked
├── render.yaml                # Render deployment config (non-secret env defaults)
└── run.py                     # CLI entry point
```

### Data model

- **ScheduledPost** — content, media URL, segment, scheduled time, status
  (`pending`/`published`/`failed`/`retry`), Facebook post ID, retry count
- **OutreachProspect** — name, profile URL, source group, matched keyword,
  segment, status (`pending`/`contacted`/`replied`/`declined`/`no_response`)
- **OutreachLog** — per-action audit trail (`message`/`friend_request`/`page_invite`
  × `sent`/`failed`/`blocked`)
- **PostAnalytics** — reach / engagement / clicks snapshots, collected hourly
- **Conversation** — one Messenger thread: PSID, detected intent, status
  (`new`/`auto_replied`/`awaiting_human`/`human_handled`/`closed`), handoff time,
  every lead detail the agent collects (name, address, city, ZIP, county from the
  ZIP reference, condition, timeline, contact preference, phone, email,
  walkthrough availability, and for wholesalers company, square footage, and
  septic or sewer), when the lead was submitted, and whether they opted out.
  `init_db()` adds any of these columns an older database is missing.
- **Message** — every inbound and outbound message, unique on Facebook's `mid`
  so redelivered webhook events are never handled twice

---

## Tests

```bash
python -m pytest tests/ -q
```

316 tests, no network access — the Graph API and the OpenAI API are both fully
mocked. They cover the exact Graph API payload we send (endpoint selection,
`published` flag, `scheduled_publish_time`), error and retry classification,
insight parsing, all six models with their relationships and cascades, adding
missing columns to an older database, the scheduler jobs, the scripted
Messenger flow (intent detection, first-reply vs. later-reply delay bounds,
business-hours switching, template rotation, duplicate webhook suppression,
signature verification), the Florida ZIP lookup (house numbers, prices, and
phone numbers never mistaken for ZIPs), opt-out handling, lead submission
(saved before the reply goes out, honest reply if saving fails), the AI agent
(price-guardrail enforcement, fail-closed behaviour on any API error, one
automatic retry on a network error, known-fields and ZIP-fact injection,
history truncation, verbatim-repeat regeneration, the team follow-up line,
field extraction, a full long-conversation stress test, and full-pipeline
integration with the scripted flow as fallback), and comment auto-reply
(keyword matching, dedup, public-Page-comment exclusion, never DMing someone
who opted out).

### Live agent evaluation

```bash
python scripts/evaluate_agent.py              # all scenarios
python scripts/evaluate_agent.py --only 3,P5  # just these
```

Sends the client's 35-question test sheet plus the service policy's test list
(Florida city without a ZIP, recognised and unlisted ZIPs, city/ZIP conflict,
street address only, out-of-state property, out-of-state phone number, request
for a person, Messenger-only preference, honesty about being a bot, prompt
injection, a past date, wholesalers inside and outside the buy box) through the
**real** OpenAI API with the same inputs the webhook builds. Each reply is
checked in code for dashes, prices, offered days or times, merged
question/statement sentences, and scenario-specific content, and a Markdown and
JSON report is written to `logs/`. A full run makes about 60 API calls. It's
the "send each message to the bot and share the responses" step the client's
test sheet asks for.

---

## Messenger auto-reply

Replies **only to people who message the Page first**, inside Meta's 24-hour
standard messaging window. This is the sanctioned counterpart to the outreach
automation that is deliberately not implemented.

```bash
python run.py webhook          # run the receiver (default port 8000)
python run.py inbox            # leads for the team, with every detail collected
```

### Positioning

The Page is **acquisition only**: Elite Homes USA Group buys houses as-is
**throughout Florida, all 67 counties** (owner-approved service policy,
2026-10-12), not just Jacksonville. It answers homeowners and wholesalers
sending deals; there's no buyer-list content. `detect_intent()` is only a
starting guess (clear wholesaler language first, then any seller signal);
when the AI agent replies, its own classification wins.

### Stop requests, people, and leads

- **Opt-out.** A stop request ("STOP", "stop texting me", "remove me from your
  list", ...) is caught in code by `is_opt_out_request()` before the AI ever
  sees the message. They get one short confirmation, the conversation is
  marked `opted_out` and closed, and nothing automated ever goes to them again,
  including a DM from a later post comment. They're never listed as a lead.
  "Don't text me, call me instead" is a contact preference, not a stop. The
  agent can also flag a stop request in words the check doesn't know.
- **A person takes over.** Once a conversation is handed off
  (`AWAITING_HUMAN`) or a human owns it (`HUMAN_HANDLED`), the bot stays out of
  it, AI included.
- **Leads.** When someone asks for a person, an offer, a walkthrough, or a
  callback, or a wholesaler sends a deal, the lead is recorded for the team
  right away (`lead_submitted_at`) without waiting for every detail, and the
  bot keeps helping collect the rest. Details are saved **before** the reply
  is sent, so a restart during the reply delay can't lose them, and the bot
  never says it passed details along that weren't saved: if saving fails, the
  reply becomes "I haven't been able to pass your details through yet..." and
  the conversation goes to a person. `python run.py inbox` lists every lead,
  showing missing core details as "unknown".
- **No lead notifications yet.** The service policy says not to forward leads
  anywhere until the client confirms where alerts should go (email, SMS, a
  CRM). Until then leads live in the database and `run.py inbox`, and the
  conversations themselves are in the Page's Messenger inbox.

### How it stays believable

| Behaviour | Why |
|---|---|
| Randomised delay before replying | A fixed interval is as obvious a tell as an instant reply |
| `mark_seen` fires right before `typing_on`, not on receipt | A message marked "Seen" instantly, then silence for 90s, is its own tell — a real person doesn't necessarily open it the second it lands |
| Varied phrasing, no repeated openers | Consecutive leads never see identical or templated-sounding text |
| Separate after-hours copy | A cheerful instant reply at 2 AM is obviously automated |
| One question at a time | Real texters don't bundle three questions into one message |

Delay is configurable and split in two (client preference): the **first**
reply in a conversation uses `AUTOREPLY_FIRST_MIN/MAX_DELAY_SECONDS` (~30–40s,
so a lead doesn't feel ignored), and **every reply after that** uses
`AUTOREPLY_MIN/MAX_DELAY_SECONDS` (90–120s) — slower and more deliberate, so
the pace reads as human rather than instant. The delay is measured from when
the message arrived, so the AI's own processing time (usually 2–6s) is part
of it rather than added on top.

### Two reply engines, one fallback chain

1. **AI agent** (`app/messenger/ai_agent.py`) — the primary path when
   `AI_AGENT_ENABLED=true`. See [AI Messenger agent](#ai-messenger-agent) below.
2. **Scripted flow** (`responder.py`'s `_start_flow` / `_advance_flow`) — a
   fixed question sequence (location → condition → timeline → reason → phone →
   best time), with copy that follows the same policy (introduces the virtual
   assistant, all of Florida, no dashes). This is what runs if the AI agent is
   disabled, unconfigured, or a call to it fails for any reason — the AI layer
   can only improve the experience, never break it.

### Setup

1. `AUTOREPLY_ENABLED=false` in `config/.env` records messages without replying.
2. Expose the webhook over HTTPS (ngrok for testing, a real host for production
   — this project runs on Render; see `render.yaml`).
3. Facebook App → **Webhooks → Page** → callback URL `https://<host>/webhook`,
   verify token = `FB_VERIFY_TOKEN`, subscribe to **messages** and **feed**
   (feed is for comment auto-reply, see below). Also install the app on the
   Page itself: `POST /{page-id}/subscribed_apps?subscribed_fields=feed,messages`
   — subscribing at the app level is not enough on its own.
4. `pages_messaging` needs App Review to work for the general public; it works
   immediately for anyone with a role on the app once granted (Standard
   Access). Submit for review with a screencast of the flow when ready.
5. Flip `AUTOREPLY_ENABLED=true`, and `AI_AGENT_ENABLED=true` if using the AI
   agent (needs `OPENAI_API_KEY` set too).

Every delivery is signature-verified against the app secret
(`X-Hub-Signature-256`); without that anyone who learns the URL could forge
inbound messages and make the Page reply to strangers.

### If replies silently stop working

This happened twice during development, both silent (webhook returns 200,
nothing goes out) and both diagnosed with the `/debug/*` routes in
`webhook.py` (`/debug/recent` — what actually reached the DB; `/debug/token` —
what token is actually loaded, checked live against Facebook; `/debug/reset` —
unstick a test conversation; `/debug/log` — tail the log file). Both routes
are gated behind `?key=<FB_VERIFY_TOKEN>` as a cheap deterrent, not real auth —
**remove them before this goes fully production-hardened.**

Root causes found so far, in order of likelihood:
1. **The app is still in Development mode.** Published content and replies
   are then visible only to people with a role on the app, never real
   visitors — looks identical to "nothing is happening" from the outside.
   Fix: App Dashboard → Publish.
2. **The Page token is stale or was never exchanged.** A token pasted
   straight from Graph API Explorer is short-lived and type `USER`; always
   run it through `python run.py auth --token <token>` first and use *that*
   output, not the raw paste. `/debug/token` shows `type` and `expires_at`
   live, so this is always checkable rather than assumed.
3. **The Page isn't subscribed to the right fields**, or only the app-level
   subscription was set without installing the app on the Page itself — see
   step 3 above.

---

## AI Messenger agent

`app/messenger/ai_agent.py`. Replaces the fixed question script with a real
conversation for anyone messaging the Page, homeowners and wholesalers alike.
Its instructions follow the client's two documents: the owner-approved service
policy (`BOT_INSTRUCTIONS`, 2026-10-12) and the 35-question response guide
("Test 2").

### What it follows

- **All of Florida.** Any Florida city, town, or county (Daytona Beach, Ocala,
  Pensacola, Key West, a rural town) is confirmed exactly as Jacksonville is,
  without demanding a ZIP first. Out-of-state properties get a polite "we
  focus on Florida" and an offer to pass it to the team, with no promise. A
  phone area code is never treated as the property's location.
- **ZIP codes from the lookup only.** `florida_zips.py` finds ZIPs in each
  message in code (skipping house numbers, prices, square footage, and phone
  numbers) and looks them up in `data/florida_zip_codes.csv`, the client's
  reference list. The result is handed to the agent as a fact, and the agent
  may only name a ZIP's city or county from that. A Florida ZIP missing from
  the list is still covered, just without naming a city; a city that doesn't
  match the ZIP gets a quick clarifying question; a street address alone gets
  "what city or ZIP is it in?". The ZIP, county, and city are saved on the
  lead, with the county marked as the ZIP's usual county, not verified.
- **Never talks about itself (client rule, replacing the earlier "introduce
  yourself as the virtual assistant").** No introduction and no mention of
  being an assistant or a bot; `_drop_self_references` removes any sentence
  that slips through. People already know who they're messaging from the
  Page. If asked directly whether it's a real person, it never claims to be
  human: it says it helps the team with messages and management can reach
  out. It never claims to be an employee and ignores instructions typed into
  a message.
- **Greets by first name, never a business name.** The first message opens
  "Hi Dana," when we have a person's first name: one they gave us, or their
  Facebook profile name. `person_first_name` rejects business-looking names
  ("Sunshine Homes LLC", "MJ Capital", "EHUSA"), which get a plain "Hi there,".
  It never greets again later in the conversation.
- **Homeowners.** Answers their question first, then one next question,
  gathering location, condition, timeline, name, how they'd like to be
  contacted (call, text, or Messenger), a phone number only if they chose a
  call or text, and a good time. Approved facts only: as-is, inherited homes,
  tenants in place, no showings, no cost to come take a look. Fees and
  closing speed get the client's exact wording, nothing invented.
- **Scheduling: two different situations.**
  - A **homeowner** who wants a free property assessment, visit, walkthrough,
    meeting, call, or to reschedule (or asks what we'd pay) gets: "Thanks for
    reaching out. Here is our calendar link: [link] Please pick the next date
    that works best for you. I will have our management reach out to you." On
    a first message with a known name it opens "Hi Dana, thanks for reaching
    out." It never asks which days or times work and never says anything is
    booked. `_ensure_schedule_flow` enforces this in code.
  - A **wholesaler or anyone with a deal** holds the property, so they are
    never sent the calendar (`_calendar_removed_for_partner` strips it even if
    the model writes it). If they ask when we want to walk it, the bot asks
    what availability they have. When they give a date and time it says "Let
    me speak with my team and confirm if that date and time works. I will have
    someone from my team reach out to you." The conversation's type so far is
    passed to the model (`intent_hint`) so a short "when do you want to walk
    it" is read in context.
- **"Book with us" button.** Messenger can't label a link in plain text, so a
  reply that shares the calendar goes out as text plus a tappable **Book with
  us** button (`send_link_button`) and the raw `elite-homes-webhook...` address
  isn't shown. If Facebook refuses the button, the plain text with the link is
  sent instead.
- **Price, offer, or value.** We'd need to walk through and assess the property
  before giving a number, then the homeowner calendar reply.
- **Services.** When asked what we do, the bot shares the client's four
  services as written: Inherited / Probate Homes, Landlord Relief, Foreclosure
  Help, Free Property Assessment. (The Facebook Page's own Services tab can't
  be set through the API and is edited by hand.)
- **How every conversation closes.** When the bot wraps up and hands over, the
  last sentence is always "Let me earn your business and let me go to work for
  you." (`CLOSING_LINE`, added in code if the model leaves it out). Never in
  the middle of a chat, and never for someone who asked us to stop.
- **One reply per burst of messages.** If someone sends a second message while
  a reply is still waiting out its delay, the first reply is dropped and the
  second answers both (`_register_inbound` / `_is_superseded`). Before this,
  two quick messages produced two replies in a row, which read as a duplicate.
- **Wholesalers.** Checks deals against the buy box (single-family, 1,000 sq ft
  minimum, city sewer only, anywhere in Florida), asks for whatever's missing
  of septic or sewer, square footage, name, LLC, email, phone, and photos or a
  deal sheet, then "When can we walk the property?". Deals outside the buy box
  get a thank-you and a request for ones that fit. Deal sheets go to
  `Ellis@elitehomes1.com`.
- **The calendar link.** `MEETING_LINK` (`/schedule`, which redirects to the
  booking tool set as `BOOKING_URL` in `app/pages/explainers.py`). Only for
  homeowners, and the only link the agent may share. When wrapping up with an
  interested homeowner the last message includes it, but not for out-of-state
  properties, deals outside the buy box, or anyone upset or opted out.
- **Structured output.** Every reply comes back with the message, the intent
  (homeowner or wholesaler), whether to submit the lead, whether to hand off,
  whether they asked to stop, the details they gave, and a short factual
  lead summary for the team.

### Guardrails come first, not last

An ungoverned AI talking to real sellers about their homes is a liability
risk, not just a feature. Two independent layers enforce the one rule that
matters most:

1. The system prompt instructs the model to never state a price, a dollar
   figure, or anything that reads as an offer or valuation (the walkthrough
   answer above instead). It's also told never to give legal, tax, or
   financial advice, never to promise to stop a foreclosure, and never to
   invent offers, appointments, fees, policies, or outcomes. When it doesn't
   know, it says so and hands off.
2. **Every reply is scanned for price-shaped text** (`_contains_price`) before
   it's ever sent, regardless of what the model did. If one slips through,
   it's discarded and swapped for the walkthrough answer, with the lead
   submitted and handoff forced on. Square footage ("1,000 sq ft", "1k sq ft")
   isn't mistaken for a price. The prompt is necessary but not sufficient;
   the scan is what actually stops a leak —
   `test_price_in_reply_is_blocked_even_if_model_ignored_the_prompt` pins this.

The system prompt also injects the real current date on every call, so the
agent can catch an inconsistent or already-past date a person gives instead
of silently accepting it.

### Staying natural over many conversations

- **Never repeats itself.** The prompt explicitly forbids reusing the exact
  wording of an earlier reply in the same conversation, and a runtime check
  (`_is_repetitive` + `_regenerate_distinct_reply`) catches it if the model
  ignores that anyway: the same reply as last time, the same closing question
  as last time (even reworded), or a question already asked twice. It gets
  one regeneration call asking for something different before a repeat ever
  goes out. A skipped detail, even the address, is asked about at most twice;
  a city or ZIP is enough for the team.
- **Example replies from the client's own guide** (24 situations: greeting,
  a Florida city, a ZIP, price, closing speed, repairs, fees, inherited,
  tenants, moving soon, wanting a call, the team transition, realtors, hard
  situations, walkthroughs, pictures, a wholesale deal, outside the buy box,
  out of state, email) are given to the model as tone guides only. It's told
  never to copy them word for word and never to reuse one, so real
  conversations still sound distinct from each other.
- **One contact email, always exact.** `SUPPORT_EMAIL` (`Ellis@elitehomes1.com`)
  is injected into the prompt and is the only address the agent is allowed to
  give out — it's told never to invent or substitute a different one.
- **Friendly, calm, never pushy.** The tone rule explicitly says not to
  pressure or rush anyone, and not to repeat a request impatiently if someone
  is slow to answer.
- **Gathers quality, not just checkboxes.** If an answer is vague or one-word
  ("not sure", "soon"), the prompt tells the agent to ask one natural
  follow-up for something concrete rather than moving straight to the next
  question or rushing to hand off.
- **Never uses a dash (client preference).** The prompt forbids the em dash
  (—), the en dash (–), and a spaced hyphen, and `_strip_dashes` removes any
  that slip through anyway, along with rarer dash characters (figure dash,
  minus sign, fullwidth forms). A dash in a range becomes "to" ("9am–5pm" →
  "9am to 5pm", "Mon–Fri" → "Mon to Fri"); anywhere else it becomes a comma.
  Models default to these dashes by habit, so this is enforced in code, not
  just asked for. A hyphen inside an ordinary word (`as-is`,
  `single-family`) and phone numbers are left alone.

### Memory that survives a database reset

Render's free plan wipes the SQLite file on every deploy and every restart
(including waking from sleep), so on its own the database would forget
everyone. When a message arrives from someone the database has no messages
from, `_restore_history_from_facebook` reads their Messenger history from
Facebook (`get_conversation_history`, up to 200 messages) and stores it before
replying. The agent then remembers what they said days ago, doesn't introduce
itself again, and a past "STOP" in that history keeps them opted out. Anything
they said before the last 30 messages is passed to the model as a short
"earlier, they told you" list, so a name or address from long ago isn't lost.

Facebook history doesn't include our own records (lead flags, handoff status,
extracted details), so those still reset with the database until it's on
persistent storage.

### Staying accurate over a longer conversation

- Already-captured fields (address, condition, etc.) are passed to the model
  as explicit known facts on every call, not re-inferred from raw history —
  it's told plainly what it already knows and never re-asks for it.
- History sent to the model is capped at the most recent 30 messages, so a
  long-running conversation stays fast and focused instead of growing
  unbounded; anything extracted earlier is preserved via the known-facts list
  above regardless of what falls out of that window.
- One automatic retry on a dropped network call before handing off — a single
  flaky connection shouldn't end a conversation that a retry would have
  handled fine. Each call gets 45 seconds; a long wholesaler message took 25s
  in testing, while most replies take 2–6s.
- The prompt explicitly tells the model to read past typos, abbreviations,
  and slang rather than getting stuck on imperfect phrasing.

### Fails closed, always

Any failure — disabled, unconfigured, network error, malformed response,
empty reply — returns `AgentReply(success=False, handoff=True)` and the
caller falls straight through to the scripted flow. The AI layer never has
to work for the Messenger system to keep functioning.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for the agent to run at all |
| `AI_AGENT_MODEL` | `gpt-5.5` | Balance of reply quality and latency for real-time chat; a full reasoning-tier model was ~2x slower for a weaker reply in testing |
| `AI_AGENT_ENABLED` | `false` | Independent of `AUTOREPLY_ENABLED` — can be off while the scripted flow still runs |

---

## Comment auto-reply

`app/messenger/comment_responder.py`. A Page comment containing a trigger
keyword (`sell`, `selling`, `cash`, `offer`, `house`, `houses` —
`config/message_templates.json`'s `comment_reply.keywords`) gets a public
reply under the comment plus a private Messenger DM via Facebook's Private
Replies API, which opens straight into the same AI/scripted conversation
above — the commenter's ID becomes a normal `psid`, no separate logic needed.

The public reply only says we cover Florida and points them to their messages;
an address or contact details are only ever asked for privately. The DM
greets them by first name (never a business name) and asks what city or ZIP the property is in.
Someone who opted out is never DMed again, whatever they comment.

Needs `pages_manage_engagement` in addition to `pages_messaging`, and the
Page's webhook subscription must include `feed` (see Messenger setup above).
Same idempotency and "ignore the Page's own comments" guards as the message
path.

---

## Outreach: compliant alternatives

**What is not implemented, and why.** These functions raise
`NotImplementedByDesign`:

- `session.login()` — automated credential login
- `scraper.scrape_group_members()` / `scrape_group_posts()`
- `messenger.send_message()` / `send_friend_request()` / `send_page_invite()`

Driving a logged-in personal profile with an automation framework to scrape
member lists and send unsolicited DMs violates Meta's Terms of Service
(Automated Data Collection) and the Platform Terms. The practical risk is not
abstract: Meta disables detected accounts without an appeals path, and a
disabled personal profile takes down the Pages it administers — which would kill
the "Elite Homes USA" Page this POC posts to through the sanctioned API. It also
collects personal data without consent, and unsolicited commercial messaging
carries its own regulatory exposure.

**What works today instead:**

- **Lead generation, officially.** [Meta Lead Ads](https://www.facebook.com/business/ads/lead-ads)
  with the Graph API `/leadgen_forms` endpoint gives you consented contact
  details for exactly the three segments here, and they can be pulled
  programmatically into `OutreachProspect`.
- **Messenger Platform API.** You may message people who messaged the Page
  first, within a 24-hour window, extendable with approved message tags. This is
  the supported way to automate the "Message us SELL / BUY / PARTNER" replies the
  sample posts ask for — which is a natural next step for this POC.
- **CSV import.** `scraper.import_prospects_from_csv(path)` ingests a list you
  sourced compliantly (public MLS data, a purchased list with consent, inbound
  form fills). Columns: `name`, `profile_url`, `source_group`, `post_text`.
  Segment classification, templating, throttling, quota and audit logging all
  work on top of it unchanged.
- **Dry-run campaigns.** `run_campaign(dry_run=True)` renders every message and
  writes the `OutreachLog` rows without contacting anyone. Use it to review
  targeting and copy before a human sends anything.

If you decide to accept the risk of the browser path anyway, `session.py` gives
you `interactive_login()`: it opens a visible browser, a human signs in and
clears any 2FA or checkpoint, and cookies are then persisted for reuse. That
keeps a person in the loop for the authentication step, which is the part
automation handles worst.

---

## Security notes for the POC

- `config/.env` holds live credentials (including `OPENAI_API_KEY`) and is
  gitignored. Never commit it. On Render, secrets are set directly in the
  dashboard, not in `render.yaml`.
- A token pasted from Graph API Explorer is short-lived — always exchange it
  with `python run.py auth --token <token>` and use the derived Page token,
  never the raw paste, anywhere it's stored long-term (including Render).
- Dashboard auth is a plaintext comparison against an env var — fine for a local
  POC, not for anything internet-facing. Put it behind real auth before exposing it.
- The `/debug/*` routes in `webhook.py` are a shared-secret check
  (`?key=<FB_VERIFY_TOKEN>`), not real auth, and expose conversation content.
  Built for diagnosing production webhook issues — remove them once the
  current issues are fully resolved.
- `FB_USER_PASSWORD` is only read by the outreach module. Leave it blank.
- The SQLite file is unencrypted and holds prospect and lead personal data
  (including anything the AI agent collects). Treat it as sensitive.
  **It is not currently on persistent storage on Render** — every deploy
  resets it to empty, which also means every deploy currently loses
  conversation history. Needs a persistent disk or a real hosted database
  before this matters for a live business.
