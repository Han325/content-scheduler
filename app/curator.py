"""
Scoring, filtering, and lineup building.

All rules are explicit — no ML.
"""

import re
import logging
from datetime import datetime, timezone, date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Video, LineupSlot, BacklogVideo
from config import settings

logger = logging.getLogger(__name__)

# --- Filter constants ---
MIN_DURATION = 90          # seconds
MAX_DURATION = 40 * 60     # 40 minutes
TRAILER_MIN = 60
TRAILER_MAX = 4 * 60       # 4 minutes

TITLE_BLACKLIST_PATTERNS = [
    re.compile(r"\breact(s|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bi tried\b", re.IGNORECASE),
    re.compile(r"\branking every\b", re.IGNORECASE),
    re.compile(r"\bvs\.", re.IGNORECASE),
    re.compile(r"\bchallenge\b", re.IGNORECASE),
    re.compile(r"#shorts", re.IGNORECASE),
]

CAPS_THRESHOLD = 0.40  # >40% uppercase letters = filter out


def _is_excessive_caps(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio > CAPS_THRESHOLD


def _passes_duration_filter(duration_seconds: int, category: str) -> bool:
    if category == "trailer":
        return TRAILER_MIN <= duration_seconds <= TRAILER_MAX
    return MIN_DURATION <= duration_seconds <= MAX_DURATION


def _passes_title_filter(title: str) -> bool:
    for pattern in TITLE_BLACKLIST_PATTERNS:
        if pattern.search(title):
            return False
    if _is_excessive_caps(title):
        return False
    return True


def _days_since_published(published_at: Optional[datetime]) -> Optional[float]:
    if published_at is None:
        return None
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    delta = now - published_at
    return delta.total_seconds() / 86400


def score_video(video: Video) -> float:
    """
    Score a video from 0.0 to 1.0+ (channel whitelist can push above 1.0).
    Higher = better.
    """
    from app.youtube import CHANNEL_WHITELIST

    s = 0.0

    # Recency
    days_old = _days_since_published(video.published_at)
    if days_old is not None:
        if days_old <= 7:
            s += 0.3
        elif days_old <= 30:
            s += 0.15

    # Channel whitelist bonus
    if video.channel_id in CHANNEL_WHITELIST:
        s += 0.4

    # Duration sweet spot (10–25 min) for non-trailers
    if video.category != "trailer":
        if 10 * 60 <= video.duration_seconds <= 25 * 60:
            s += 0.2

    # Category trailer gets a small base boost (they're pre-filtered by duration)
    if video.category == "trailer":
        s += 0.1

    # Topic match — already sourced from topic queries so slight boost
    # (subscription videos get +0 here; topic-searched get +0.1)
    # We use the category field as a proxy: "general" from search, keep neutral.
    # A cleaner approach would tag at fetch time — left as future improvement.

    return round(s, 4)


def filter_videos(
    videos: list[Video],
    db: Session,
    today: Optional[date] = None,
) -> list[Video]:
    """
    Remove videos that don't pass quality rules or are already seen.
    """
    if today is None:
        today = date.today()

    # Collect already-seen youtube_ids
    watched_ids: set[str] = set()

    # Watched in lineup (any date)
    watched_slots = db.query(LineupSlot).filter(LineupSlot.is_watched == True).all()  # noqa: E712
    for slot in watched_slots:
        watched_ids.add(slot.video.youtube_id)

    # Watched in backlog
    watched_backlog = db.query(BacklogVideo).filter(BacklogVideo.is_watched == True).all()  # noqa: E712
    for entry in watched_backlog:
        watched_ids.add(entry.video.youtube_id)

    # Already in today's lineup
    todays_slots = db.query(LineupSlot).filter(LineupSlot.date == today).all()
    todays_ids: set[str] = {slot.video.youtube_id for slot in todays_slots}

    # Already in backlog (unwatched)
    backlog_entries = db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).all()  # noqa: E712
    backlog_ids: set[str] = {entry.video.youtube_id for entry in backlog_entries}

    excluded = watched_ids | todays_ids | backlog_ids

    filtered = []
    for video in videos:
        if video.youtube_id in excluded:
            continue
        if not _passes_duration_filter(video.duration_seconds, video.category):
            logger.debug(f"Filtered (duration): {video.title} ({video.duration_seconds}s)")
            continue
        if not _passes_title_filter(video.title):
            logger.debug(f"Filtered (title): {video.title}")
            continue
        filtered.append(video)

    return filtered


