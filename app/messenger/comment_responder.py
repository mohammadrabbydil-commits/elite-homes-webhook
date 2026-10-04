"""Comment auto-reply: a public reply plus a private-message DM.

When someone comments one of the trigger keywords on a Page post, this posts
a public reply under their comment and sends them a private-reply DM to open
a Messenger thread. That DM uses the same opening question as the seller
question-intake flow (see `app.messenger.responder`), and marks the resulting
Conversation at the same stage the flow would - so if the person replies in
Messenger, it continues straight into the existing question flow rather than
needing separate logic.

Requires `pages_manage_engagement` (comment replies) in addition to
`pages_messaging` (private replies) - neither is granted yet, so this is
built and tested ahead of that, the same way the Messenger flow was built
ahead of `pages_messaging` approval.
"""

from __future__ import annotations

import json
import logging
import random
import re

from sqlalchemy import select

from app.config import TEMPLATES_FILE, settings
from app.database.db import session_scope
from app.database.models import (
    Conversation,
    ConversationStatus,
    FlowStage,
    Intent,
    Message,
    MessageDirection,
    utcnow,
)
from app.messenger.api import reply_to_comment, send_private_reply

logger = logging.getLogger(__name__)


def _load_comment_reply_config() -> dict:
    if not TEMPLATES_FILE.exists():
        raise FileNotFoundError(f"Templates file not found: {TEMPLATES_FILE}")
    payload = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    return payload.get("comment_reply", {})


def matches_keyword(text: str) -> bool:
    """Whether a comment contains one of the configured trigger words."""
    if not text:
        return False
    keywords = _load_comment_reply_config().get("keywords", [])
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in keywords)


def handle_inbound_comment(
    comment_id: str,
    commenter_id: str,
    text: str,
    commenter_name: str | None = None,
) -> dict:
    """Process one inbound comment: reply publicly, then open a Messenger DM.

    Idempotent on `comment_id` (via a synthetic Message.mid), same reasoning
    as the Messenger webhook: redelivered events must never double-reply.
    """
    dedupe_mid = f"comment:{comment_id}"

    with session_scope() as session:
        if session.scalar(select(Message).where(Message.mid == dedupe_mid).limit(1)):
            logger.info("Comment %s already handled, ignoring redelivery", comment_id)
            return {"handled": False, "reason": "duplicate webhook delivery"}

        if not settings.autoreply_enabled:
            return {"handled": False, "reason": "auto-reply disabled in config"}

        if commenter_id == settings.fb_page_id:
            return {"handled": False, "reason": "comment is from the Page itself"}

        if not matches_keyword(text):
            return {"handled": False, "reason": "no trigger keyword"}

        conversation = session.scalar(
            select(Conversation).where(Conversation.psid == commenter_id).limit(1)
        )
        is_new = conversation is None
        if is_new:
            conversation = Conversation(
                psid=commenter_id, name=commenter_name, status=ConversationStatus.NEW
            )
            session.add(conversation)
            session.flush()
        elif conversation.status in {ConversationStatus.HUMAN_HANDLED, ConversationStatus.CLOSED}:
            # Already a human's conversation - a comment keyword hit shouldn't
            # reopen it with an automated reply.
            return {"handled": False, "reason": f"status is {conversation.status.value}"}

        conversation_id = conversation.id
        first_name = (commenter_name or "").split()[0] if commenter_name else None

    config = _load_comment_reply_config()
    public_text = random.choice(config.get("public_replies") or ["Thanks for the comment!"])
    private_text = (config.get("private_reply") or "Thanks for commenting!").format(
        first_name=first_name or "there"
    )

    public_result = reply_to_comment(comment_id, public_text)
    private_result = send_private_reply(comment_id, private_text)

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return {"handled": False, "reason": "conversation vanished"}

        conversation.last_message_at = utcnow()
        session.add(
            Message(
                conversation_id=conversation.id,
                mid=dedupe_mid,
                direction=MessageDirection.OUTBOUND,
                text=f"[public comment reply] {public_text}",
                is_auto=True,
                timestamp=utcnow(),
            )
        )

        if private_result.success:
            # Opens exactly where the Messenger flow's greeting would leave
            # off, so a reply in Messenger continues straight into it.
            conversation.intent = Intent.SELL
            conversation.stage = FlowStage.AWAITING_ADDRESS
            conversation.status = ConversationStatus.AUTO_REPLIED
            session.add(
                Message(
                    conversation_id=conversation.id,
                    mid=private_result.message_id,
                    direction=MessageDirection.OUTBOUND,
                    text=private_text,
                    is_auto=True,
                    timestamp=utcnow(),
                )
            )
            logger.info("Private reply opened for commenter %s", commenter_id)
        else:
            conversation.status = ConversationStatus.AWAITING_HUMAN
            logger.error(
                "Private reply failed for comment %s: %s", comment_id, private_result.error
            )

    return {
        "handled": True,
        "public_reply_sent": public_result.success,
        "private_reply_sent": private_result.success,
    }
