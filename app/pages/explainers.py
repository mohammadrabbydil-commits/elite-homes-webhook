"""Branded explainer pages: the real, stable URLs that Facebook's native tabs
can't hold on their own.

Facebook's Services tab and Featured tiles don't support a custom link or an
embedded video per item (confirmed against the Graph API and the Page UI
directly - see project notes). The fix is a small set of our own pages, one
per item the client asked for, each wired to a real demo video as a
proof-of-concept - every DEMO_VIDEO_URL below is a placeholder clip ("we'll
switch it out with our own"), clearly labelled as a demo in the UI so it's
never mistaken for real content.

Not a separate project: this is served from the same FastAPI app as the
Messenger webhook, so it ships on whatever permanent domain that ends up
hosted on.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

BRAND_NAME = "Elite Homes USA"
BRAND_TAGLINE = "Your Home, Our Expertise, Elite Results"
BRAND_PHONE = "[PHONE]"
BRAND_ADDRESS = "[ADDRESS - provided once confirmed]"
BUSINESS_HOURS = "9am - 5pm"
FACEBOOK_URL = "https://www.facebook.com/profile.php?id=1005218406001595"

# Placeholder POC clip (CC0, hosted by MDN) used everywhere a "demo video of
# your choice" was asked for. Swap per-item once real footage exists.
DEMO_VIDEO_URL = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"

# Booking link - set once a tool (Calendly recommended) is created. Until
# then /schedule falls back to a Message Us button.
# NOTE: this is a 30-min Calendly event; the brief asked for a 15-min slot.
# Swap the URL below once a 15-min event type is created - no other changes needed.
BOOKING_URL: str | None = "https://calendly.com/mohammadrabby-dil/30min"


@dataclass
class Explainer:
    slug: str
    title: str
    fallback_text: str
    duration_label: str  # e.g. "20 sec" - shown next to the Click Video link
    video_url: str | None = DEMO_VIDEO_URL


MEET_THE_TEAM = Explainer(
    "meet-the-team",
    "Meet The Team",
    "The people behind Elite Homes USA - local, real, and here to help "
    "Jacksonville homeowners move forward.",
    "45-60 sec",
)

# No hyperlink per the client's latest direction - stays as the static
# graphic they already designed (Logos/photo_2026-10-05_00-04-13.jpg), not a
# linked video page. Kept here only so the route doesn't 404 if referenced.
HOW_IT_WORKS = Explainer(
    "how-it-works",
    "How It Works",
    "1. Schedule your free assessment - pick a time that works for you. "
    "2. Get a fair cash offer - we buy as-is, no repairs or cleaning. "
    "3. Close on your date - you pick the timeline, we handle the rest.",
    "",
    video_url=None,
)

# Also no hyperlink - plain text per the client's latest direction.
WHERE_WE_BUY = Explainer(
    "where-we-buy",
    "Where We Buy",
    "Jacksonville and surrounding counties.",
    "",
    video_url=None,
)

# Order and per-question durations match the client's "Common Questions"
# mockup exactly (total 2:15, under the 3-minute cap).
FAQ: list[Explainer] = [
    Explainer(
        "what-is-free-assessment", "What is the free assessment?",
        "We review your home and discuss a cash offer.", "20 sec",
    ),
    Explainer(
        "how-do-i-schedule", "How do I schedule it?",
        "Message our team to arrange a time.", "15 sec",
    ),
    Explainer(
        "do-i-have-to-fix-anything", "Do I have to fix anything?",
        "No, you do not have to fix anything. We buy houses as-is.", "15 sec",
    ),
    Explainer(
        "do-i-pay-commissions", "Do I pay commissions?",
        "Ask us about commissions and closing costs.", "20 sec",
    ),
    Explainer(
        "how-fast-can-you-close", "How fast can you close?",
        "We work with you on your closing timeline.", "15 sec",
    ),
    Explainer(
        "what-houses-do-you-buy", "What houses do you buy?",
        "Inherited homes, homes needing repairs, and more.", "20 sec",
    ),
    Explainer(
        "where-do-you-buy", "Where do you buy?",
        "Jacksonville, FL and surrounding counties.", "15 sec",
    ),
    Explainer(
        "do-i-have-to-accept", "Do I have to accept?",
        "No cost. No obligation. The choice is yours.", "15 sec",
    ),
]

# Services tab stays exactly as it is - plain text, no hyperlinks, per the
# client's latest direction. Kept here for the (unlinked) individual pages
# only, in case they're wanted later.
SERVICES: list[Explainer] = [
    Explainer("sell-as-is", "Sell As-Is", "No repairs, no cleanout, no showings.", "", video_url=None),
    Explainer("inherited-probate", "Inherited & Probate", "We work with the family and the title company.", "", video_url=None),
    Explainer("behind-on-payments", "Behind On Payments", "Options before foreclosure.", "", video_url=None),
    Explainer("divorce-relocation", "Divorce Or Relocation", "A quick, simple sale on your timeline.", "", video_url=None),
    Explainer("tired-landlords", "Tired Landlords", "We buy rentals, tenants in place is okay.", "", video_url=None),
    Explainer("code-violations-liens", "Code Violations & Liens", "We handle the hard ones.", "", video_url=None),
]

_ALL: dict[str, Explainer] = {
    e.slug: e for e in [HOW_IT_WORKS, MEET_THE_TEAM, WHERE_WE_BUY, *FAQ, *SERVICES]
}


def _page(title: str, body: str, wide: bool = False) -> str:
    max_width = "1040px" if wide else "640px"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | {BRAND_NAME}</title>
<style>
  :root {{ --red:#e8242a; --blue:#1f4e8c; --ink:#111; --bg:#f5f5f5; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: {max_width}; margin: 0 auto; padding: 24px 16px 48px; }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom: 20px; }}
  .brand img {{ width: 40px; height: 40px; border-radius: 8px; object-fit: cover; }}
  .brand span {{ font-weight: 800; font-size: 18px; letter-spacing: 0.3px; }}
  h1 {{ font-size: 32px; margin: 0 0 4px; font-weight: 800; }}
  h1 + .rule {{ width:110px; height:4px; border-radius:2px; margin: 10px 0 22px;
    background: linear-gradient(90deg, #1ec6e8, var(--blue)); }}
  .video {{
    aspect-ratio: 16/9; border-radius: 14px; background: #111;
    display:flex; align-items:center; justify-content:center; color:#aaa;
    font-size: 14px; margin-bottom: 14px; text-align:center; padding: 16px;
    position: relative; overflow:hidden;
  }}
  .video .play {{ font-size: 40px; display:block; margin-bottom:8px; }}
  .demo-badge {{
    position:absolute; top:10px; right:10px; background:var(--red); color:#fff;
    font-size:11px; font-weight:800; letter-spacing:0.5px; padding:4px 8px;
    border-radius:6px; z-index:2;
  }}
  p.copy {{ font-size: 16px; line-height: 1.55; color:#333; }}
  .cta {{
    display:inline-block; margin-top: 16px; background: var(--red); color:#fff;
    text-decoration:none; font-weight:700; padding: 16px 22px; border-radius: 10px;
    width: 100%; text-align:center; font-size: 17px;
  }}
  .cta span.arrow {{ float:right; }}
  footer {{ margin-top: 40px; font-size: 13px; color:#666; border-top:1px solid #ddd; padding-top:16px; }}
  footer a {{ color: var(--blue); text-decoration:none; }}
  .hours {{ font-size: 13px; color:#555; margin-top:6px; }}

  .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 560px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{
    background:#fff; border-radius:14px; padding:16px; text-decoration:none; color:inherit;
    display:block; box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  }}
  .card h3 {{ color: var(--blue); font-size:17px; margin:0 0 6px; }}
  .card .clicklink {{ color: var(--red); font-weight:700; font-size:14px; display:flex; align-items:center; gap:6px; margin-bottom:10px; }}
  .card .clicklink .dot {{ width:22px;height:22px;border-radius:50%;background:var(--red);color:#fff;
    display:inline-flex;align-items:center;justify-content:center;font-size:11px; }}
  .card .thumb {{
    aspect-ratio:16/9; border-radius:10px; background:#1b1b1b; position:relative;
    display:flex; align-items:center; justify-content:center; margin-bottom:10px;
  }}
  .card .thumb .playcircle {{
    width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,0.92);
    display:flex;align-items:center;justify-content:center;color:var(--ink);font-size:16px;
  }}
  .card .caption {{ font-size:14px; color:#444; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <img src="/static/logo.jpg" alt="{BRAND_NAME} logo">
    <span>{BRAND_NAME.upper()}</span>
  </div>
  {body}
  <footer>
    {BRAND_TAGLINE} &middot; {BRAND_PHONE}
    <div class="hours">Hours: {BUSINESS_HOURS} &middot; {BRAND_ADDRESS}</div>
    <div style="margin-top:8px;"><a href="{FACEBOOK_URL}">Message us on Facebook</a></div>
  </footer>
</div>
</body>
</html>"""


