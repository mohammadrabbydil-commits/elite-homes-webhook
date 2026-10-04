"""SQLAlchemy models for the Elite Homes USA automation POC."""

from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite stores it naive, so we normalise on write)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --- Enums -----------------------------------------------------------------


class PostType(str, enum.Enum):
    HOMEOWNER = "homeowner"
    BUYER = "buyer"
    PARTNER = "partner"


class PostStatus(str, enum.Enum):
    PENDING = "pending"
    # Handed to Facebook's own scheduler; Facebook publishes it server-side,
    # so publish_due_posts must never touch it.
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    RETRY = "retry"


class Segment(str, enum.Enum):
    HOMEOWNER = "homeowner"
    BUYER = "buyer"
    PARTNER = "partner"


class ProspectStatus(str, enum.Enum):
    PENDING = "pending"
    CONTACTED = "contacted"
    REPLIED = "replied"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"


class ActionType(str, enum.Enum):
    MESSAGE = "message"
    FRIEND_REQUEST = "friend_request"
    PAGE_INVITE = "page_invite"


class ActionResult(str, enum.Enum):
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"


class ConversationStatus(str, enum.Enum):
    NEW = "new"
    AUTO_REPLIED = "auto_replied"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_HANDLED = "human_handled"
    CLOSED = "closed"


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Intent(str, enum.Enum):
    """What a person messaging the Page appears to want."""

    SELL = "sell"
    BUY = "buy"
    PARTNER = "partner"
    UNKNOWN = "unknown"


class FlowStage(str, enum.Enum):
    """Where a seller lead is in the question intake flow.

    Each value names the question still waiting on an answer - the next
    inbound message in that conversation is treated as the answer to it.
    """

    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_CONDITION = "awaiting_condition"
    AWAITING_TIMELINE = "awaiting_timeline"
    AWAITING_REASON = "awaiting_reason"
    AWAITING_PHONE = "awaiting_phone"
    AWAITING_BEST_TIME = "awaiting_best_time"
    COMPLETE = "complete"


# --- Tables ----------------------------------------------------------------


class ScheduledPost(Base):
    """A Page post queued for publication via the Graph API."""

    __tablename__ = "scheduled_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    post_type: Mapped[PostType] = mapped_column(
        SAEnum(PostType, native_enum=False, length=20), nullable=False
    )
    scheduled_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[PostStatus] = mapped_column(
        SAEnum(PostStatus, native_enum=False, length=20),
        nullable=False,
        default=PostStatus.PENDING,
        index=True,
    )
    fb_post_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    analytics: Mapped[list["PostAnalytics"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ScheduledPost id={self.id} type={self.post_type.value} status={self.status.value}>"


class OutreachProspect(Base):
    """A person identified for outreach, with the segment they were bucketed into."""

    __tablename__ = "outreach_prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_url: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    source_group: Mapped[str | None] = mapped_column(String(512), nullable=True)
    keyword_match: Mapped[str | None] = mapped_column(String(512), nullable=True)
    segment: Mapped[Segment] = mapped_column(
        SAEnum(Segment, native_enum=False, length=20), nullable=False
    )
    status: Mapped[ProspectStatus] = mapped_column(
        SAEnum(ProspectStatus, native_enum=False, length=20),
        nullable=False,
        default=ProspectStatus.PENDING,
        index=True,
    )
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_sent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    logs: Mapped[list["OutreachLog"]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<OutreachProspect id={self.id} name={self.name!r} status={self.status.value}>"


class OutreachLog(Base):
    """An audit record of every outreach action attempted against a prospect."""

    __tablename__ = "outreach_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_prospects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[ActionType] = mapped_column(
        SAEnum(ActionType, native_enum=False, length=20), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    result: Mapped[ActionResult] = mapped_column(
        SAEnum(ActionResult, native_enum=False, length=20), nullable=False
    )
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    prospect: Mapped[OutreachProspect] = relationship(back_populates="logs")

    def __repr__(self) -> str:
        return f"<OutreachLog id={self.id} action={self.action_type.value} result={self.result.value}>"


class PostAnalytics(Base):
    """A point-in-time metrics snapshot for a published post."""

    __tablename__ = "post_analytics"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    post: Mapped[ScheduledPost] = relationship(back_populates="analytics")

    def __repr__(self) -> str:
        return f"<PostAnalytics post_id={self.post_id} reach={self.reach}>"


class Conversation(Base):
    """A Messenger thread with one person who messaged the Page first.

    Meta permits automated replies only to people who initiated contact, so a
    row here is the record that the 24-hour standard messaging window opened.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    psid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    intent: Mapped[Intent] = mapped_column(
        SAEnum(Intent, native_enum=False, length=20), nullable=False, default=Intent.UNKNOWN
    )
    status: Mapped[ConversationStatus] = mapped_column(
        SAEnum(ConversationStatus, native_enum=False, length=20),
        nullable=False,
        default=ConversationStatus.NEW,
        index=True,
    )
    property_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Seller question-intake flow (SELL-intent conversations only). `stage`
    # tracks which question is still outstanding; the fields below fill in as
    # each is answered. Never populated for buy/partner/unknown conversations.
    stage: Mapped[FlowStage] = mapped_column(
        SAEnum(FlowStage, native_enum=False, length=20),
        nullable=False,
        default=FlowStage.AWAITING_ADDRESS,
    )
    condition: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timeline: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason_for_selling: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    best_time_to_call: Mapped[str | None] = mapped_column(String(128), nullable=True)
    auto_replies_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_message_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    @property
    def window_open(self) -> bool:
        """Whether the 24-hour standard messaging window is still open."""
        return (utcnow() - self.last_message_at) < timedelta(hours=24)

    def __repr__(self) -> str:
        return f"<Conversation psid={self.psid} intent={self.intent.value} status={self.status.value}>"


class Message(Base):
    """One message in a conversation, inbound or outbound."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Facebook's message id. Webhook events are redelivered on failure, so this
    # is unique and is what makes handling idempotent.
    mid: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    direction: Mapped[MessageDirection] = mapped_column(
        SAEnum(MessageDirection, native_enum=False, length=20), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message {self.direction.value} auto={self.is_auto} {self.text[:30]!r}>"
