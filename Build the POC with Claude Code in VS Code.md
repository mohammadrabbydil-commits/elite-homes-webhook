# Build the POC with Claude Code in VS Code

## Step 0 — Set Up Claude Code in VS Code

Before you start prompting, you need Claude Code running inside VS Code. Here's the quick setup.

### Install Claude Code Extension

Open VS Code → go to the Extensions panel (Ctrl+Shift+X) → search for "Claude Code" → install the official Anthropic extension. Once installed, you'll see a Claude icon in your sidebar. Click it to open the Claude Code panel.

Sign in with your Anthropic account or API key when prompted.

### Create Your Project Folder

Open a terminal in VS Code and run:

```
mkdir elite-homes-poc
cd elite-homes-poc
code .
```

This opens a fresh VS Code window in your project folder. Claude Code can now see your entire workspace and create, edit, and run files directly.

### How This Workflow Goes

You'll give Claude Code one detailed prompt per phase. It will scaffold files, write code, install packages, and even run tests. After each prompt, you review what it built, test it manually, fix anything that's off, and then move to the next prompt.

The five prompts below are designed to be pasted one at a time, in order. Don't paste them all at once — each one builds on what the previous one created. Wait for Claude Code to finish each phase before moving to the next.

### One Important Rule

After Claude Code finishes each prompt, always do a quick manual test before moving on. Run the code, check the output, make sure it works. If something's broken, tell Claude Code what went wrong and let it fix it before you proceed. The prompts are written to produce working code, but real-world APIs and browser automation always have edge cases.

## Prompt 1 — Project Scaffolding & Facebook API Setup

Copy and paste this entire prompt into Claude Code. It will create the full folder structure, install all dependencies, set up the config, and build the Facebook API wrapper.

---

**PASTE THIS INTO CLAUDE CODE:**