def _render_explainer(e: Explainer) -> str:
    if e.video_url:
        video_block = (
            '<div class="video" style="background:#000;">'
            '<span class="demo-badge">DEMO</span>'
            f'<video controls style="width:100%;height:100%;border-radius:14px;" src="{e.video_url}"></video>'
            "</div>"
        )
    else:
        video_block = ""
    duration = f'<p style="color:#888;font-size:13px;margin-top:-8px;">{e.duration_label} demo clip</p>' if e.duration_label else ""
    body = f"<h1>{e.title}</h1><div class='rule'></div>{video_block}{duration}<p class='copy'>{e.fallback_text}</p>"
    return _page(e.title, body)


@router.get("/explainers/{slug}", response_class=HTMLResponse)
async def explainer_page(slug: str) -> str:
    e = _ALL.get(slug)
    if e is None:
        return _page("Not found", "<h1>Page not found</h1><p class='copy'>That link isn't set up yet.</p>")
    return _render_explainer(e)


@router.get("/common-questions", response_class=HTMLResponse)
async def common_questions_page() -> str:
    """Matches the client's 'Common Questions - Educate Yourself in Under 3
    Min' layout: an 8-card grid, each linking to its own video page."""
    cards = "".join(
        f"""<a class="card" href="/explainers/{e.slug}">
          <h3>{e.title}</h3>
          <div class="clicklink"><span class="dot">&#9658;</span>Click video &middot; {e.duration_label}</div>
          <div class="thumb"><span class="demo-badge">DEMO</span><div class="playcircle">&#9658;</div></div>
          <div class="caption">{e.fallback_text}</div>
        </a>"""
        for e in FAQ
    )
    total_seconds = sum(int(e.duration_label.split()[0]) for e in FAQ)
    body = f"""
    <h1>Common Questions</h1>
    <div class="rule"></div>
    <p class="copy" style="margin-top:-10px;color:#888;font-size:14px;">
      Educate yourself in under 3 min &middot; total {total_seconds // 60}:{total_seconds % 60:02d}
    </p>
    <div class="grid">{cards}</div>
    <a class="cta" href="/schedule" style="margin-top:24px;">Schedule Your Free Assessment <span class="arrow">&rarr;</span></a>
    """
    return _page("Common Questions", body, wide=True)


