"""Streamlit dashboard for the Elite Homes USA automation POC.

Run with:  streamlit run app/dashboard/app.py

Pages:
    Overview   - queue health, token status, daily outreach quota
    Posts      - the scheduled post queue, plus manual scheduling
    Analytics  - reach / engagement / clicks over time
    Outreach   - prospect pipeline and the outreach audit log
"""

from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

# Streamlit runs this file as a script, so the project root is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database.db import init_db, session_scope  # noqa: E402
from app.database.models import (  # noqa: E402
    OutreachLog,
    OutreachProspect,
    PostAnalytics,
    PostStatus,
    PostType,
    ProspectStatus,
    ScheduledPost,
)

st.set_page_config(page_title="Elite Homes USA - Automation", page_icon="", layout="wide")


# --- Auth ------------------------------------------------------------------


def check_login() -> bool:
    """Gate the dashboard behind the configured username/password."""
    if st.session_state.get("authenticated"):
        return True

    st.title("Elite Homes USA")
    st.caption("Automation dashboard")

    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        if (
            username == settings.dashboard_username
            and password == settings.dashboard_password
        ):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    return False


# --- Data helpers ----------------------------------------------------------


@st.cache_data(ttl=30)
def load_posts() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.scalars(
            select(ScheduledPost).order_by(ScheduledPost.scheduled_time.desc())
        ).all()
        return pd.DataFrame(
            [
                {
                    "id": r.id,
                    "type": r.post_type.value,
                    "status": r.status.value,
                    "scheduled_time": r.scheduled_time,
                    "published_at": r.published_at,
                    "fb_post_id": r.fb_post_id,
                    "retries": r.retry_count,
                    "content": r.content,
                    "last_error": r.last_error,
                }
                for r in rows
            ]
        )


@st.cache_data(ttl=30)
def load_analytics() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.scalars(
            select(PostAnalytics).order_by(PostAnalytics.fetched_at)
        ).all()
        return pd.DataFrame(
            [
                {
                    "post_id": r.post_id,
                    "reach": r.reach,
                    "engagement": r.engagement,
                    "clicks": r.clicks,
                    "fetched_at": r.fetched_at,
                }
                for r in rows
            ]
        )


@st.cache_data(ttl=30)
def load_prospects() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.scalars(
            select(OutreachProspect).order_by(OutreachProspect.created_at.desc())
        ).all()
        return pd.DataFrame(
            [
                {
                    "id": r.id,
                    "name": r.name,
                    "segment": r.segment.value,
                    "status": r.status.value,
                    "source_group": r.source_group,
                    "keyword": r.keyword_match,
                    "contacted_at": r.contacted_at,
                    "profile_url": r.profile_url,
                }
                for r in rows
            ]
        )


@st.cache_data(ttl=30)
def load_outreach_logs() -> pd.DataFrame:
    with session_scope() as session:
        rows = session.scalars(
            select(OutreachLog).order_by(OutreachLog.timestamp.desc()).limit(500)
        ).all()
        return pd.DataFrame(
            [
                {
                    "id": r.id,
                    "prospect_id": r.prospect_id,
                    "action": r.action_type.value,
                    "result": r.result.value,
                    "timestamp": r.timestamp,
                    "details": r.details,
                }
                for r in rows
            ]
        )


# --- Pages -----------------------------------------------------------------


def page_overview() -> None:
    st.header("Overview")

    posts = load_posts()
    prospects = load_prospects()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Posts queued", int((posts["status"] == "pending").sum()) if not posts.empty else 0)
    col2.metric("Published", int((posts["status"] == "published").sum()) if not posts.empty else 0)
    col3.metric("Failed", int((posts["status"] == "failed").sum()) if not posts.empty else 0)
    col4.metric("Prospects", len(prospects))

    st.divider()
    st.subheader("Configuration")

    missing = settings.missing_graph_settings()
    if missing:
        st.warning(
            "Graph API is not fully configured. Missing: "
            + ", ".join(missing)
            + ". Copy `config/.env.example` to `config/.env` and fill these in."
        )
    else:
        st.success("Graph API settings are present.")

    if st.button("Check token status"):
        from app.facebook.auth import FacebookAuthError, verify_token

        try:
            info = verify_token()
        except FacebookAuthError as exc:
            st.error(str(exc))
        else:
            if info.is_valid:
                window = "never expires" if info.never_expires else f"{info.days_remaining} days left"
                st.success(f"Token valid ({info.token_type}, {window})")
                if info.scopes:
                    st.caption("Scopes: " + ", ".join(info.scopes))
            else:
                st.error(f"Token invalid: {info.error}")

    st.divider()
    st.subheader("Outreach quota")
    from app.outreach.worker import remaining_quota, sent_today

    used, remaining = sent_today(), remaining_quota()
    st.progress(
        min(1.0, used / settings.outreach_daily_limit) if settings.outreach_daily_limit else 0.0,
        text=f"{used} of {settings.outreach_daily_limit} sent today ({remaining} remaining)",
    )