```
I'm building a Facebook automation POC for a real estate client called "Elite Homes USA" based in Jacksonville, FL. Their Facebook Page name is "Elite Homes USA" and they have a personal profile called "El Homes". I need you to scaffold the entire project and build the Facebook API integration layer.

Here's what I need you to do:

1. CREATE THE PROJECT STRUCTURE:

elite-homes-poc/
├── app/
│   ├── __init__.py
│   ├── config.py              # Load env vars, settings
│   ├── database/
│   │   ├── __init__.py
│   │   ├── models.py           # SQLAlchemy models
│   │   └── db.py               # SQLite connection & session
│   ├── facebook/
│   │   ├── __init__.py
│   │   ├── auth.py             # Token management, long-lived token exchange
│   │   └── poster.py           # Graph API posting functions
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── tasks.py            # APScheduler jobs for posting
│   ├── outreach/
│   │   ├── __init__.py
│   │   ├── session.py          # Playwright Facebook login & cookie manager
│   │   ├── scraper.py          # Group member/post scraper
│   │   ├── messenger.py        # Personalized message sender
│   │   └── worker.py           # Throttled outreach execution
│   └── dashboard/
│       ├── __init__.py
│       └── app.py              # Streamlit multi-page dashboard
├── data/
│   └── posts.json              # The 3 sample posts pre-loaded
├── logs/
├── config/
│   ├── .env.example            # Template with all required env vars
│   └── message_templates.json  # Outreach message templates
├── tests/
│   ├── test_poster.py
│   ├── test_database.py
│   └── test_scheduler.py
├── requirements.txt
├── README.md
└── run.py                      # Entry point

2. INSTALL DEPENDENCIES (create requirements.txt and install):
- fastapi, uvicorn
- sqlalchemy
- apscheduler
- requests
- playwright
- streamlit
- python-dotenv
- pandas (for dashboard data)

Run: pip install -r requirements.txt
Run: python -m playwright install chromium

3. CREATE THE DATABASE MODELS (SQLite for POC):

- ScheduledPost: id, content (text), media_url (nullable), post_type (enum: homeowner/buyer/partner), scheduled_time (datetime), status (enum: pending/published/failed/retry), fb_post_id (nullable), published_at (nullable), retry_count (int default 0), created_at
- OutreachProspect: id, name, profile_url, source_group, keyword_match, segment (enum: homeowner/buyer/partner), status (enum: pending/contacted/replied/declined/no_response), contacted_at (nullable), message_sent (nullable text), created_at
- OutreachLog: id, prospect_id (FK), action_type (enum: message/friend_request/page_invite), timestamp, result (enum: sent/failed/blocked), details (nullable text)
- PostAnalytics: id, post_id (FK), reach (int), engagement (int), clicks (int), fetched_at

Create the database initialization in db.py with create_all.

4. BUILD THE FACEBOOK API WRAPPER (app/facebook/):

- auth.py: Functions to exchange a short-lived token for a long-lived one using the Graph API. Function to verify a token is still valid. Store token in .env and load it.
- poster.py: Function publish_post(message, image_url=None, scheduled_time=None) that posts to the page via Graph API. If scheduled_time is provided, use Facebook's scheduled post feature (published=False + scheduled_publish_time). Function get_post_metrics(post_id) that fetches reach, engagement, clicks from the Graph API. Include proper error handling and logging.

5. PRE-LOAD THE 3 SAMPLE POSTS in data/posts.json:

Post 1 (homeowner segment):
"Thinking about selling your Jacksonville property?\n\nWhether it needs repairs, is sitting vacant, or is a rental you're ready to sell, Elite Homes USA Group would like to hear from you.\n\nMessage us \"SELL\" with the property address and when you'd like to sell."

Post 2 (buyer segment):
"Buying investment properties in Jacksonville?\n\nElite Homes USA Group is expanding our buyer list. Let us know what you're looking for, whether it's a renovation project or your next rental.\n\nMessage us \"BUY\" with your preferred areas, budget, property type, and how much renovation you're willing to take on."

Post 3 (partner segment):
"Have a Jacksonville property under contract or a deal you're looking to move?\n\nElite Homes USA Group is connecting with wholesalers, agents, and investors on local opportunities.\n\nMessage us \"PARTNER\" with the property address, asking price, condition, and your role in the deal."

6. CREATE .env.example with these variables:
FB_APP_ID=
FB_APP_SECRET=
FB_PAGE_ACCESS_TOKEN=
FB_PAGE_ID=
FB_USER_EMAIL=
FB_USER_PASSWORD=
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=elite2024
DATABASE_URL=sqlite:///./elite_homes.db

7. CREATE a README.md with setup instructions.

8. Create a simple test in tests/test_poster.py that mocks the Graph API call and verifies the post function sends the correct payload.

After creating everything, run the tests to make sure the database initializes and the mock API test passes.
```

---

### After Claude Code Finishes

Check that you see the full folder structure in your VS Code file explorer. Open `.env.example`, copy it to `.env`, and fill in the real Facebook credentials. Then run the tests Claude Code created to make sure everything initializes.

If anything fails, tell Claude Code: "The test \[test name\] failed with this error: \[paste error\]. Fix it."

## Prompt 2 — Trial 1: Scheduled Posting Engine

Paste this after Prompt 1 is complete and tested.

---

**PASTE THIS INTO CLAUDE CODE:**

