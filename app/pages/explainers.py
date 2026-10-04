"""Branded explainer pages: the real, stable URLs that Facebook's native tabs
can't hold on their own.

Facebook's Services tab and Featured tiles don't support a custom link or an
embedded video per item (confirmed against the Graph API and the Page UI
directly - see project notes). The fix here is a small set of our own pages,
one per item the client asked for, each with a placeholder video slot ready
to swap for the real file once it's supplied ("we will work on content
side"). Facebook elements that only support a single destination link (the
Website field, a CTA button, a Featured tile) point at one of these.

Not a separate project: this is served from the same FastAPI app as the
Messenger webhook, so it ships on whatever permanent domain that ends up
hosted on.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

BRAND_NAME = "Elite Homes USA"
BRAND_TAGLINE = "Your Home, Our Expertise, Elite Results"
BRAND_PHONE = "[PHONE]"
FACEBOOK_URL = "https://www.facebook.com/profile.php?id=1005218406001595"


@dataclass
class Explainer:
    slug: str
    title: str
    # Shown while no video has been supplied yet - keeps the page useful on
    # its own, not just an empty placeholder.
    fallback_text: str
    video_url: str | None = None  # set once the client supplies a file/link


HOW_IT_WORKS = Explainer(
    "how-it-works",
    "How It Works",
    "1. Schedule your free assessment - pick a time that works for you. "
    "2. Get a fair cash offer - we buy as-is, no repairs or cleaning. "
    "3. Close on your date - you pick the timeline, we handle the rest.",
)

MEET_THE_TEAM = Explainer(
    "meet-the-team",
    "Meet The Team",
    "The people behind Elite Homes USA - local, real, and here to help "
    "Jacksonville homeowners move forward.",
)

WHERE_WE_BUY = Explainer(
    "where-we-buy",
    "Where We Buy",
    "We buy throughout Jacksonville, FL and the surrounding counties.",
)

FAQ: list[Explainer] = [
    Explainer(
        "what-is-free-assessment", "What is the free assessment?",
        "We look at your house and its condition, then give you a cash offer. No cost, no obligation.",
    ),
    Explainer(
        "how-do-i-schedule", "How do I schedule it?",
        f"Tap the booking button on our page and pick a time, or call {BRAND_PHONE}.",
    ),
    Explainer(
        "do-i-have-to-fix-anything", "Do I have to fix anything?",
        "No. We buy houses as-is. No repairs, no cleaning, no showings.",
    ),
    Explainer(
        "do-i-pay-commissions", "Do I pay commissions?",
        "No agent commissions. We buy direct.",
    ),
    Explainer(
        "how-fast-can-you-close", "How fast can you close?",
        "As soon as you need, or on whatever date works for you.",
    ),
    Explainer(
        "what-houses-do-you-buy", "What houses do you buy?",
        "Any condition, any situation: inherited, needs repairs, or you're just ready to move on.",
    ),
    Explainer(
        "where-do-you-buy", "Where do you buy?",
        "We buy throughout Jacksonville, FL and the surrounding counties.",
    ),
    Explainer(
        "do-i-have-to-accept", "Do I have to accept?",
        "Never. The assessment and offer are free, and the choice is always yours.",
    ),
]

SERVICES: list[Explainer] = [
    Explainer("sell-as-is", "Sell As-Is", "No repairs, no cleanout, no showings. Sell your house exactly as it is."),
    Explainer("inherited-probate", "Inherited & Probate", "We work with the family and the title company to make an inherited property simple to sell."),
    Explainer("behind-on-payments", "Behind On Payments", "Options before foreclosure - we can move quickly to help."),
    Explainer("divorce-relocation", "Divorce Or Relocation", "A quick, simple sale on your timeline."),
    Explainer("tired-landlords", "Tired Landlords", "We buy rentals, tenants in place is okay."),
    Explainer("code-violations-liens", "Code Violations & Liens", "We handle the hard ones - violations and liens included."),
]

_ALL: dict[str, Explainer] = {
    e.slug: e
    for e in [HOW_IT_WORKS, MEET_THE_TEAM, WHERE_WE_BUY, *FAQ, *SERVICES]
}


def _page(title: str, body: str) -> str:
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
  .wrap {{ max-width: 640px; margin: 0 auto; padding: 24px 16px 48px; }}
  .brand {{ display:flex; align-items:center; gap:10px; margin-bottom: 28px; }}
  .brand img {{ width: 40px; height: 40px; border-radius: 8px; object-fit: cover; }}
  .brand span {{ font-weight: 800; font-size: 18px; letter-spacing: 0.3px; }}
  h1 {{ font-size: 28px; margin: 0 0 16px; }}
  .video {{
    aspect-ratio: 16/9; border-radius: 14px; background: #1b1b1b;
    display:flex; align-items:center; justify-content:center; color:#aaa;
    font-size: 14px; margin-bottom: 20px; text-align:center; padding: 16px;
  }}
  .video .play {{ font-size: 40px; display:block; margin-bottom:8px; }}
  p.copy {{ font-size: 16px; line-height: 1.55; color:#333; }}
  .cta {{
    display:inline-block; margin-top: 24px; background: var(--red); color:#fff;
    text-decoration:none; font-weight:700; padding: 14px 22px; border-radius: 10px;
  }}
  footer {{ margin-top: 40px; font-size: 13px; color:#666; border-top:1px solid #ddd; padding-top:16px; }}
  footer a {{ color: var(--blue); text-decoration:none; }}
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
    {BRAND_TAGLINE} &middot; {BRAND_PHONE}<br>
    <a href="{FACEBOOK_URL}">Message us on Facebook</a>
  </footer>
</div>
</body>
</html>"""