def build_lineup(
    videos: list[Video],
    target_seconds: int = None,
    today: Optional[date] = None,
) -> list[Video]:
    """
    Greedily pick highest-scored videos until the daily budget is met.
    Returns ordered list of videos for the lineup.
    """
    if target_seconds is None:
        target_seconds = settings.DAILY_BUDGET_MINUTES * 60

    if today is None:
        today = date.today()

    # Score each video
    for video in videos:
        video.score = score_video(video)

    # Sort by score descending
    sorted_videos = sorted(videos, key=lambda v: v.score, reverse=True)

    lineup: list[Video] = []
    total_seconds = 0

    for video in sorted_videos:
        if total_seconds >= target_seconds:
            break
        lineup.append(video)
        total_seconds += video.duration_seconds

    logger.info(
        f"Built lineup: {len(lineup)} videos, "
        f"{total_seconds // 60}m total (budget: {target_seconds // 60}m)"
    )
    return lineup


def upsert_video(db: Session, video_data: dict) -> Video:
    """Insert or update a video record. Returns the Video ORM object."""
    existing = db.query(Video).filter(Video.youtube_id == video_data["youtube_id"]).first()
    if existing:
        # Update score and fetched_at
        for key, value in video_data.items():
            if key not in ("id",):
                setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing
    else:
        video = Video(**video_data)
        db.add(video)
        db.commit()
        db.refresh(video)
        return video


def run_daily_curation(db: Session) -> dict:
    """
    Full pipeline: fetch -> score -> filter -> build lineup -> persist.
    Returns summary dict.
    """
    from app.youtube import get_subscription_videos, search_topic_videos

    today = date.today()
    logger.info(f"Starting daily curation for {today}")

    # Fetch
    sub_videos_raw = get_subscription_videos(max_results=50)
    topic_videos_raw = search_topic_videos()

    # Tag topic videos
    for v in topic_videos_raw:
        if "topic_match" not in v:
            v["topic_match"] = True

    all_raw = sub_videos_raw + topic_videos_raw
    logger.info(f"Fetched {len(sub_videos_raw)} subscription + {len(topic_videos_raw)} topic videos")

    # Persist to DB (upsert)
    all_videos: list[Video] = []
    for video_data in all_raw:
        video_data.pop("topic_match", None)
        video = upsert_video(db, video_data)
        all_videos.append(video)

    # Deduplicate by youtube_id
    seen_ids: set[str] = set()
    unique_videos: list[Video] = []
    for v in all_videos:
        if v.youtube_id not in seen_ids:
            seen_ids.add(v.youtube_id)
            unique_videos.append(v)

    # Filter
    eligible = filter_videos(unique_videos, db, today)
    logger.info(f"{len(eligible)} videos passed filters")

    # Build lineup
    lineup_videos = build_lineup(eligible, today=today)

    # Save lineup slots
    for i, video in enumerate(lineup_videos):
        video.score = score_video(video)
        db.add(video)

        slot = LineupSlot(
            date=today,
            video_id=video.id,
            position=i,
        )
        db.add(slot)

    # Add remaining eligible videos to backlog
    lineup_youtube_ids = {v.youtube_id for v in lineup_videos}
    backlog_count = 0
    for video in eligible:
        if video.youtube_id not in lineup_youtube_ids:
            # Check not already in backlog
            existing_backlog = db.query(BacklogVideo).filter(
                BacklogVideo.video_id == video.id,
                BacklogVideo.is_watched == False,  # noqa: E712
            ).first()
            if not existing_backlog:
                db.add(BacklogVideo(video_id=video.id))
                backlog_count += 1

    db.commit()

    return {
        "date": str(today),
        "fetched": len(all_raw),
        "eligible": len(eligible),
        "lineup_count": len(lineup_videos),
        "lineup_minutes": sum(v.duration_seconds for v in lineup_videos) // 60,
        "backlog_added": backlog_count,
    }
