"""Florida ZIP code lookup for the Messenger agent.

Backed by the client's Florida ZIP/city/county reference list
(`data/florida_zip_codes.csv`). It's a recognition aid, not an eligibility
list: Elite Homes covers all of Florida, so a ZIP missing from it never means
"we can't help". A ZIP's county is the ZIP's usual association only - it
doesn't verify which county a specific property is actually in.

The agent states a city or county for a ZIP only from this lookup, never from
the model's own guess.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import DATA_DIR

ZIP_FILE = DATA_DIR / "florida_zip_codes.csv"

_MAX_MENTIONS = 3

_FIVE_DIGITS = re.compile(r"(?<![\d$])(\d{5})(?:-\d{4})?(?!\d)")

# "13579 Beach Blvd" - a five-digit house number, not a ZIP.
_STREET_AFTER = re.compile(
    r"\s+(?:[NSEW]\.?\s+)?(?:[\w.'#]+\s+){0,3}"
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|court|way|"
    r"pl|place|cir|circle|ter|terrace|pkwy|parkway|hwy|highway|trl|trail|loop|path)\b",
    re.IGNORECASE,
)
# "12000 sq ft", "95000 dollars" - a size or an amount.
_QUANTITY_AFTER = re.compile(r"\s*(?:k\b|sq|square|sf\b|ft\b|feet|dollars|usd)", re.IGNORECASE)
_AMOUNT_BEFORE = re.compile(
    r"(?:asking|price|priced|arv|offer|pay|paid|owe|owed|for|usd)\s*:?\s*$", re.IGNORECASE
)
_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)
# What usually sits right before a ZIP: a state, the word "zip", or a comma.
_ZIP_CONTEXT_BEFORE = re.compile(
    rf"(?:\b(?:{_STATE_CODES})\.?|florida|zip(?:\s*code)?(?:\s*is)?|,)\s*:?\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class ZipInfo:
    zip_code: str
    city: str
    county: str
    state: str = "FL"


@dataclass(frozen=True)
class ZipMention:
    zip_code: str
    info: ZipInfo | None
    in_florida_range: bool


@lru_cache(maxsize=1)
def _reference() -> dict[str, ZipInfo]:
    with ZIP_FILE.open(encoding="utf-8", newline="") as fh:
        return {
            row["zip"]: ZipInfo(row["zip"], row["city"], row["county"], row["state"])
            for row in csv.DictReader(fh)
        }


def lookup_florida_zip(zip_code: str) -> ZipInfo | None:
    """The reference entry for a ZIP, or None if it isn't in the list."""
    return _reference().get(zip_code.strip()[:5])


def is_florida_zip_range(zip_code: str) -> bool:
    """USPS assigns the 320-349 prefixes to Florida; 340 is military mail
    (APO/FPO AA), not a place."""
    prefix = int(zip_code[:3])
    return 320 <= prefix <= 349 and prefix != 340


def find_zip_mentions(text: str) -> list[ZipMention]:
    """ZIP codes mentioned in a message, each with its lookup result.

    Skips five-digit numbers that are clearly something else: a house number
    followed by a street name, a price, or a square footage. A number outside
    Florida's range only counts when it sits where a ZIP usually does (after
    a state or the word "zip"), so an unrelated number never gets reported as
    an out-of-state ZIP.
    """
    if not text:
        return []

    mentions: list[ZipMention] = []
    seen: set[str] = set()
    for match in _FIVE_DIGITS.finditer(text):
        zip_code = match.group(1)
        before = text[: match.start()]
        after = text[match.end():]
        if zip_code in seen:
            continue
        if _STREET_AFTER.match(after) or _QUANTITY_AFTER.match(after) or _AMOUNT_BEFORE.search(before):
            continue

        in_florida = is_florida_zip_range(zip_code)
        standalone = not text.replace(match.group(0), "").strip(" .,!?")
        if not in_florida and not (standalone or _ZIP_CONTEXT_BEFORE.search(before)):
            continue

        seen.add(zip_code)
        mentions.append(
            ZipMention(
                zip_code=zip_code,
                info=lookup_florida_zip(zip_code) if in_florida else None,
                in_florida_range=in_florida,
            )
        )
        if len(mentions) == _MAX_MENTIONS:
            break
    return mentions


def describe_mention(mention: ZipMention) -> str:
    """One line of fact for the agent's prompt about a mentioned ZIP."""
    if mention.info:
        return (
            f"ZIP {mention.zip_code}: {mention.info.city}, {mention.info.county}, FL. "
            "That's the ZIP's usual area from our reference list; it doesn't verify "
            "the property's exact county."
        )
    if mention.in_florida_range:
        return (
            f"ZIP {mention.zip_code}: a Florida ZIP that isn't in our reference list. "
            "It's still in our service area (all of Florida), but don't name a city or "
            "county for it; ask what city it's in if that matters."
        )
    return (
        f"ZIP {mention.zip_code}: not a Florida ZIP code, so the property may be outside "
        "Florida. Don't guess its city; confirm where the property is."
    )
