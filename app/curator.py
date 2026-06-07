"""
Scoring, filtering, and lineup building.

All rules are explicit — no ML.
"""

import re
import logging
import unicodedata
from datetime import datetime, timezone, date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Video, LineupSlot, BacklogVideo, ExternalWatch, RejectedVideo
from config import settings

logger = logging.getLogger(__name__)

# --- Filter constants ---
MIN_DURATION = 90          # seconds
MAX_DURATION = 40 * 60     # 40 minutes
TRAILER_MIN = 60
TRAILER_MAX = 4 * 60       # 4 minutes

TITLE_BLACKLIST_PATTERNS = [
    # Reaction / low-effort formats
    re.compile(r"\breact(s|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bi tried\b", re.IGNORECASE),
    re.compile(r"\branking every\b", re.IGNORECASE),
    re.compile(r"\bvs\.", re.IGNORECASE),
    re.compile(r"\bchallenge\b", re.IGNORECASE),
    re.compile(r"#shorts", re.IGNORECASE),
    # Language tags (Romanized or explicit)
    re.compile(r"\b(hindi|telugu|tamil|kannada|malayalam|punjabi|bengali|marathi|gujarati|urdu|odia|hinglish)\b", re.IGNORECASE),
    re.compile(r"\b(mein|hai|kaise|kya|aur|nahi|bahut|accha|theek|bilkul|lekin)\b"),
    # Exam / academic junk
    re.compile(r"\bexams?\b", re.IGNORECASE),
    re.compile(r"\b(exam\s*paper|past\s*paper|model\s*paper|question\s*paper|solved\s*paper|answer\s*key)\b", re.IGNORECASE),
    re.compile(r"\b(mcqs?|ssc|upsc|jee|neet|ias|ips|gate\s*exam|board\s*exam)\b", re.IGNORECASE),
    re.compile(r"\b(solutions?\s*(paper|set|class)|class\s*\d+\s*(maths?|science|physics|chemistry|biology))\b", re.IGNORECASE),
    re.compile(r"\b(lecture\s*\d+|chapter\s*\d+|full\s*course|crash\s*course|complete\s*course)\b", re.IGNORECASE),
    # Recruitment / HR content
    re.compile(r"\brecruitment\b", re.IGNORECASE),
    re.compile(r"\b(hiring\s+process|job\s+interview|resume\s+tips|cv\s+tips|how\s+to\s+get\s+hired)\b", re.IGNORECASE),
    # Geographic filters
    re.compile(r"\bindia\b", re.IGNORECASE),
    # News broadcast re-uploads and live TV rips
    re.compile(r"\bfull\s+(episode|show|broadcast)\b", re.IGNORECASE),
    re.compile(r"\b(morning|evening|tonight|today'?s?|latest|breaking)\s+headlines?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*(am|pm)\s+headlines?\b", re.IGNORECASE),
    re.compile(r"\d{3,4}[pP]\s*(hd|HD)?\s*$"),  # ends in "1080P HD" — pirated rip
    re.compile(r"\|\s*\w[\w\s]{1,25}(tv|news|channel)\s*$", re.IGNORECASE),  # "| Sathiyam TV" title branding
    # Regional South Asian content (Latin-script but clearly regional)
    re.compile(r"\b(operation\s+sindoor|pahalgam|imran\s+khan|adiala|tollywood|lollywood)\b", re.IGNORECASE),
    re.compile(r"\b(ram\s+charan|buchi\s+babu|prabhas|allu\s+arjun|vijay\s+devarakonda)\b", re.IGNORECASE),
    re.compile(r"\b(zee\s+news|ndtv|aaj\s+tak|india\s+tv|times\s+now|sathiyam|sun\s+tv)\b", re.IGNORECASE),
    # Offensive language
    re.compile(r"\bretards?\b", re.IGNORECASE),
    re.compile(r"\b(nigger|faggot|tranny)\b", re.IGNORECASE),
]

MAX_PER_CHANNEL = 2  # max videos from the same channel in one lineup


def _has_emoji(text: str) -> bool:
    # Unicode category 'So' (Symbol, Other) covers all emoji and emoji-like symbols,
    # including those outside the BMP (⭐ U+2B50, ▶ U+25B6, ™ U+2122, etc.)
    # that the previous hard-coded range check missed.
    for c in text:
        if unicodedata.category(c) == 'So':
            return True
    return False

CAPS_THRESHOLD = 0.40  # >40% uppercase letters = filter out