```
Now build the scheduled posting engine. This is Trial 1 of the POC — the client needs to see their 3 sample posts get scheduled and published automatically to their Facebook page.

Here's what I need:

1. BUILD THE SCHEDULER (app/scheduler/tasks.py):

- Use APScheduler (BackgroundScheduler) to manage scheduled posts.
- Create a function load_and_schedule_posts() that:
  - Reads posts from data/posts.json
  - For each post, checks if it's already in the database (avoid duplicates)
  - Inserts new posts into the ScheduledPost table with status='pending'
  - Calls the Facebook Graph API to schedule each post using Facebook's native scheduled_publish_time parameter
  - Updates the database with the fb_post_id and status='published' on success
  - On failure: logs the error, sets status='retry', increments retry_count
  - Retry logic: if retry_count < 3, try again after 60 seconds. If retry_count >= 3, set status='failed'

- Create a function check_published_posts() that runs every 5 minutes:
  - Queries all posts where scheduled_time has passed and status is still 'pending'
  - Verifies on Facebook (via Graph API) whether the post actually went live
  - Updates status to 'published' with the actual published_at timestamp
  - Fetches initial engagement metrics (reach, likes, comments) and stores them in PostAnalytics

2. CREATE A CLI COMMAND to schedule posts:

- In run.py, add a command: python run.py schedule
  - This loads posts.json, schedules all 3 posts, and prints a summary
  - Show: post text (first 50 chars), scheduled time, status, fb_post_id

- Add another command: python run.py status
  - Shows the current status of all posts in the database

3. CREATE A SCHEDULE CONFIG in data/posts.json:

Update the posts.json to include schedule times. Space them out realistically:
- Post 1 (homeowner): tomorrow at 9:00 AM EST
- Post 2 (buyer): tomorrow at 12:30 PM EST
- Post 3 (partner): tomorrow at 4:00 PM EST

Use ISO 8601 format for the times. Calculate the actual datetime based on today's date.

4. ADD LOGGING:

- Set up Python logging to write to both console and logs/posting.log
- Log every API call, every status change, every error with timestamps
- Format: [2024-01-15 09:00:03] INFO: Post scheduled successfully - fb_id: 12345, type: homeowner

5. CREATE A TEST:

- test_scheduler.py: Test that load_and_schedule_posts reads the JSON, creates DB records, and handles API failures gracefully (mock the Graph API to return an error on the second post, verify retry logic kicks in)

6. IMPORTANT EDGE CASES TO HANDLE:

- Token expiration: before each API call, verify the token is still valid. If expired, log a clear error message telling the user to refresh it.
- Rate limiting: if Facebook returns a rate limit error (code 4 or 32), wait 5 minutes and retry.
- If scheduled_time is in the past, skip the post and log a warning.
- Facebook requires scheduled_publish_time to be at least 10 minutes in the future and no more than 75 days. Validate this before calling the API.

After building everything, run: python run.py schedule --dry-run (create a dry-run flag that does everything except actually call the Facebook API, for testing).

Then run the actual tests.
```

---

### After Claude Code Finishes

Run `python run.py schedule --dry-run` first. You should see all 3 posts loaded with "DRY RUN" status. Check the logs/posting.log file.

Once dry-run works, fill in the real Facebook credentials in `.env` and run `python run.py schedule` for real. Watch the Facebook page to see if the posts are scheduled correctly.

Then run `python run.py status` to confirm all posts show as scheduled in the database.

**Pro tip:** After the posts are scheduled on Facebook, go to the Elite Homes USA page → Publishing Tools → Scheduled Posts to visually confirm they're queued. Screenshot this for the client.

## Prompt 3 — Trial 2: Outreach Engine

Paste this after Trial 1 is working and the 3 posts are successfully scheduled on Facebook.

---

**PASTE THIS INTO CLAUDE CODE:**