def _render_explainer(e: Explainer) -> str:
    if e.video_url:
        video_block = (
            f'<div class="video" style="background:#000;">'
            f'<video controls style="width:100%;height:100%;border-radius:14px;" src="{e.video_url}"></video>'
            f"</div>"
        )
    else:
        video_block = (
            '<div class="video"><span><span class="play">&#9658;</span>'
            "Video coming soon</span></div>"
        )
    body = f"<h1>{e.title}</h1>{video_block}<p class='copy'>{e.fallback_text}</p>"
    return _page(e.title, body)


@router.get("/explainers/{slug}", response_class=HTMLResponse)
async def explainer_page(slug: str) -> str:
    e = _ALL.get(slug)
    if e is None:
        return _page("Not found", "<h1>Page not found</h1><p class='copy'>That link isn't set up yet.</p>")
    return _render_explainer(e)


@router.get("/schedule", response_class=HTMLResponse)
async def schedule_page() -> str:
    # Placeholder until a booking tool (e.g. Calendly) is chosen - see
    # project notes. Swap this for a redirect to the real booking URL then.
    body = (
        "<h1>Schedule Your Free Assessment</h1>"
        "<p class='copy'>Online booking is being set up. "
        f"In the meantime, call or message us and we'll find a time that works.</p>"
        f"<a class='cta' href='{FACEBOOK_URL}'>Message Us</a>"
    )
    return _page("Schedule", body)


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
    <h1>{BRAND_NAME}</h1>
    <p class="copy">We buy houses in Jacksonville, FL - any condition, cash offer.</p>
    <a class="cta" href="/schedule">Schedule Your Free Assessment</a>
    <h2 style="margin-top:32px;font-size:18px;">How It Works</h2>
    {_row([HOW_IT_WORKS])}
    <h2 style="margin-top:24px;font-size:18px;">Common Questions</h2>
    {_row(FAQ)}
    <h2 style="margin-top:24px;font-size:18px;">What We Help With</h2>
    {_row(SERVICES)}
    <h2 style="margin-top:24px;font-size:18px;">More</h2>
    {_row([MEET_THE_TEAM, WHERE_WE_BUY])}
    """
    return _page("Home", body)
