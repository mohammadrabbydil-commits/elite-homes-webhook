"""Throttled outreach execution.

The rate limiting, daily quota, audit logging, and dry-run pipeline here are
fully implemented - they are what keeps any outreach campaign from looking like
a bot, whatever channel actually delivers the message.

`run_campaign(dry_run=True)` is the default and works end to end today: it
selects prospects, renders their messages, and writes OutreachLog rows without
contacting anyone. Live sending raises NotImplementedByDesign from
messenger.send_message(); see that module's compliance notice.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.database.db import session_scope
from app.database.models import (
    ActionResult,
    ActionType,
    OutreachLog,
    OutreachProspect,
    ProspectStatus,
    Segment,
    utcnow,
)
from app.outreach.messenger import render_message
from app.outreach.session import NotImplementedByDesign

logger = logging.getLogger(__name__)


@dataclass
class CampaignResult:
    """Summary of one campaign run."""

    attempted: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = True
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "LIVE"
        return (
            f"[{mode}] attempted={self.attempted} sent={self.sent} "
            f"failed={self.failed} skipped={self.skipped}"
        )


def sent_today() -> int:
    """Count outreach actions logged as sent since midnight UTC."""
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(OutreachLog)
            .where(
                OutreachLog.result == ActionResult.SENT,
                OutreachLog.timestamp >= start_of_day,
            )
        ) or 0


def remaining_quota() -> int:
    """How many more messages the daily limit still allows."""
    return max(0, settings.outreach_daily_limit - sent_today())


def _throttle_delay() -> float:
    """A randomised, human-paced gap between actions."""
    low = settings.outreach_min_delay_seconds
    high = max(low, settings.outreach_max_delay_seconds)
    return random.uniform(low, high)


def select_prospects(limit: int, segment: Segment | None = None) -> list[int]:
    """IDs of pending prospects to contact, oldest first."""
    with session_scope() as session:
        query = select(OutreachProspect.id).where(
            OutreachProspect.status == ProspectStatus.PENDING
        )
        if segment is not None:
            query = query.where(OutreachProspect.segment == segment)
        return list(
            session.scalars(query.order_by(OutreachProspect.created_at).limit(limit)).all()
        )


def _log_action(
    session,
    prospect_id: int,
    result: ActionResult,
    details: str | None = None,
    action: ActionType = ActionType.MESSAGE,
) -> None:
    session.add(
        OutreachLog(
            prospect_id=prospect_id,
            action_type=action,
            timestamp=utcnow(),
            result=result,
            details=details,
        )
    )


def run_campaign(
    limit: int | None = None,
    segment: Segment | None = None,
    dry_run: bool = True,
    throttle: bool = True,
) -> CampaignResult:
    """Run one throttled outreach pass.

    Args:
        limit: Max prospects to process. Defaults to the remaining daily quota.
        segment: Restrict to one segment.
        dry_run: When True (the default) messages are rendered and logged but
            never delivered. When False, delivery is attempted and will raise
            NotImplementedByDesign unless you have supplied a compliant channel.
        throttle: Sleep between actions. Disable only in tests.

    Returns:
        CampaignResult with counts and, in dry-run mode, the rendered messages.
    """
    quota = remaining_quota()
    if quota <= 0:
        logger.warning(
            "Daily outreach limit of %d already reached, nothing to do",
            settings.outreach_daily_limit,
        )
        return CampaignResult(dry_run=dry_run)

    batch_size = min(limit or quota, quota)
    prospect_ids = select_prospects(batch_size, segment)

    result = CampaignResult(dry_run=dry_run)
    if not prospect_ids:
        logger.info("No pending prospects match the criteria")
        return result

    logger.info(
        "Starting %s campaign: %d prospect(s), quota %d remaining today",
        "dry-run" if dry_run else "LIVE",
        len(prospect_ids),
        quota,
    )

    for index, prospect_id in enumerate(prospect_ids):
        with session_scope() as session:
            prospect = session.get(OutreachProspect, prospect_id)
            if prospect is None or prospect.status != ProspectStatus.PENDING:
                result.skipped += 1
                continue

            result.attempted += 1

            try:
                rendered = render_message(prospect)
            except Exception as exc:
                logger.error("Could not render message for prospect %d: %s", prospect_id, exc)
                _log_action(session, prospect_id, ActionResult.FAILED, f"render error: {exc}")
                result.failed += 1
                continue

            if dry_run:
                result.messages.append(f"-> {prospect.name}: {rendered.body}")
                _log_action(session, prospect_id, ActionResult.SENT, "dry run - not delivered")
                result.sent += 1
                logger.info("[dry run] Would message %s", prospect.name)
            else:
                try:
                    from app.outreach.messenger import send_message

                    send_message(prospect, rendered.body)
                except NotImplementedByDesign as exc:
                    _log_action(session, prospect_id, ActionResult.BLOCKED, str(exc))
                    result.failed += 1
                    logger.error("Live send blocked: %s", exc)
                    break
                except Exception as exc:
                    _log_action(session, prospect_id, ActionResult.FAILED, str(exc))
                    result.failed += 1
                    logger.error("Send failed for %s: %s", prospect.name, exc)
                    continue

                prospect.status = ProspectStatus.CONTACTED
                prospect.contacted_at = utcnow()
                prospect.message_sent = rendered.body
                _log_action(session, prospect_id, ActionResult.SENT)
                result.sent += 1

        # Pause between prospects, but not after the last one.
        if throttle and index < len(prospect_ids) - 1:
            delay = _throttle_delay()
            logger.debug("Throttling %.0fs before next prospect", delay)
            time.sleep(delay)

    logger.info(result.summary())
    return result


def mark_no_response(after_days: int = 5) -> int:
    """Flip stale CONTACTED prospects to NO_RESPONSE. Returns the count."""
    cutoff = utcnow() - timedelta(days=after_days)
    with session_scope() as session:
        stale = session.scalars(
            select(OutreachProspect).where(
                OutreachProspect.status == ProspectStatus.CONTACTED,
                OutreachProspect.contacted_at.is_not(None),
                OutreachProspect.contacted_at <= cutoff,
            )
        ).all()
        for prospect in stale:
            prospect.status = ProspectStatus.NO_RESPONSE
        count = len(stale)

    if count:
        logger.info("Marked %d prospect(s) as no_response", count)
    return count