```
Now build the outreach engine. This is Trial 2 of the POC — the client needs to see us find real prospects from Jacksonville real estate Facebook groups and send them personalized messages.

The Facebook Graph API does NOT support sending DMs to strangers or friend requests, so we'll use Playwright browser automation for the outreach actions.

Here's what I need:

1. FACEBOOK SESSION MANAGER (app/outreach/session.py):

- Use Playwright (async) to manage a persistent Facebook browser session
- On first run: log in with email/password from .env, handle any 2FA prompts by pausing and asking for manual input, save cookies to config/fb_cookies.json
- On subsequent runs: load cookies from file, check if session is still valid by navigating to facebook.com and checking for the logged-in state
- If cookies are expired: re-login and save new cookies
- Use headless=False during development (so I can see what's happening), switch to headless=True for production
- Add random User-Agent rotation from a list of 5 common desktop browser user agents
- Add realistic viewport sizes (1280x720, 1366x768, 1920x1080 — pick randomly)

2. GROUP SCRAPER (app/outreach/scraper.py):

- Create a function scrape_group_members(group_url, max_profiles=20) that:
  - Navigates to a Facebook group URL
  - Scrolls through recent posts (last 7 days)
  - Extracts profile names and URLs of people who posted or commented
  - Filters for keyword matches from this list: "selling", "sold", "inherited", "probate", "vacant", "landlord", "tenant", "investor", "cash buyer", "wholesale", "contractor", "flip", "rental", "foreclosure", "tax lien", "FSBO", "for sale by owner", "eviction", "property management"
  - Deduplicates against existing prospects in the database
  - Categorizes each prospect into a segment: homeowner, buyer, or partner (based on which keywords matched)
  - Stores each prospect in the OutreachProspect table
  - Returns a summary: total found, by segment, new vs. already known

- Create a function discover_groups(search_terms, location="Jacksonville FL") that:
  - Searches Facebook for groups matching the search terms
  - Returns group name, URL, member count, and activity level
  - Use these search terms from the client's brief:
    - "Jacksonville real estate investing"
    - "Northeast Florida wholesale properties"
    - "Jacksonville fix and flip"
    - "Jacksonville landlord"
    - "Jacksonville for sale by owner"
    - "Clay County real estate"

3. MESSAGE PERSONALIZATION (config/message_templates.json):

Create 3 message templates, one per segment. Keep them short, natural, and non-spammy:

Homeowner template:
"Hi {first_name}, I came across your post in {group_name}. My team at Elite Homes USA buys properties as-is in the Jacksonville area — no repairs needed, no fees, and we can close on your timeline. If you've been thinking about selling, we'd love to have a conversation. Just message us 'SELL' whenever you're ready."

Buyer/Investor template:
"Hey {first_name}, saw you're active in {group_name}. We're building our buyer list at Elite Homes USA for Jacksonville investment properties — rehabs, rentals, and off-market deals. If you're looking to buy, message us 'BUY' with what you're looking for and we'll keep you in the loop."

Partner template:
"Hi {first_name}, noticed you in {group_name}. Elite Homes USA is looking to connect with local professionals on Jacksonville deals. Whether you're wholesaling, listing, or building, we'd love to explore working together. Message us 'PARTNER' if you're open to it."

4. OUTREACH WORKER (app/outreach/worker.py):

- Create a function run_outreach_batch(max_messages=15) that:
  - Pulls pending prospects from the database (status='pending')
  - For each prospect:
    - Opens their profile page in Playwright
    - Clicks the "Message" button
    - Types the personalized message (using the correct template for their segment)
    - Sends it
    - Waits a random delay between 45-120 seconds before the next one
    - Every 3-4 messages, takes a longer break of 5-15 minutes
    - Logs the action to OutreachLog (timestamp, result, message text)
    - Updates prospect status to 'contacted' and stores contacted_at and message_sent
  
  - SAFETY CONTROLS (critical):
    - Maximum 15 messages per batch, hard cap
    - If Facebook shows any CAPTCHA, warning, or "you're going too fast" message: STOP immediately, log the warning, set a cooldown flag for 24 hours
    - If a profile doesn't have a Message button: skip and log as 'skipped - no message button'
    - If the message fails to send: log as 'failed' and continue to next prospect
    - Track total daily actions across all batches — never exceed 20 actions per day for the POC
    - Add human-like typing simulation: type each character with a random delay of 30-80ms

5. CLI COMMANDS (add to run.py):

- python run.py discover-groups — finds and lists relevant groups
- python run.py scrape --group-url "URL" — scrapes prospects from a group
- python run.py outreach --dry-run — shows what would be sent without sending
- python run.py outreach — runs the actual outreach batch
- python run.py prospects — lists all prospects and their statuses

6. LOGGING:

- Log everything to logs/outreach.log
- Format: [timestamp] ACTION: prospect_name | segment | group | result | message_preview
- Log safety events prominently: [timestamp] WARNING: Facebook showed CAPTCHA — stopping outreach

7. CREATE TESTS:

- test_scraper.py: Mock a Facebook group page HTML and verify the scraper extracts profiles correctly
- test_messenger.py: Verify message template personalization fills in correctly
- test_worker.py: Verify throttling logic (delays are within range, batch caps work, daily limits enforced)

Build all of this. After creation, run the tests. Then run: python run.py discover-groups --dry-run to verify the group discovery logic works.
```

