"""Group member and post scraping for prospect discovery.

  COMPLIANCE NOTICE
  -----------------
  Scraping Facebook group members or posts with an automated browser violates
  Meta's Terms of Service and, depending on what is collected, may also engage
  data-protection obligations around personal data collected without consent.
  The extraction functions below are therefore left unimplemented.

  What IS implemented here is the part that stays useful under any sourcing
  method: segment classification by keyword, and normalising a raw prospect
  record into an OutreachProspect row with de-duplication. Point those at a
  compliant source (see README) and the rest of the pipeline works unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select

from app.config import TEMPLATES_FILE
from app.database.db import session_scope
from app.database.models import OutreachProspect, ProspectStatus, Segment
from app.outreach.session import NotImplementedByDesign

logger = logging.getLogger(__name__)


@dataclass
class RawProspect:
    """A prospect as discovered, before it is persisted."""

    name: str
    profile_url: str
    source_group: str | None = None
    post_text: str | None = None


def _load_segment_keywords() -> dict[Segment, list[str]]:
    """Read the per-segment keyword lists from config/message_templates.json."""
    if not TEMPLATES_FILE.exists():
        logger.warning("Templates file missing: %s", TEMPLATES_FILE)
        return {}

    payload = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    keywords: dict[Segment, list[str]] = {}
    for name, block in payload.get("segments", {}).items():
        try:
            keywords[Segment(name)] = [k.lower() for k in block.get("keywords", [])]
        except ValueError:
            logger.warning("Unknown segment %r in templates file, ignoring", name)
    return keywords


def classify_segment(text: str) -> tuple[Segment | None, str | None]:
    """Bucket free text into a segment by keyword match.

    Returns (segment, matched_keyword). The segment with the most keyword hits
    wins; ties break toward the earliest-defined segment.
    """
    if not text:
        return None, None

    haystack = text.lower()
    best: tuple[Segment, str, int] | None = None

    for segment, keywords in _load_segment_keywords().items():
        hits = [kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", haystack)]
        if not hits:
            continue
        if best is None or len(hits) > best[2]:
            best = (segment, hits[0], len(hits))

    if best is None:
        return None, None
    return best[0], best[1]


def save_prospects(raw: list[RawProspect]) -> int:
    """Classify and persist prospects, skipping ones already in the database.

    Returns the number of new rows inserted.
    """
    inserted = 0

    with session_scope() as session:
        for item in raw:
            existing = session.scalar(
                select(OutreachProspect)
                .where(OutreachProspect.profile_url == item.profile_url)
                .limit(1)
            )
            if existing is not None:
                logger.debug("Prospect already known: %s", item.profile_url)
                continue

            segment, keyword = classify_segment(item.post_text or "")
            if segment is None:
                logger.debug("No segment match for %s, skipping", item.name)
                continue

            session.add(
                OutreachProspect(
                    name=item.name,
                    profile_url=item.profile_url,
                    source_group=item.source_group,
                    keyword_match=keyword,
                    segment=segment,
                    status=ProspectStatus.PENDING,
                )
            )
            inserted += 1

    logger.info("Saved %d new prospect(s) out of %d candidate(s)", inserted, len(raw))
    return inserted


def import_prospects_from_csv(csv_path: str) -> int:
    """Import prospects from a CSV you sourced compliantly.

    Expected columns: name, profile_url, source_group (optional),
    post_text (optional, used for segment classification).

    This is the supported ingestion path for the POC.
    """
    import csv

    raw: list[RawProspect] = []
    with open(csv_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("name") or not row.get("profile_url"):
                continue
            raw.append(
                RawProspect(
                    name=row["name"].strip(),
                    profile_url=row["profile_url"].strip(),
                    source_group=(row.get("source_group") or "").strip() or None,
                    post_text=(row.get("post_text") or "").strip() or None,
                )
            )

    logger.info("Read %d row(s) from %s", len(raw), csv_path)
    return save_prospects(raw)


# --- Unimplemented by design ----------------------------------------------


def scrape_group_members(group_url: str, limit: int = 100) -> list[RawProspect]:
    """NOT IMPLEMENTED - scraping group members violates Meta's Terms."""
    raise NotImplementedByDesign(
        "Group member scraping is not implemented: it violates Meta's Terms of "
        "Service (Automated Data Collection) and collects personal data without "
        "consent. Use import_prospects_from_csv() with a compliantly sourced "
        "list, or see the README section 'Outreach: compliant alternatives'."
    )


def scrape_group_posts(group_url: str, limit: int = 100) -> list[RawProspect]:
    """NOT IMPLEMENTED - scraping group posts violates Meta's Terms."""
    raise NotImplementedByDesign(
        "Group post scraping is not implemented, for the same reason as "
        "scrape_group_members(). See the README."
    )
