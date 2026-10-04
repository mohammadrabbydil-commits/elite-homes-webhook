#!/usr/bin/env python
"""Entry point for the Elite Homes USA automation POC.

Commands:
    python run.py init                      Create the database tables
    python run.py seed                      Queue the 3 sample posts
    python run.py auth --token <short>      Exchange a token, save the Page token
    python run.py verify                    Check the stored token
    python run.py post --message "..."      Publish or schedule one post now
    python run.py scheduler                 Run the posting scheduler
    python run.py dashboard                 Launch the Streamlit dashboard
    python run.py outreach --dry-run        Run a dry-run outreach campaign
    python run.py status                    Print a queue summary
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta

from app.config import BASE_DIR, configure_logging, settings


def cmd_init(args: argparse.Namespace) -> int:
    from app.database.db import init_db

    init_db()
    print(f"Database ready at {settings.database_url}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from app.database.db import init_db
    from app.scheduler.tasks import load_sample_posts

    init_db()
    count = load_sample_posts(interval_hours=args.interval_hours)
    print(f"Seeded {count} sample post(s)." if count else "Sample posts already queued.")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    from app.facebook.auth import FacebookAuthError, bootstrap_page_token

    try:
        token = bootstrap_page_token(args.token, persist=not args.no_persist)
    except FacebookAuthError as exc:
        print(f"Auth failed: {exc}", file=sys.stderr)
        return 1

    if args.no_persist:
        print(f"Page access token:\n{token}")
    else:
        print("Page access token saved to config/.env")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Remove pending posts from the queue, by id or by segment.

    Only touches posts that have not gone out. Published posts are untouched -
    delete those on the Page itself.
    """
    from sqlalchemy import select

    from app.database.db import init_db, session_scope
    from app.database.models import PostStatus, PostType, ScheduledPost
    from app.facebook.poster import delete_post

    init_db()

    with session_scope() as session:
        query = select(ScheduledPost).where(
            ScheduledPost.status.in_(
                [PostStatus.PENDING, PostStatus.RETRY, PostStatus.SCHEDULED]
            )
        )

        if args.id:
            query = query.where(ScheduledPost.id.in_(args.id))
        elif args.post_type:
            try:
                types = [PostType(t.strip()) for t in args.post_type.split(",")]
            except ValueError as exc:
                print(f"Unknown post type: {exc}", file=sys.stderr)
                return 1
            query = query.where(ScheduledPost.post_type.in_(types))
        else:
            print("Specify --id or --post-type.", file=sys.stderr)
            return 1

        doomed = session.scalars(query).all()
        if not doomed:
            print("Nothing pending matches that.")
            return 0

        print(f"Removing {len(doomed)} pending post(s):\n")
        for post in doomed:
            headline = post.content.splitlines()[0][:56]
            # A post handed to Facebook's scheduler still goes out unless it is
            # deleted there too, so only drop the row once that succeeds.
            if post.status is PostStatus.SCHEDULED and post.fb_post_id:
                if not delete_post(post.fb_post_id):
                    print(f"  [{post.id}] NOT removed, Facebook delete failed: {headline}")
                    continue
            print(f"  [{post.id}] {post.post_type.value:<10} {headline}")
            session.delete(post)

    print("\nDone. Verify with: python run.py status")
    return 0