# Unicode ranges for non-Latin scripts
_NON_LATIN_RANGES = [
    (0x0600, 0x06FF),  # Arabic / Urdu
    (0x0900, 0x097F),  # Devanagari (Hindi)
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi (Punjabi)
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B00, 0x0B7F),  # Odia
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
    (0x0D80, 0x0DFF),  # Sinhala
    (0x0E00, 0x0E7F),  # Thai
    (0x0E80, 0x0EFF),  # Lao
    (0x1000, 0x109F),  # Myanmar / Burmese
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x4E00, 0x9FFF),  # CJK (Chinese/Japanese/Korean)
    (0xAC00, 0xD7AF),  # Hangul
    (0x1E00, 0x1EFF),  # Latin Extended Additional (heavy Vietnamese diacritics)
]


def _is_non_latin(c: str) -> bool:
    cp = ord(c)
    return any(start <= cp <= end for start, end in _NON_LATIN_RANGES)


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
    if '|' in title:
        return False
    if '#' in title:
        return False
    if _has_emoji(title):
        return False
    for pattern in TITLE_BLACKLIST_PATTERNS:
        if pattern.search(title):
            return False
    if _is_excessive_caps(title):
        return False
    if any(_is_non_latin(c) for c in title):
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

    # Imported YouTube watch history (Google Takeout)
    for ew in db.query(ExternalWatch).all():
        watched_ids.add(ew.youtube_id)

    # Explicitly rejected by the user
    for rv in db.query(RejectedVideo).all():
        watched_ids.add(rv.youtube_id)

    # Already in today's lineup
    todays_slots = db.query(LineupSlot).filter(LineupSlot.date == today).all()
    todays_ids: set[str] = {slot.video.youtube_id for slot in todays_slots}

    # Already in backlog (unwatched)
    backlog_entries = db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).all()  # noqa: E712
    backlog_ids: set[str] = {entry.video.youtube_id for entry in backlog_entries}

    excluded = watched_ids | todays_ids | backlog_ids

    from app.youtube import CHANNEL_NAME_BLOCKLIST

    filtered = []
    for video in videos:
        if video.youtube_id in excluded:
            continue
        if getattr(video, "dismissed", False):
            continue
        if video.channel_name in CHANNEL_NAME_BLOCKLIST:
            logger.debug(f"Filtered (channel blocklist): {video.channel_name}")
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
        target_seconds = settings.daily_budget_minutes * 60

    if today is None:
        today = date.today()

    # Score each video
    for video in videos:
        video.score = score_video(video)

    # Sort by score descending
    sorted_videos = sorted(videos, key=lambda v: v.score, reverse=True)

    lineup: list[Video] = []
    total_seconds = 0
    channel_counts: dict[str, int] = {}

    for video in sorted_videos:
        if total_seconds >= target_seconds:
            break
        if channel_counts.get(video.channel_id, 0) >= MAX_PER_CHANNEL:
            continue
        lineup.append(video)
        total_seconds += video.duration_seconds
        channel_counts[video.channel_id] = channel_counts.get(video.channel_id, 0) + 1

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


def _clean_backlog(db: Session) -> int:
    """Remove unwatched backlog entries that fail current title/channel rules, are dismissed, or were rejected."""
    from app.youtube import CHANNEL_NAME_BLOCKLIST
    rejected_ids = {rv.youtube_id for rv in db.query(RejectedVideo).all()}
    entries = db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).all()  # noqa: E712
    removed = 0
    for entry in entries:
        v = entry.video
        if (getattr(v, "dismissed", False)
                or not _passes_title_filter(v.title)
                or v.channel_name in CHANNEL_NAME_BLOCKLIST
                or v.youtube_id in rejected_ids):
            db.delete(entry)
            removed += 1
    if removed:
        db.commit()
        logger.info(f"Cleaned {removed} backlog entries that failed filters")
    return removed


def run_daily_curation(db: Session) -> dict:
    """
    Full pipeline: fetch -> score -> filter -> build lineup -> persist.
    Returns summary dict.
    """
    from app.youtube import get_subscription_videos, search_topic_videos

    today = date.today()
    logger.info(f"Starting daily curation for {today}")

    # Clear today's unwatched slots so each Refresh produces a clean lineup
    stale = db.query(LineupSlot).filter(
        LineupSlot.date == today,
        LineupSlot.is_watched == False,  # noqa: E712
    ).all()
    for slot in stale:
        db.delete(slot)
    db.commit()

    _clean_backlog(db)

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