---

### After Claude Code Finishes

This is the most complex piece. Test in this order:

First, run `python run.py discover-groups` to find real Jacksonville real estate groups. Pick 2–3 active ones and note their URLs.

Second, run `python run.py scrape --group-url "[paste a group URL]"` with `headless=False` so you can watch Playwright navigate the group and extract profiles. Make sure Facebook doesn't throw any warnings.

Third, run `python run.py outreach --dry-run` to see the personalized messages that would be sent. Review them — make sure the templates look natural and the right segment template is matched to each prospect.

Fourth, and only when everything above works, run `python run.py outreach` to send a real batch of 10–15 messages. Watch the browser as it runs. If anything looks off, Ctrl+C to stop immediately.

**Critical:** If Facebook shows any warning during scraping or outreach, stop immediately. Tell Claude Code: "Facebook showed a warning \[describe it\]. Add detection for this specific warning and make the system stop automatically when it appears."

## Prompt 4 — Dashboard & Analytics

Paste this after both trials are working and you have real data in the database.

---

**PASTE THIS INTO CLAUDE CODE:**

```
Now build the Streamlit dashboard that ties everything together. The client needs to see a clean, professional interface showing their scheduled posts, outreach activity, and analytics — all with real data from the database.

Here's what I need:

1. MULTI-PAGE STREAMLIT APP (app/dashboard/app.py):

Create a Streamlit app with a sidebar navigation and 3 pages. Use st.set_page_config with page_title="Elite Homes USA - Automation Dashboard", layout="wide", and a house emoji as the page icon.

Add a simple login gate at the top: username/password check against DASHBOARD_USERNAME and DASHBOARD_PASSWORD from .env. Use st.session_state to persist the login across page navigation. Show a clean login form, not a raw text input.

2. PAGE 1 — CONTENT CALENDAR (pages/1_Content_Calendar.py):

- Header: "Content Calendar" with the Elite Homes USA branding
- Summary cards at top row (use st.columns): Total Posts Scheduled, Posts Published, Posts Pending, Posts Failed — each in a st.metric card with delta indicators
- Main section: a table (st.dataframe) showing all posts from the ScheduledPost table with columns:
  - Post Type (homeowner/buyer/partner) with colored badges
  - Content (first 80 characters with ... truncation)
  - Scheduled Time (formatted nicely)
  - Status (color-coded: green=published, yellow=pending, red=failed, blue=retry)
  - Facebook Link (clickable link to the live post, or "—" if not yet published)
  - Published At
- Below the table: a "Schedule New Post" expander with a form:
  - Text area for post content
  - Dropdown for post type (homeowner/buyer/partner)
  - Date picker and time picker for schedule time
  - Optional image URL input
  - Submit button that inserts into the database and calls the scheduling function

3. PAGE 2 — OUTREACH DASHBOARD (pages/2_Outreach.py):

- Header: "Outreach Dashboard"
- Summary cards (st.columns): Total Prospects, Messages Sent, Replies Received, Response Rate (percentage), Pending Outreach
- Filter bar: dropdown to filter by segment (All/Homeowner/Buyer/Partner), dropdown to filter by status (All/Pending/Contacted/Replied/No Response)
- Main table (st.dataframe) showing OutreachProspect records:
  - Name
  - Segment (with colored badge)
  - Source Group
  - Keyword Match
  - Status (color-coded)
  - Contacted At
  - Message Sent (expandable)
- Below: "Outreach Log" expander showing the OutreachLog records in reverse chronological order with timestamp, prospect name, action type, and result

4. PAGE 3 — ANALYTICS (pages/3_Analytics.py):

- Header: "Analytics Overview"
- Top row: key metrics — Total Reach, Total Engagement, Outreach Acceptance Rate, Audience Growth
- Chart 1 (st.bar_chart or plotly): Post Performance — reach and engagement for each published post, grouped by post type
- Chart 2 (st.line_chart or plotly): Outreach Activity Over Time — messages sent per day as a line, replies per day as another line
- Chart 3 (plotly pie chart): Prospect Breakdown by Segment — pie chart showing homeowner vs buyer vs partner distribution
- Chart 4 (st.bar_chart): Outreach Response Rate by Segment — which segment responds best
- Use Plotly for the charts if it makes them look more professional. Install plotly if not already installed.

5. STYLING:

- Add custom CSS via st.markdown with unsafe_allow_html=True to:
  - Use a clean color scheme: navy (#1a365d) for headers, green (#38a169) for success, amber (#d69e2e) for pending, red (#e53e3e) for failed
  - Clean up the default Streamlit padding
  - Style the metric cards to look polished
  - Add the Elite Homes USA logo/name in the sidebar header area

6. DATA LAYER:

- Create app/dashboard/data.py with functions that query the SQLite database and return pandas DataFrames for each page
- Functions: get_all_posts(), get_post_metrics(), get_all_prospects(segment=None, status=None), get_outreach_stats(), get_daily_outreach_activity()
- Use caching (st.cache_data with ttl=60) so the dashboard doesn't hammer the database on every interaction

7. CLI COMMAND:

- Add to run.py: python run.py dashboard — starts the Streamlit app
- The command should run: streamlit run app/dashboard/app.py --server.port 8501

Build everything. Make sure it connects to the existing SQLite database with real data from Trial 1 and Trial 2. After building, start the dashboard and verify all 3 pages render with real data.
```