def cmd_reschedule(args: argparse.Namespace) -> int:
    """Re-time pending posts, given a local time in the configured timezone.

    `seed` spaces posts from "now" in UTC, which rarely lands in business hours
    for the Page's audience. This sets them explicitly in local time instead.
    """
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from app.database.db import init_db, session_scope
    from app.database.models import PostStatus, ScheduledPost

    init_db()
    local_zone = ZoneInfo(settings.timezone)

    try:
        first_local = datetime.fromisoformat(args.at).replace(tzinfo=local_zone)
    except ValueError:
        print(
            f"Could not parse --at {args.at!r}. Use e.g. 2026-09-24T09:00",
            file=sys.stderr,
        )
        return 1

    with session_scope() as session:
        pending = session.scalars(
            select(ScheduledPost)
            .where(ScheduledPost.status.in_([PostStatus.PENDING, PostStatus.RETRY]))
            .order_by(ScheduledPost.scheduled_time)
        ).all()

        if not pending:
            print("No pending posts to reschedule.")
            return 0

        print(f"Rescheduling {len(pending)} post(s), timezone {settings.timezone}:\n")
        for index, post in enumerate(pending):
            local_time = first_local + timedelta(hours=args.interval_hours * index)
            post.scheduled_time = local_time.astimezone(timezone.utc).replace(tzinfo=None)
            headline = post.content.splitlines()[0][:52]
            print(
                f"  {post.post_type.value:<10} "
                f"{local_time.strftime('%a %d %b %I:%M %p %Z')}   {headline}"
            )

    print("\nDone. Verify with: python run.py status")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose why a token cannot see the Page. Prints no secrets."""
    from app.facebook.auth import (
        FacebookAuthError,
        describe_object,
        get_me,
        list_pages,
        verify_token,
    )

    print("=" * 62)
    print("TOKEN")
    print("=" * 62)

    try:
        info = verify_token(args.token)
    except FacebookAuthError as exc:
        print(f"  Could not debug the token: {exc}", file=sys.stderr)
        return 1

    if not info.is_valid:
        print(f"  INVALID: {info.error}", file=sys.stderr)
        return 1

    window = "never expires" if info.never_expires else f"{info.days_remaining} days remaining"
    print(f"  Type    : {info.token_type}")
    print(f"  Expires : {window}")
    print(f"  App ID  : {info.app_id} (expected {settings.fb_app_id})")

    scopes = set(info.scopes)
    print(f"  Scopes  : {', '.join(sorted(scopes)) if scopes else '(none reported)'}")

    needed = {"pages_show_list", "pages_manage_posts", "pages_read_engagement"}
    missing_scopes = needed - scopes
    if missing_scopes:
        print(f"  MISSING : {', '.join(sorted(missing_scopes))}")

    print()
    print("=" * 62)
    print("ACCOUNT")
    print("=" * 62)
    try:
        me = get_me(args.token)
        print(f"  Signed in as: {me.get('name')} (id {me.get('id')})")
    except FacebookAuthError as exc:
        print(f"  Could not identify the account: {exc}")

    print()
    print("=" * 62)
    print(f"TARGET OBJECT  {settings.fb_page_id}")
    print("=" * 62)
    obj = describe_object(settings.fb_page_id, args.token)
    if "error" in obj:
        print(f"  Not readable with this token: {obj['error']}")
        is_page = None
    else:
        is_page = "category" in obj
        print(f"  Name    : {obj.get('name')}")
        print(f"  Category: {obj.get('category', '(none - not a Page)')}")
        print(f"  Verdict : {'PAGE' if is_page else 'PERSONAL PROFILE, not a Page'}")

    print()
    print("=" * 62)
    print("PAGES THIS USER ADMINISTERS")
    print("=" * 62)
    try:
        pages = list_pages(args.token)
    except FacebookAuthError as exc:
        print(f"  Could not list Pages: {exc}")
        pages = []

    if pages:
        for page in pages:
            marker = "  <-- matches FB_PAGE_ID" if page["id"] == settings.fb_page_id else ""
            print(f"  {page['id']:<20} {page.get('name', '?')}{marker}")
    else:
        print("  (none)")

    print()
    print("=" * 62)
    print("DIAGNOSIS")
    print("=" * 62)

    if pages and any(p["id"] == settings.fb_page_id for p in pages):
        print("  All good. Run:  python run.py auth --token <this token>")
        return 0

    if pages:
        print("  The Page list above does not include FB_PAGE_ID.")
        print("  Update FB_PAGE_ID in config/.env to the correct ID, then re-run.")
        return 1

    if missing_scopes:
        print("  The token is missing pages_show_list, so /me/accounts returns")
        print("  nothing even if Pages exist. Regenerate the token with all three")
        print("  scopes granted.")
    elif is_page is False:
        print(f"  {settings.fb_page_id} is a PERSONAL PROFILE, not a Page.")
        print("  The Graph API cannot post to personal profiles. Elite Homes USA")
        print("  needs a real Facebook Page - see the README.")
    else:
        print("  The token carries pages_show_list but no Pages came back. Most")
        print("  likely the Page was not ticked on the Page-selection screen when")
        print("  the token was generated, or this account holds no admin role on")
        print("  the Elite Homes USA Page.")
        print("  Revoke at facebook.com/settings?tab=business_tools, then retry.")
    return 1


def cmd_set_token(args: argparse.Namespace) -> int:
    """Validate an already-issued Page token and persist it to config/.env.

    Use this when the token came from somewhere other than the short-lived
    exchange - a Business Manager System User, or the Access Token Tool.
    """
    from app.facebook.auth import FacebookAuthError, persist_token_to_env, verify_token
    from app.facebook.poster import get_post_metrics  # noqa: F401  (import check)

    try:
        info = verify_token(args.token)
    except FacebookAuthError as exc:
        print(f"Could not verify the token: {exc}", file=sys.stderr)
        return 1

    if not info.is_valid:
        print(f"Token is INVALID: {info.error}", file=sys.stderr)
        return 1

    if info.token_type and info.token_type.upper() == "USER":
        print(
            "This is a USER token, not a Page token.\n"
            "Run this instead, which exchanges it and derives the Page token:\n"
            f"    python run.py auth --token {args.token[:12]}...",
            file=sys.stderr,
        )
        return 1

    required = {"pages_manage_posts", "pages_read_engagement"}
    granted = set(info.scopes)
    if info.scopes and not required.issubset(granted):
        print(
            "Warning: token is missing "
            + ", ".join(sorted(required - granted))
            + ". Posting or metrics may fail.",
            file=sys.stderr,
        )

    persist_token_to_env(args.token)
    window = "never expires" if info.never_expires else f"{info.days_remaining} days remaining"
    print(f"Token saved to config/.env ({info.token_type}, {window})")
    if info.scopes:
        print("Scopes: " + ", ".join(info.scopes))
    return 0


def cmd_pages(args: argparse.Namespace) -> int:
    """List the Pages the token's user administers, to confirm FB_PAGE_ID."""
    from app.facebook.auth import FacebookAuthError, list_pages

    try:
        pages = list_pages(args.token)
    except FacebookAuthError as exc:
        print(f"Could not list Pages: {exc}", file=sys.stderr)
        return 1

    if not pages:
        print(
            "This user administers no Pages.\n"
            "If https://www.facebook.com/profile.php?id=61586869300206 is a personal\n"
            "profile rather than a Page, you cannot post to it with the Graph API -\n"
            "you need to create a Page, or convert the profile to one.",
            file=sys.stderr,
        )
        return 1

    print(f"{'PAGE ID':<20} NAME")
    for page in pages:
        marker = "  <-- matches FB_PAGE_ID" if page["id"] == settings.fb_page_id else ""
        print(f"{page['id']:<20} {page.get('name', '?')}{marker}")

    print("\nPut the correct ID in config/.env as FB_PAGE_ID.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from app.facebook.auth import FacebookAuthError, verify_token

    try:
        info = verify_token()
    except FacebookAuthError as exc:
        print(f"Could not verify: {exc}", file=sys.stderr)
        return 1

    if not info.is_valid:
        print(f"Token is INVALID: {info.error}", file=sys.stderr)
        return 1

    window = "never expires" if info.never_expires else f"{info.days_remaining} days remaining"
    print(f"Token is valid ({info.token_type}, {window})")
    if info.scopes:
        print("Scopes: " + ", ".join(info.scopes))
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    from app.facebook.poster import FacebookAPIError, publish_post

    scheduled_time = datetime.fromisoformat(args.at) if args.at else None

    try:
        result = publish_post(
            message=args.message, image_url=args.image, scheduled_time=scheduled_time
        )
    except FacebookAPIError as exc:
        print(f"Cannot post: {exc}", file=sys.stderr)
        return 1

    if result.success:
        verb = "Scheduled" if result.scheduled else "Published"
        print(f"{verb} post {result.post_id}")
        return 0

    print(f"Post failed: {result.error} (code={result.error_code})", file=sys.stderr)
    return 1


def cmd_fb_schedule(args: argparse.Namespace) -> int:
    """Hand every pending post to Facebook's own scheduler.

    Facebook then publishes each one server-side at its scheduled time, so this
    machine does not need to be on. Re-time posts with `reschedule` first.
    """
    from app.database.db import init_db
    from app.scheduler.tasks import hand_off_to_facebook

    init_db()
    handed_off, errors = hand_off_to_facebook()
    print(f"Handed {handed_off} post(s) to Facebook's scheduler.")
    for error in errors:
        print(f"  FAILED {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    from app.database.db import init_db
    from app.scheduler.tasks import run_scheduler

    init_db()
    run_scheduler()
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Run the Messenger webhook server."""
    from app.database.db import init_db

    init_db()
    print(f"Webhook listening on http://0.0.0.0:{args.port}/webhook")
    print(f"Verify token: {settings.fb_verify_token}")
    if not settings.autoreply_enabled:
        print()
        print("NOTE: AUTOREPLY_ENABLED is false. Messages will be recorded")
        print("but no replies sent. Flip it in config/.env when ready to go live.")
    return subprocess.call(
        [sys.executable, "-m", "uvicorn", "app.messenger.webhook:app",
         "--host", "0.0.0.0", "--port", str(args.port)]
    )


def cmd_inbox(args: argparse.Namespace) -> int:
    """Show conversations waiting on a human."""
    from app.database.db import init_db
    from app.messenger.responder import pending_handoffs

    init_db()
    rows = pending_handoffs()
    if not rows:
        print("No conversations waiting on a human.")
        return 0

    print(f"{len(rows)} conversation(s) awaiting a human:")
    print()
    for row in rows:
        window = "window OPEN" if row["window_open"] else "window CLOSED - they must message again"
        print(f"  {row['name']:<24} {row['intent']:<8} {row['last_message_at']:%Y-%m-%d %H:%M}  {window}")
        for label, key in [
            ("address", "address"),
            ("condition", "condition"),
            ("timeline", "timeline"),
            ("reason", "reason_for_selling"),
            ("phone", "phone_number"),
            ("best time to call", "best_time_to_call"),
        ]:
            if row.get(key):
                print(f"    {label}: {row[key]}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    target = BASE_DIR / "app" / "dashboard" / "app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(target)])


def cmd_outreach(args: argparse.Namespace) -> int:
    from app.database.db import init_db
    from app.outreach.worker import run_campaign

    init_db()
    result = run_campaign(limit=args.limit, dry_run=not args.live, throttle=not args.no_throttle)
    print(result.summary())
    for message in result.messages:
        print(message)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.database.db import init_db, session_scope
    from app.database.models import OutreachProspect, ScheduledPost

    init_db()
    with session_scope() as session:
        posts = session.execute(
            select(ScheduledPost.status, func.count()).group_by(ScheduledPost.status)
        ).all()
        prospects = session.execute(
            select(OutreachProspect.status, func.count()).group_by(OutreachProspect.status)
        ).all()

    print("Posts:")
    print("\n".join(f"  {s.value:<12} {c}" for s, c in posts) or "  (none)")
    print("Prospects:")
    print("\n".join(f"  {s.value:<12} {c}" for s, c in prospects) or "  (none)")

    missing = settings.missing_graph_settings()
    print("\nGraph API: " + ("OK" if not missing else "missing " + ", ".join(missing)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Elite Homes USA automation POC"
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create database tables").set_defaults(func=cmd_init)

    seed = sub.add_parser("seed", help="Queue the 3 sample posts")
    seed.add_argument("--interval-hours", type=int, default=24)
    seed.set_defaults(func=cmd_seed)

    auth = sub.add_parser("auth", help="Exchange a short-lived token for a Page token")
    auth.add_argument("--token", required=True, help="Short-lived user access token")
    auth.add_argument("--no-persist", action="store_true", help="Print instead of writing .env")
    auth.set_defaults(func=cmd_auth)

    cancel = sub.add_parser("cancel", help="Remove pending posts from the queue")
    cancel.add_argument("--id", type=int, nargs="+", help="Post id(s) to remove")
    cancel.add_argument("--post-type", help="Segment(s) to remove, e.g. buyer,partner")
    cancel.set_defaults(func=cmd_cancel)

    resched = sub.add_parser("reschedule", help="Re-time pending posts in local time")
    resched.add_argument("--at", required=True, help="First post, local time e.g. 2026-09-24T09:00")
    resched.add_argument("--interval-hours", type=int, default=24)
    resched.set_defaults(func=cmd_reschedule)

    doctor = sub.add_parser("doctor", help="Diagnose token and Page access problems")
    doctor.add_argument("--token", required=True, help="User or Page access token")
    doctor.set_defaults(func=cmd_doctor)

    set_token = sub.add_parser(
        "set-token", help="Validate and save an already-issued Page token"
    )
    set_token.add_argument("--token", required=True, help="Page access token")
    set_token.set_defaults(func=cmd_set_token)

    pages = sub.add_parser("pages", help="List Pages this user administers")
    pages.add_argument("--token", required=True, help="Short-lived or long-lived user token")
    pages.set_defaults(func=cmd_pages)

    sub.add_parser("verify", help="Verify the stored token").set_defaults(func=cmd_verify)

    post = sub.add_parser("post", help="Publish or schedule a single post")
    post.add_argument("--message", required=True)
    post.add_argument("--image", default=None, help="Public image URL")
    post.add_argument("--at", default=None, help="ISO datetime, e.g. 2026-10-01T09:00:00")
    post.set_defaults(func=cmd_post)

    sub.add_parser("scheduler", help="Run the posting scheduler").set_defaults(func=cmd_scheduler)
    sub.add_parser(
        "fb-schedule", help="Hand pending posts to Facebook's scheduler (no local process needed)"
    ).set_defaults(func=cmd_fb_schedule)

    webhook = sub.add_parser("webhook", help="Run the Messenger auto-reply webhook")
    webhook.add_argument("--port", type=int, default=8000)
    webhook.set_defaults(func=cmd_webhook)

    sub.add_parser("inbox", help="Show conversations awaiting a human").set_defaults(func=cmd_inbox)
    sub.add_parser("dashboard", help="Launch the dashboard").set_defaults(func=cmd_dashboard)

    outreach = sub.add_parser("outreach", help="Run an outreach campaign")
    outreach.add_argument("--limit", type=int, default=None)
    outreach.add_argument("--live", action="store_true", help="Attempt real delivery")
    outreach.add_argument("--no-throttle", action="store_true")
    outreach.set_defaults(func=cmd_outreach)

    sub.add_parser("status", help="Print a queue summary").set_defaults(func=cmd_status)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