@router.get("/schedule")
async def schedule_page():
    if BOOKING_URL:
        return RedirectResponse(url=BOOKING_URL, status_code=302)
    body = (
        "<h1>Schedule Your Free Assessment</h1><div class='rule'></div>"
        "<p class='copy'>Online booking is being set up. "
        "In the meantime, call or message us and we'll find a time that works.</p>"
        f"<a class='cta' href='{FACEBOOK_URL}'>Message Us</a>"
    )
    return HTMLResponse(_page("Schedule", body))


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page() -> str:
    body = f"""
    <h1>Privacy Policy</h1>
    <div class="rule"></div>
    <p class="copy"><strong>Last updated:</strong> October 2026</p>

    <p class="copy">This page explains what information Elite Homes USA (operating as
    "Elite Homes Automation" on Facebook/Meta) collects through our Facebook Page,
    Messenger, and this website, and how it is used.</p>

    <h2 style="font-size:19px;margin-top:28px;">What we collect</h2>
    <p class="copy">When you message our Facebook Page or interact with our automated
    assistant, we may collect: your name and Facebook profile information made available
    to us by Messenger, your messages to us, and - if you choose to share it while
    asking about selling a property - your property address, the property's condition,
    your timeline to sell, your reason for selling, your phone number, and your
    preferred time to be called.</p>
    <p class="copy">We do not collect this information through any other means, and we
    do not ask for it unless you message us first.</p>

    <h2 style="font-size:19px;margin-top:28px;">How we use it</h2>
    <p class="copy">Solely to respond to your inquiry and, if you are interested in
    selling a property, to have a member of our team follow up with you. We do not sell,
    rent, or share this information with third parties for marketing purposes.</p>

    <h2 style="font-size:19px;margin-top:28px;">How long we keep it</h2>
    <p class="copy">We retain conversation records for as long as reasonably necessary
    to respond to your inquiry and maintain accurate business records, and delete or
    anonymize it on request (see Contact below).</p>

    <h2 style="font-size:19px;margin-top:28px;">Facebook Platform data</h2>
    <p class="copy">Our Messenger integration uses the Facebook Messenger Platform.
    Any data we receive through it is handled according to this policy and is used only
    for the purposes described above, consistent with
    <a href="https://developers.facebook.com/devpolicy/" style="color:#1f4e8c;">Meta's
    Platform Terms</a>.</p>

    <h2 style="font-size:19px;margin-top:28px;">Your rights</h2>
    <p class="copy">You can ask us what information we hold about you, ask us to correct
    it, or ask us to delete it, at any time - see Contact below.</p>

    <h2 style="font-size:19px;margin-top:28px;">Contact</h2>
    <p class="copy">For any question about this policy or your data, message us on
    Facebook at <a href="{FACEBOOK_URL}" style="color:#1f4e8c;">Elite Homes USA</a>.</p>
    """
    return HTMLResponse(_page("Privacy Policy", body))


