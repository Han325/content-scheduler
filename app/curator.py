import re
import logging
from datetime import datetime, timezone, date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Video, LineupSlot, BacklogVideo
from config import settings

logger = logging.getLogger(__name__)

MIN_DURATION = 90
MAX_DURATION = 40 * 60
TRAILER_MIN = 60
TRAILER_MAX = 4 * 60

TITLE_BLACKLIST_PATTERNS = [
    re.compile(r"\breact(s|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bi tried\b", re.IGNORECASE),
    re.compile(r"\branking every\b", re.IGNORECASE),
    re.compile(r"\bvs\.", re.IGNORECASE),
    re.compile(r"\bchallenge\b", re.IGNORECASE),
    re.compile(r"#shorts", re.IGNORECASE),
]

CAPS_THRESHOLD = 0.40


def _is_excessive_caps(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > CAPS_THRESHOLD


def _passes_duration_filter(duration_seconds: int, category: str) -> bool:
    if category == "trailer":
        return TRAILER_MIN <= duration_seconds <= TRAILER_MAX
    return MIN_DURATION <= duration_seconds <= MAX_DURATION


def _passes_title_filter(title: str) -> bool:
    for pattern in TITLE_BLACKLIST_PATTERNS:
        if pattern.search(title):
            return False
    return not _is_excessive_caps(title)


def _days_since_published(published_at: Optional[datetime]) -> Optional[float]:
    if published_at is None:
        return None
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return (now - published_at).total_seconds() / 86400


def score_video(video: Video) -> float:
    from app.youtube import CHANNEL_WHITELIST
    s = 0.0
    days_old = _days_since_published(video.published_at)
    if days_old is not None:
        if days_old <= 7:
            s += 0.3
        elif days_old <= 30:
            s += 0.15
    if video.channel_id in CHANNEL_WHITELIST:
        s += 0.4
    if video.category != "trailer" and 10 * 60 <= video.duration_seconds <= 25 * 60:
        s += 0.2
    if video.category == "trailer":
        s += 0.1
    return round(s, 4)


def filter_videos(videos: list[Video], db: Session, today: Optional[date] = None) -> list[Video]:
    if today is None:
        today = date.today()
    watched_ids: set[str] = set()
    for slot in db.query(LineupSlot).filter(LineupSlot.is_watched == True).all():  # noqa: E712
        watched_ids.add(slot.video.youtube_id)
    for entry in db.query(BacklogVideo).filter(BacklogVideo.is_watched == True).all():  # noqa: E712
        watched_ids.add(entry.video.youtube_id)
    todays_ids = {slot.video.youtube_id for slot in db.query(LineupSlot).filter(LineupSlot.date == today).all()}
    backlog_ids = {entry.video.youtube_id for entry in db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).all()}  # noqa: E712
    excluded = watched_ids | todays_ids | backlog_ids
    filtered = []
    for video in videos:
        if video.youtube_id in excluded:
            continue
        if not _passes_duration_filter(video.duration_seconds, video.category):
            continue
        if not _passes_title_filter(video.title):
            continue
        filtered.append(video)
    return filtered


def build_lineup(videos: list[Video], target_seconds: int = None, today: Optional[date] = None) -> list[Video]:
    if target_seconds is None:
        target_seconds = settings.DAILY_BUDGET_MINUTES * 60
    for video in videos:
        video.score = score_video(video)
    lineup: list[Video] = []
    total_seconds = 0
    for video in sorted(videos, key=lambda v: v.score, reverse=True):
        if total_seconds >= target_seconds:
            break
        lineup.append(video)
        total_seconds += video.duration_seconds
    logger.info(f"Built lineup: {len(lineup)} videos, {total_seconds // 60}m total")
    return lineup


def upsert_video(db: Session, video_data: dict) -> Video:
    existing = db.query(Video).filter(Video.youtube_id == video_data["youtube_id"]).first()
    if existing:
        for key, value in video_data.items():
            if key != "id":
                setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing
    video = Video(**video_data)
    db.add(video)
    db.commit()
    db.refresh(video)
    return video


def run_daily_curation(db: Session) -> dict:
    from app.youtube import get_subscription_videos, search_topic_videos
    today = date.today()
    logger.info(f"Starting daily curation for {today}")
    sub_videos_raw = get_subscription_videos(max_results=50)
    topic_videos_raw = search_topic_videos()
    all_raw = sub_videos_raw + topic_videos_raw
    logger.info(f"Fetched {len(sub_videos_raw)} subscription + {len(topic_videos_raw)} topic videos")
    all_videos: list[Video] = []
    for video_data in all_raw:
        video_data.pop("topic_match", None)
        all_videos.append(upsert_video(db, video_data))
    seen_ids: set[str] = set()
    unique_videos: list[Video] = []
    for v in all_videos:
        if v.youtube_id not in seen_ids:
            seen_ids.add(v.youtube_id)
            unique_videos.append(v)
    eligible = filter_videos(unique_videos, db, today)
    logger.info(f"{len(eligible)} videos passed filters")
    lineup_videos = build_lineup(eligible, today=today)
    for i, video in enumerate(lineup_videos):
        video.score = score_video(video)
        db.add(video)
        db.add(LineupSlot(date=today, video_id=video.id, position=i))
    lineup_youtube_ids = {v.youtube_id for v in lineup_videos}
    backlog_count = 0
    for video in eligible:
        if video.youtube_id not in lineup_youtube_ids:
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
