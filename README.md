# Elite Homes USA — Facebook Automation POC

A proof-of-concept automation stack for **Elite Homes USA** (Jacksonville, FL):
scheduled Facebook Page posting through the official Graph API, post analytics
collection, an outreach prospect pipeline, and a Streamlit dashboard over all of it.

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
| Messenger auto-reply (inbound only) | **Built, gated** | Needs `pages_messaging` App Review before it can send |
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

Your Facebook App needs these permissions, and the Page must be administered by
the user generating the token:

- `pages_manage_posts` — publish and schedule posts
- `pages_read_engagement` — read post insights
- `pages_show_list` — enumerate the Pages the user administers

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
│   │   ├── responder.py       # intent, humanised delay, templates, handoff
│   │   └── webhook.py         # FastAPI receiver, signature-verified
│   └── dashboard/app.py       # Streamlit: Overview, Posts, Analytics, Outreach
├── data/posts.json            # the 5 acquisition posts
├── config/
│   ├── .env.example
│   └── message_templates.json
├── tests/                     # 65 tests, Graph API fully mocked
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

65 tests, no network access. They cover the exact Graph API payload we send
(endpoint selection, `published` flag, `scheduled_publish_time`), error and retry
classification, insight parsing, all six models with their relationships and
cascades, the scheduler jobs, and the Messenger auto-reply — intent detection,
delay bounds, business-hours switching, template rotation, one-reply-then-handoff,
duplicate webhook suppression, and signature verification.

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
| Randomised 45–90s delay | A fixed interval is as obvious a tell as an instant reply |
| `mark_seen` then `typing_on` | The indicator appears when a person would start typing |
| 2–3 phrasings per case | Consecutive leads never see identical text |
| Separate after-hours copy | A cheerful instant reply at 2 AM is obviously automated |
| **One reply, then handoff** | It never gets a second turn, so it never sounds robotic |

The delay is capped under two minutes on purpose. Sellers message several
buyers at once and the first real response usually holds the conversation —
looking unhurried is not worth losing the lead. `test_delay_stays_under_two_minutes`
pins this.

### Setup

1. `AUTOREPLY_ENABLED=false` in `config/.env` records messages without replying.
   Leave it false until App Review passes.
2. Expose the webhook over HTTPS (ngrok for testing, a real host for production).
3. Facebook App → **Webhooks → Page** → callback URL `https://<host>/webhook`,
   verify token = `FB_VERIFY_TOKEN`, subscribe to **messages**.
4. Submit `pages_messaging` for App Review. They require a screencast of the
   flow. Until it passes, replies work only for people with a role on the app.
5. Flip `AUTOREPLY_ENABLED=true`.

Every delivery is signature-verified against the app secret
(`X-Hub-Signature-256`); without that anyone who learns the URL could forge
inbound messages and make the Page reply to strangers.

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

- `config/.env` holds live credentials and is gitignored. Never commit it.
- Dashboard auth is a plaintext comparison against an env var — fine for a local
  POC, not for anything internet-facing. Put it behind real auth before exposing it.
- `FB_USER_PASSWORD` is only read by the outreach module. Leave it blank.
- The SQLite file is unencrypted and holds prospect personal data. Treat it as
  sensitive and delete it when the POC concludes.