@router.get("/", response_class=HTMLResponse)
async def home_page() -> str:
    def _row(items: list[Explainer]) -> str:
        return "".join(
            f'<div style="padding:12px 0;border-bottom:1px solid #ddd;">'
            f'<a style="color:#1f4e8c;text-decoration:none;font-weight:600;" '
            f'href="/explainers/{e.slug}">{e.title}</a></div>'
            for e in items
        )

    body = f"""
    <h1>{BRAND_NAME}</h1><div class="rule"></div>
    <p class="copy">We buy houses in Jacksonville, FL - any condition, cash offer.</p>
    <a class="cta" href="/schedule">Schedule Your Free Assessment <span class="arrow">&rarr;</span></a>
    <h2 style="margin-top:32px;font-size:18px;">Meet The Team</h2>
    {_row([MEET_THE_TEAM])}
    <h2 style="margin-top:24px;font-size:18px;">Common Questions</h2>
    <div style="padding:12px 0;border-bottom:1px solid #ddd;">
      <a style="color:#1f4e8c;text-decoration:none;font-weight:600;" href="/common-questions">
        All 8 questions - under 3 min &rarr;</a>
    </div>
    <h2 style="margin-top:24px;font-size:18px;">Where We Buy</h2>
    <p class="copy">{WHERE_WE_BUY.fallback_text}</p>
    """
    return _page("Home", body)