---

### After Claude Code Finishes

Run `python run.py dashboard` and check all three pages. Make sure the Content Calendar shows your 3 real posts with correct statuses. Make sure the Outreach Dashboard shows your real prospects and message logs. Make sure the Analytics page has charts with actual data.

If any page looks broken or empty, tell Claude Code: "Page \[name\] shows \[describe the problem\]. The data exists in the database — here's what I see when I run \[query\]. Fix the data connection."

Once the dashboard is working locally, deploy it so the client gets a real URL. Tell Claude Code:

"Deploy this Streamlit app to Streamlit Community Cloud. Create a requirements.txt with all dependencies, make sure the database file is included, and set it up for deployment. If Streamlit Cloud won't work with SQLite, switch to a free-tier cloud database alternative and update the connection."

Or if you prefer: "Create a Dockerfile for this Streamlit app so I can deploy it to DigitalOcean / Railway / Render."

## Prompt 5 — Testing, Polish & Demo Package

Paste this once the dashboard is running with real data.

---

**PASTE THIS INTO CLAUDE CODE:**

```
Final phase — let's polish everything, run full tests, and create a demo package for the client.

1. FULL TEST SUITE:

Create/update the test suite to cover the complete system:

- test_poster.py: test Graph API posting (mocked), test token validation, test retry logic, test scheduled_time validation (past dates, too far in future)
- test_scheduler.py: test post loading from JSON, test duplicate prevention, test status transitions, test the check_published_posts monitoring job
- test_scraper.py: test profile extraction from mocked group HTML, test keyword matching, test deduplication, test segment categorization
- test_messenger.py: test all 3 message templates with sample data, test placeholder replacement, test edge cases (missing first name, no group name)
- test_worker.py: test throttle delays are within range (45-120s), test batch cap enforcement, test daily limit tracking, test cooldown flag behavior
- test_dashboard_data.py: test all data query functions return correct pandas DataFrames, test filtering logic

Run all tests and make sure they pass. Fix any failures.

2. GENERATE DEMO DATA (if needed):

If the real outreach hasn't produced enough data to make the dashboard look good, create a script data/seed_demo_data.py that:
- Adds 15-20 realistic prospect records with varied statuses (some contacted, some replied, some pending)
- Adds outreach log entries with realistic timestamps spread over the past 3 days
- Adds post analytics with realistic reach/engagement numbers
- Uses realistic Jacksonville names, group names, and timestamps
- Mark these as demo data so they can be cleared later

Run this ONLY if the real data is sparse. Real data is always better.

3. SYSTEM HEALTH CHECK SCRIPT:

Create a script check_health.py that verifies:
- Database is accessible and has data
- Facebook token is valid (test API call)
- Playwright can launch and load Facebook
- Dashboard starts without errors
- All scheduled tasks are in the correct state

Print a clean report: ✓ Database: 3 posts, 18 prospects, 12 outreach logs | ✓ Facebook API: token valid, expires 2024-03-15 | ✓ Browser: Playwright ready | ✓ Dashboard: running on port 8501

4. README OVERHAUL:

Update README.md with:
- Project overview (1 paragraph)
- Quick start guide (5 steps max)
- Architecture diagram (ASCII art is fine)
- Command reference: all available CLI commands with descriptions
- Environment variables reference
- Troubleshooting section (common issues and fixes)

5. CREATE A DEMO SCRIPT:

Create demo/run_demo.sh (or .bat for Windows) that:
- Checks .env is configured
- Runs health check
- Starts the dashboard
- Opens the browser to the dashboard URL
- Prints: "Elite Homes USA Automation Dashboard is running at http://localhost:8501"

6. FINAL CLEANUP:

- Remove any debug print statements
- Make sure all log files are in the logs/ directory
- Ensure .env is in .gitignore (never commit credentials)
- Add proper docstrings to all major functions
- Make sure the code follows PEP 8 formatting

Run the full test suite one final time, then run the health check, then start the dashboard.
```