def page_posts() -> None:
    st.header("Post queue")

    with st.expander("Schedule a new post"):
        with st.form("new_post"):
            content = st.text_area("Content", height=180)
            col1, col2 = st.columns(2)
            post_type = col1.selectbox("Segment", [t.value for t in PostType])
            media_url = col2.text_input("Image URL (optional)")
            col3, col4 = st.columns(2)
            date = col3.date_input("Date", value=datetime.now().date())
            clock = col4.time_input("Time", value=dtime(9, 0))

            if st.form_submit_button("Add to queue"):
                if not content.strip():
                    st.error("Content cannot be empty.")
                else:
                    with session_scope() as session:
                        session.add(
                            ScheduledPost(
                                content=content,
                                media_url=media_url.strip() or None,
                                post_type=PostType(post_type),
                                scheduled_time=datetime.combine(date, clock),
                                status=PostStatus.PENDING,
                            )
                        )
                    st.cache_data.clear()
                    st.success("Post queued.")
                    st.rerun()

    if st.button("Seed the 3 sample posts"):
        from app.scheduler.tasks import load_sample_posts

        count = load_sample_posts()
        st.cache_data.clear()
        st.success(f"Seeded {count} post(s).") if count else st.info("Sample posts already queued.")
        st.rerun()

    posts = load_posts()
    if posts.empty:
        st.info("No posts in the queue yet.")
        return

    status_filter = st.multiselect(
        "Filter by status", [s.value for s in PostStatus], default=[s.value for s in PostStatus]
    )
    filtered = posts[posts["status"].isin(status_filter)]

    st.dataframe(
        filtered[["id", "type", "status", "scheduled_time", "published_at", "retries", "content"]],
        use_container_width=True,
        hide_index=True,
    )

    failures = filtered[filtered["last_error"].notna()]
    if not failures.empty:
        st.subheader("Recent errors")
        for _, row in failures.iterrows():
            st.error(f"Post {row['id']}: {row['last_error']}")


def page_analytics() -> None:
    st.header("Analytics")

    analytics = load_analytics()
    if analytics.empty:
        st.info(
            "No analytics recorded yet. Metrics are collected hourly for posts "
            "published in the last 30 days."
        )
        return

    latest = analytics.sort_values("fetched_at").groupby("post_id").last().reset_index()

    col1, col2, col3 = st.columns(3)
    col1.metric("Total reach", int(latest["reach"].sum()))
    col2.metric("Total engagement", int(latest["engagement"].sum()))
    col3.metric("Total clicks", int(latest["clicks"].sum()))

    st.subheader("Over time")
    pivot = analytics.pivot_table(
        index="fetched_at", values=["reach", "engagement", "clicks"], aggfunc="sum"
    )
    st.line_chart(pivot)

    st.subheader("Latest snapshot per post")
    st.dataframe(latest, use_container_width=True, hide_index=True)


def page_outreach() -> None:
    st.header("Outreach")

    st.info(
        "Automated scraping and unsolicited DM sending are not implemented in "
        "this POC - they violate Meta's Terms of Service. Prospects are imported "
        "from a CSV and campaigns run in dry-run mode, which renders and logs "
        "messages without contacting anyone. See the README.",
        icon=None,
    )

    prospects = load_prospects()

    if prospects.empty:
        st.warning("No prospects yet. Import a CSV with app.outreach.scraper.import_prospects_from_csv().")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Pending", int((prospects["status"] == "pending").sum()))
        col2.metric("Contacted", int((prospects["status"] == "contacted").sum()))
        col3.metric("Replied", int((prospects["status"] == "replied").sum()))

        segment_filter = st.multiselect(
            "Segment", sorted(prospects["segment"].unique()), default=list(prospects["segment"].unique())
        )
        st.dataframe(
            prospects[prospects["segment"].isin(segment_filter)],
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.subheader("Dry-run campaign")
    limit = st.number_input("Prospects to process", min_value=1, max_value=100, value=5)
    if st.button("Run dry-run campaign"):
        from app.outreach.worker import run_campaign

        result = run_campaign(limit=int(limit), dry_run=True, throttle=False)
        st.cache_data.clear()
        st.success(result.summary())
        for message in result.messages:
            st.text(message)

    st.divider()
    st.subheader("Outreach log")
    logs = load_outreach_logs()
    if logs.empty:
        st.caption("No outreach actions logged yet.")
    else:
        st.dataframe(logs, use_container_width=True, hide_index=True)


# --- Entry point -----------------------------------------------------------


def main() -> None:
    init_db()

    if not check_login():
        return

    st.sidebar.title("Elite Homes USA")
    st.sidebar.caption("Jacksonville, FL")
    choice = st.sidebar.radio("Go to", ["Overview", "Posts", "Analytics", "Outreach"])

    if st.sidebar.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

    if st.sidebar.button("Sign out"):
        st.session_state["authenticated"] = False
        st.rerun()

    {
        "Overview": page_overview,
        "Posts": page_posts,
        "Analytics": page_analytics,
        "Outreach": page_outreach,
    }[choice]()


main()
