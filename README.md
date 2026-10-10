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
│   │   ├── responder.py       # intent, humanised delay, scripted flow, AI handoff
│   │   ├── ai_agent.py        # the AI Messenger agent - see below
│   │   ├── comment_responder.py  # keyword-triggered comment reply + private DM
│   │   └── webhook.py         # FastAPI receiver, signature-verified, /debug/* routes
│   ├── pages/
│   │   └── explainers.py      # FAQ hub, booking redirect, privacy policy
│   └── dashboard/app.py       # Streamlit: Overview, Posts, Analytics, Outreach
├── data/posts.json            # the acquisition posts
├── config/
│   ├── .env.example
│   └── message_templates.json
├── tests/                     # 103 tests, Graph API and OpenAI both fully mocked
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
  (`new`/`auto_replied`/`awaiting_human`/`human_handled`/`closed`), handoff time
- **Message** — every inbound and outbound message, unique on Facebook's `mid`
  so redelivered webhook events are never handled twice

---

## Tests

```bash
python -m pytest tests/ -q
```

109 tests, no network access — the Graph API and the OpenAI API are both fully
mocked. They cover the exact Graph API payload we send (endpoint selection,
`published` flag, `scheduled_publish_time`), error and retry classification,
insight parsing, all six models with their relationships and cascades, the
scheduler jobs, the scripted Messenger flow (intent detection, delay bounds,
business-hours switching, template rotation, duplicate webhook suppression,
signature verification), the AI agent (price-guardrail enforcement, fail-closed
behaviour on any API error, one automatic retry on a network error, known-fields
injection, history truncation, field extraction, a full long-conversation
stress test, and full-pipeline integration with the scripted flow as fallback),
and comment auto-reply (keyword matching, dedup, public-Page-comment exclusion).

---

## Messenger auto-reply

Replies **only to people who message the Page first**, inside Meta's 24-hour
standard messaging window. This is the sanctioned counterpart to the outreach
automation that is deliberately not implemented.

```bash
python run.py webhook          # run the receiver (default port 8000)
python run.py inbox            # conversations waiting on a human
```

### Positioning

The Page is **acquisition only** (client confirmed 2026-09-23): Elite Homes USA
presents as a company actively *buying* in Jacksonville. No buyer-list,
wholesaler, or partner content. `detect_intent()` reflects this — any seller
signal wins outright, even when a message scores higher on buyer keywords,
because a seller lead is the one that matters.

### How it stays believable

| Behaviour | Why |
|---|---|
| Randomised delay before replying | A fixed interval is as obvious a tell as an instant reply |
| `mark_seen` then `typing_on` | The indicator appears when a person would start typing |
| Varied phrasing, no repeated openers | Consecutive leads never see identical or templated-sounding text |
| Separate after-hours copy | A cheerful instant reply at 2 AM is obviously automated |
| One question at a time | Real texters don't bundle three questions into one message |

Delay is configurable (`AUTOREPLY_MIN_DELAY_SECONDS` / `_MAX_`), currently
1.5–2 minutes (client preference) — long enough that a reply never feels
instant/automated, short enough that it still arrives while the person is
actively in the conversation.

### Two reply engines, one fallback chain

1. **AI agent** (`app/messenger/ai_agent.py`) — the primary path when
   `AI_AGENT_ENABLED=true`. See [AI Messenger agent](#ai-messenger-agent) below.
2. **Scripted flow** (`responder.py`'s `_start_flow` / `_advance_flow`) — a
   fixed question sequence (address → condition → timeline → reason → phone →
   best time). This is what runs if the AI agent is disabled, unconfigured, or
   a call to it fails for any reason — the AI layer can only improve the
   experience, never break it.

Either way, once a conversation is hands off to a human
(`ConversationStatus.AWAITING_HUMAN`), it never gets another automated reply —
`python run.py inbox` lists everything waiting.

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
conversation for anyone messaging the Page — not just sellers — while
collecting the same information a seller lead needs (address, condition,
timeline, reason for selling, phone, best time to call).

### Guardrails come first, not last

An ungoverned AI talking to real sellers about their homes is a liability
risk, not just a feature. Two independent layers enforce the one rule that
matters most:

1. The system prompt instructs the model to never state a price, a dollar
   figure, or anything that reads as an offer or valuation — and to never
   guess at legal, tax, or financial specifics, or invent an answer it
   doesn't actually have. When it doesn't know, it says so and hands off.
2. **Every reply is scanned for price-shaped text** (`_contains_price`) before
   it's ever sent, regardless of what the model did. If one slips through,
   it's discarded and swapped for a safe fallback with handoff forced on.
   The prompt is necessary but not sufficient; the scan is what actually
   stops a leak — `test_price_in_reply_is_blocked_even_if_model_ignored_the_prompt`
   pins this.

The system prompt also injects the real current date on every call, so the
agent can catch an inconsistent or already-past date a person gives instead
of silently accepting it.

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
  handled fine.
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