---

### After Claude Code Finishes

Run `python check_health.py` and make sure everything shows green. Start the dashboard one final time and click through every page to verify it works smoothly.

Now you're ready to present to the client. Record a 3–5 minute screen recording walking through the dashboard: show the Content Calendar with their 3 published posts, show the Outreach Dashboard with the real prospects and messages, and show the Analytics page. Send the video along with the live dashboard URL and the results summary to the client.

## Tips for Working with Claude Code

A few things that will make this build go much smoother.

**Don't paste all 5 prompts at once.** Each prompt builds on the previous one. Wait for Claude Code to finish, test the output, and fix any issues before moving to the next. If you paste Prompt 3 before Prompt 1's code is stable, you'll end up with a mess.

**When something breaks, give Claude Code the exact error.** Don't say "it doesn't work." Say: "When I run `python run.py schedule`, I get this error: \[paste the full traceback\]. Fix it." The more specific the error, the faster the fix.

**Use "keep the existing code, just add/fix X" phrasing.** If Claude Code rewrites a file you already tested and broke something that was working, tell it: "You broke the publish\_post function when you updated poster.py. Revert that function to the previous version and only change \[the specific thing\]." This keeps working code stable.

**Test the Facebook API manually first.** Before running any automation, open a browser, go to the Graph API Explorer (developers.facebook.com/tools/explorer), and manually test posting to the page. Make sure the token works and you have the right permissions. If the manual test fails, no amount of code will fix it — it's a permissions issue.

**Watch the browser during outreach.** Always run the outreach with `headless=False` during development. Watch what Playwright does. If it clicks the wrong button, navigates to the wrong page, or Facebook's UI has changed, you'll see it immediately. Tell Claude Code: "Playwright is clicking \[wrong element\]. The correct button is \[describe it\]. Update the selector."

**Commit your code after each working prompt.** After each prompt's code is tested and working, run `git init` (first time) and `git add . && git commit -m "Phase X complete"`. This gives you a rollback point if a later prompt breaks something.

**If Facebook blocks anything during the POC:** Don't panic. Stop all automation. Wait 24 hours. Then tell Claude Code: "Facebook temporarily blocked outreach. Add a 24-hour cooldown system that prevents any outreach actions for 24 hours after a block, and add a warning check at the start of every outreach run." The client will respect that you prioritized their account safety over speed.

**The order matters:** Prompt 1 (scaffolding) → Prompt 2 (posting trial) → show client the posts went live → Prompt 3 (outreach trial) → show client the outreach results → Prompt 4 (dashboard) → Prompt 5 (polish) → send the full demo.
