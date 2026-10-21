"""FastAPI webhook receiving Messenger events from Facebook.

Run with:  python run.py webhook

Facebook requires a 200 response within a few seconds, so message handling is
dispatched to a background thread and the endpoint returns immediately. That
also means the humanised reply delay never blocks the webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from concurrent.futures import ThreadPoolExecutor

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, settings
from app.database.db import init_db
from app.messenger import ai_agent
from app.messenger.comment_responder import handle_inbound_comment
from app.messenger.responder import handle_inbound_message
from app.pages.explainers import router as explainers_router

logger = logging.getLogger(__name__)

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="messenger")


def verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """Verify the X-Hub-Signature-256 header against the app secret.

    Without this anyone who learns the webhook URL can forge inbound messages
    and make the Page reply to strangers.
    """
    if not settings.fb_app_secret:
        logger.warning("FB_APP_SECRET is not set; cannot verify webhook signatures")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.fb_app_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


@router.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Facebook's subscription handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == settings.fb_verify_token:
        logger.info("Webhook verified by Facebook")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("Webhook verification failed (mode=%s)", mode)
    return Response(content="Verification failed", status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhook")
async def receive_event(request: Request) -> Response:
    """Receive message events and dispatch them off the request path."""
    raw = await request.body()

    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Rejected webhook delivery with a bad signature")
        return Response(content="Invalid signature", status_code=status.HTTP_403_FORBIDDEN)

    payload = await request.json()

    if payload.get("object") != "page":
        return Response(content="Ignored", status_code=status.HTTP_200_OK)

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            psid = (event.get("sender") or {}).get("id")
            message = event.get("message") or {}

            # Echoes are the Page's own messages coming back. Replying to those
            # would make the Page talk to itself.
            if not psid or message.get("is_echo"):
                continue

            text = message.get("text")
            if not text:
                logger.info("Ignoring non-text message from %s", psid)
                continue

            mid = message.get("mid")
            # Present when the person tapped a quick-reply button rather than
            # typing - `text` is still set to the button's title in that case.
            quick_reply_payload = (message.get("quick_reply") or {}).get("payload")
            logger.info("Inbound message from %s: %.60s", psid, text)
            _executor.submit(_safe_handle, psid, text, mid, quick_reply_payload)

        # Comments on posts arrive as "feed" field changes, a separate
        # structure from the "messaging" events above. Needs the Page's
        # webhook subscribed to "feed" and the pages_manage_engagement
        # permission - see app.messenger.comment_responder.
        for change in entry.get("changes", []):
            if change.get("field") != "feed":
                continue
            value = change.get("value") or {}
            if value.get("item") != "comment" or value.get("verb") != "add":
                continue

            comment_id = value.get("comment_id")
            commenter_id = (value.get("from") or {}).get("id")
            commenter_name = (value.get("from") or {}).get("name")
            comment_text = value.get("message")
            if not comment_id or not commenter_id or not comment_text:
                logger.info("Ignoring incomplete comment event: %s", value)
                continue

            logger.info("Inbound comment from %s: %.60s", commenter_id, comment_text)
            _executor.submit(
                _safe_handle_comment, comment_id, commenter_id, comment_text, commenter_name
            )

    # Always 200, even on internal failure: a non-200 makes Facebook redeliver,
    # which would replay messages already being handled.
    return Response(content="EVENT_RECEIVED", status_code=status.HTTP_200_OK)


def _safe_handle(psid: str, text: str, mid: str | None, quick_reply_payload: str | None = None) -> None:
    try:
        handle_inbound_message(psid, text, mid, quick_reply_payload=quick_reply_payload)
    except Exception:
        logger.exception("Unhandled error processing message from %s", psid)


def _safe_handle_comment(
    comment_id: str, commenter_id: str, text: str, commenter_name: str | None
) -> None:
    try:
        handle_inbound_comment(comment_id, commenter_id, text, commenter_name)
    except Exception:
        logger.exception("Unhandled error processing comment %s", comment_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not settings.autoreply_enabled:
        logger.warning(
            "AUTOREPLY_ENABLED is false: messages will be recorded but no "
            "replies sent. Set it to true in config/.env when ready."
        )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Elite Homes USA - Messenger webhook", lifespan=lifespan)
    app.include_router(router)
    app.include_router(explainers_router)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "pages" / "static")), name="static")

    @app.get("/health")
    def health() -> dict:
        import os

        return {
            "status": "ok",
            # Set by Render on every deploy, so this shows which commit is live.
            "version": (os.getenv("RENDER_GIT_COMMIT") or "local")[:7],
            "autoreply_enabled": settings.autoreply_enabled,
            "first_reply_delay_seconds": [
                settings.autoreply_first_min_delay_seconds,
                settings.autoreply_first_max_delay_seconds,
            ],
            "delay_seconds": [
                settings.autoreply_min_delay_seconds,
                settings.autoreply_max_delay_seconds,
            ],
            "ai_agent_enabled": settings.ai_agent_enabled,
            "ai_agent_configured": bool(settings.openai_api_key),
            "ai_agent_status": ai_agent.AI_STATUS,
        }

    @app.get("/followups")
    def followups_page(key: str, format: str = "html"):
        """For the team: who has gone quiet for 5+ days, with the follow-up
        message ready to copy. A person sends it from the Page inbox (Meta
        doesn't allow a bot to message after 24 hours). Shared-secret key,
        like the /debug routes."""
        import html as _html

        from app.messenger.followups import FOLLOW_UP_AFTER_DAYS, HUMAN_WINDOW_DAYS, due_followups

        if key != settings.fb_verify_token:
            return Response(content="unauthorized", status_code=status.HTTP_403_FORBIDDEN)

        rows = due_followups()
        if format == "json":
            return {
                "count": len(rows),
                "followups": [
                    {
                        "name": f.name,
                        "days_quiet": f.days_silent,
                        "days_left_to_message": round(f.days_left, 1),
                        "can_message": f.can_still_message,
                        "last_from_us": f.last_page_message,
                        "suggested_message": f.suggested_message,
                    }
                    for f in rows
                ],
            }

        inbox = f"https://business.facebook.com/latest/inbox/all?asset_id={settings.fb_page_id}"
        cards = []
        for f in rows:
            if f.can_still_message:
                tail = (
                    f"<p class='left'>{f.days_left:.1f} days left to message</p>"
                    f"<p class='msg' id='m{abs(hash(f.psid))}'>{_html.escape(f.suggested_message)}</p>"
                    f"<button onclick=\"navigator.clipboard.writeText(document.getElementById('m{abs(hash(f.psid))}').innerText)\">Copy message</button>"
                )
            else:
                tail = "<p class='left closed'>7 day window closed. Wait for them to write again.</p>"
            cards.append(
                f"<div class='card'><h3>{_html.escape(f.name)}</h3>"
                f"<p class='meta'>Quiet for {f.days_silent} days</p>"
                f"<p class='last'>Our last message: {_html.escape(f.last_page_message)}</p>{tail}</div>"
            )
        body = "".join(cards) or f"<p>Nobody is due a follow-up (nothing unanswered for {FOLLOW_UP_AFTER_DAYS}+ days).</p>"
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Follow-ups</title><style>
body{{font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:16px;background:#f4f6f9;color:#15213b}}
h1{{font-size:22px}} .card{{background:#fff;border:1px solid #dce1ea;border-radius:10px;padding:14px;margin:12px 0}}
h3{{margin:0 0 4px}} .meta,.last,.left{{margin:4px 0;color:#5a6479;font-size:14px}} .left{{color:#2c7a4b;font-weight:bold}}
.closed{{color:#b3261e}} .msg{{background:#f7f0e1;padding:10px;border-radius:8px;margin:8px 0}}
button{{background:#13254a;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:15px}}
a{{color:#13254a}}</style></head><body>
<h1>Follow-ups due</h1>
<p>People who have gone quiet for {FOLLOW_UP_AFTER_DAYS}+ days. Send each message yourself from the
<a href="{inbox}">Page inbox</a>; Meta lets a person reply for {HUMAN_WINDOW_DAYS} days after the customer's last message.</p>
{body}</body></html>"""
        return Response(content=page, media_type="text/html")

    @app.get("/debug/recent")
    def debug_recent(key: str, limit: int = 20) -> dict:
        """Temporary diagnostic: what has actually reached the webhook and
        been processed. Protected by FB_VERIFY_TOKEN as a shared secret -
        not real auth, just enough to keep it off casual discovery.
        Remove once the pages_messaging delivery issue is resolved.
        """
        if key != settings.fb_verify_token:
            return {"error": "unauthorized"}

        from app.database.db import session_scope
        from app.database.models import Conversation, Message
        from sqlalchemy import func, select

        with session_scope() as session:
            messages = session.scalars(
                select(Message).order_by(Message.timestamp.desc()).limit(limit)
            ).all()
            conversations = session.scalars(
                select(Conversation).order_by(Conversation.last_message_at.desc()).limit(limit)
            ).all()
            total = session.scalar(select(func.count()).select_from(Message))
            return {
                "message_count_total": total,
                "recent_messages": [
                    {
                        "conversation_id": m.conversation_id,
                        "mid": m.mid,
                        "direction": m.direction.value,
                        "text": m.text,
                        "is_auto": m.is_auto,
                        "timestamp": m.timestamp.isoformat(),
                    }
                    for m in messages
                ],
                "recent_conversations": [
                    {
                        "id": c.id,
                        "psid": c.psid,
                        "status": c.status.value,
                        "stage": c.stage.value,
                        "intent": c.intent.value,
                        "last_message_at": c.last_message_at.isoformat(),
                    }
                    for c in conversations
                ],
            }

    @app.get("/debug/reset")
    def debug_reset(key: str, psid: str) -> dict:
        """Reset a test conversation back to a clean, replyable state -
        testing-only convenience. Remove alongside the other /debug routes."""
        if key != settings.fb_verify_token:
            return {"error": "unauthorized"}

        from app.database.db import session_scope
        from app.database.models import Conversation, ConversationStatus, FlowStage
        from sqlalchemy import select

        with session_scope() as session:
            convo = session.scalar(select(Conversation).where(Conversation.psid == psid).limit(1))
            if convo is None:
                return {"error": "no conversation found for that psid"}
            convo.status = ConversationStatus.NEW
            convo.stage = FlowStage.AWAITING_ADDRESS
            convo.auto_replies_sent = 0
            return {"reset": True, "conversation_id": convo.id, "psid": psid}

    @app.get("/debug/token")
    def debug_token(key: str) -> dict:
        """What token is actually loaded in this running process, and is it
        valid - checked live against Facebook, not assumed."""
        if key != settings.fb_verify_token:
            return {"error": "unauthorized"}

        import requests as req

        app_token = f"{settings.fb_app_id}|{settings.fb_app_secret}"
        token_suffix = settings.fb_page_access_token[-8:] if settings.fb_page_access_token else None
        r = req.get(
            f"{settings.graph_base_url}/debug_token",
            params={"input_token": settings.fb_page_access_token, "access_token": app_token},
            timeout=15,
        )
        return {
            "token_suffix": token_suffix,
            "page_id_configured": settings.fb_page_id,
            "debug_token_response": r.json(),
        }

    @app.get("/debug/log")
    def debug_log(key: str, lines: int = 200) -> dict:
        if key != settings.fb_verify_token:
            return {"error": "unauthorized"}
        from app.config import LOG_DIR

        log_path = LOG_DIR / "elite_homes.log"
        if not log_path.exists():
            return {"error": "no log file found", "path": str(log_path)}
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": content[-lines:]}

    return app


app = create_app()
