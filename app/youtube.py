"""
YouTube API client — subscriptions + topic search.

OAuth token is stored locally in token.json.
On first run, visit /auth to start the OAuth consent flow.
"""

import os
import json
import logging

_QUOTA_FILE = "quota_state.json"
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = settings.TOKEN_FILE
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

_pending_flow: Optional["Flow"] = None

# --- Quota tracking (resets at midnight, 10 000 units/day default) ---
_quota: dict = {"date": None, "units": 0}
QUOTA_DAILY_LIMIT = 10_000


def _load_quota() -> None:
    """Load persisted quota state from disk so restarts don't reset the counter."""
    if os.path.exists(_QUOTA_FILE):
        try:
            with open(_QUOTA_FILE) as f:
                state = json.load(f)
            _quota["date"] = state.get("date")
            _quota["units"] = state.get("units", 0)
        except Exception:
            pass


def _save_quota() -> None:
    try:
        with open(_QUOTA_FILE, "w") as f:
            json.dump({"date": _quota["date"], "units": _quota["units"]}, f)
    except Exception:
        pass


def _track(units: int) -> None:
    from datetime import date
    today = str(date.today())
    if _quota["date"] != today:
        _quota["date"] = today
        _quota["units"] = 0
    _quota["units"] += units
    _save_quota()


def quota_status() -> dict:
    from datetime import date
    today = str(date.today())
    used = _quota["units"] if _quota["date"] == today else 0
    return {"used": used, "limit": QUOTA_DAILY_LIMIT}


_load_quota()

TOPIC_QUERIES: dict = {
    "News": [
        "geopolitics explained 2025",
        "world news analysis 2025",
        "foreign policy explained",
        "tldr news",
        "international relations documentary",
    ],
    "Tech & Science": [
        "technology explained 2025",
        "artificial intelligence explained",
        "science documentary 2025",
        "future technology analysis",
    ],
    "Film & TV": [
        "official movie trailer 2025",
        "official tv show trailer 2025",
    ],
    "Philosophy": [
        "philosophy explained",
        "social commentary essay",
        "cultural criticism video essay",
        "ideas explained",
    ],
}

# YouTube Data API categoryId → display label
_YT_CATEGORY_DISPLAY = {
    "1":  "Film & TV",
    "10": "Music",
    "17": "Sports",
    "20": "Gaming",
    "22": "Culture",
    "23": "Entertainment",
    "24": "Entertainment",
    "25": "News",
    "27": "Education",
    "28": "Tech & Science",
    "29": "Culture",
}

CHANNEL_WHITELIST: list[str] = [
    # Add channel IDs here as strings:
    # "UCxxxxxx",  # About That (Andrew Chang)
    # "UCxxxxxx",  # Good Work
    # "UCxxxxxx",  # Morning Brew
]

MIN_SUBSCRIBER_COUNT = 500

# ISO 3166-1 alpha-2 country codes to exclude entirely
CHANNEL_COUNTRY_BLOCKLIST: set[str] = {"IN", "PK", "ID"}

# Channel names to block regardless of content
CHANNEL_NAME_BLOCKLIST: set[str] = {
    "Fox News",
    "Fox Business",
}


def _client_config() -> dict:
    return {
        "installed": {
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "client_secret": settings.YOUTUBE_CLIENT_SECRET,
            "redirect_uris": ["http://localhost:8000/auth/callback"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def get_credentials() -> Optional[Credentials]:
    """Load and refresh credentials from token.json, or return None if not available."""
    if not os.path.exists(TOKEN_FILE):
        return None

    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except Exception as e:
        logger.warning(f"Could not load token.json: {e}")
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except Exception as e:
            logger.warning(f"Could not refresh credentials: {e}")
            return None

    return creds if creds and creds.valid else None


def _save_credentials(creds: Credentials) -> None:
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())


def get_auth_flow(redirect_uri: str = "http://localhost:8000/auth/callback") -> Flow:
    global _pending_flow
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    _pending_flow = flow
    return flow


def exchange_code_for_token(code: str, state: str) -> Credentials:
    global _pending_flow
    flow = _pending_flow
    if flow is None:
        raise ValueError("No pending OAuth flow. Visit /auth to start authentication.")
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    _pending_flow = None
    return creds


def _build_youtube(creds: Credentials):
    return build("youtube", "v3", credentials=creds)


def _parse_duration(iso_duration: str) -> int:
    """Parse ISO 8601 duration string to seconds. e.g. PT4M13S -> 253"""
    import re
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _blocked_channel_ids(youtube, channel_ids: list[str]) -> set[str]:
    """Return channel IDs blocked by country, subscriber count, or name blocklist."""
    if not channel_ids:
        return set()
    blocked: set[str] = set()
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        try:
            resp = youtube.channels().list(
                part="snippet,statistics",
                id=",".join(batch),
            ).execute()
            _track(1)
            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                country = snippet.get("country", "")
                name = snippet.get("title", "")
                sub_raw = item.get("statistics", {}).get("subscriberCount")
                too_small = sub_raw is None or int(sub_raw) < MIN_SUBSCRIBER_COUNT
                if (country in CHANNEL_COUNTRY_BLOCKLIST
                        or name in CHANNEL_NAME_BLOCKLIST
                        or too_small):
                    blocked.add(item["id"])
        except HttpError as e:
            logger.warning(f"Error fetching channel details: {e}")
    return blocked


def _video_ids_to_details(youtube, video_ids: list[str]) -> list[dict]:
    """Fetch video details (duration, etc.) for a list of video IDs."""
    if not video_ids:
        return []

    results = []
    # YouTube API allows up to 50 per request
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        try:
            response = youtube.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(batch),
            ).execute()
            _track(1)
            results.extend(response.get("items", []))
        except HttpError as e:
            logger.error(f"Error fetching video details: {e}")

    videos = []
    for item in results:
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        published_raw = snippet.get("publishedAt", "")
        try:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            published_at = None

        yt_cat_id = snippet.get("categoryId", "")
        videos.append({
            "youtube_id": item["id"],
            "title": snippet.get("title", ""),
            "channel_name": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "duration_seconds": _parse_duration(content.get("duration", "PT0S")),
            "thumbnail_url": (snippet.get("thumbnails", {}).get("medium", {}) or {}).get("url", ""),
            "published_at": published_at,
            "category": _YT_CATEGORY_DISPLAY.get(yt_cat_id, "general"),
        })

    return videos


def get_subscription_videos(max_results: int = 50) -> list[dict]:
    """Fetch recent uploads from subscribed channels."""
    creds = get_credentials()
    if not creds:
        logger.warning("No YouTube credentials available. Skipping subscription fetch.")
        return []

    youtube = _build_youtube(creds)
    video_ids = []

    try:
        # Get subscriptions
        subs_response = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
        ).execute()
        _track(1)

        channel_ids = [
            item["snippet"]["resourceId"]["channelId"]
            for item in subs_response.get("items", [])
        ]

        # Add whitelist channels
        for cid in CHANNEL_WHITELIST:
            if cid not in channel_ids:
                channel_ids.append(cid)

        # For each channel, get the uploads playlist
        for channel_id in channel_ids[:40]:  # Limit to avoid quota explosion
            try:
                chan_response = youtube.channels().list(
                    part="contentDetails,snippet,statistics",
                    id=channel_id,
                ).execute()
                _track(1)
                items = chan_response.get("items", [])
                if not items:
                    continue
                snippet = items[0].get("snippet", {})
                country = snippet.get("country", "")
                name = snippet.get("title", "")
                sub_raw = items[0].get("statistics", {}).get("subscriberCount")
                if country in CHANNEL_COUNTRY_BLOCKLIST:
                    logger.debug(f"Skipping channel {channel_id} (country: {country})")
                    continue
                if name in CHANNEL_NAME_BLOCKLIST:
                    logger.debug(f"Skipping channel {channel_id} (name blocklist: {name})")
                    continue
                if sub_raw is not None and int(sub_raw) < MIN_SUBSCRIBER_COUNT:
                    logger.debug(f"Skipping channel {channel_id} (subscribers: {sub_raw})")
                    continue

                uploads_playlist = (
                    items[0]
                    .get("contentDetails", {})
                    .get("relatedPlaylists", {})
                    .get("uploads", "")
                )
                if not uploads_playlist:
                    continue

                pl_response = youtube.playlistItems().list(
                    part="contentDetails",
                    playlistId=uploads_playlist,
                    maxResults=20,
                ).execute()
                _track(1)

                for pl_item in pl_response.get("items", []):
                    vid_id = pl_item.get("contentDetails", {}).get("videoId")
                    if vid_id:
                        video_ids.append(vid_id)

            except HttpError as e:
                logger.warning(f"Error fetching uploads for channel {channel_id}: {e}")
                continue

    except HttpError as e:
        logger.error(f"Error fetching subscriptions: {e}")
        return []

    video_ids = list(dict.fromkeys(video_ids))[:max(max_results, 150)]
    return _video_ids_to_details(youtube, video_ids)


def search_topic_videos(queries: Optional[list[str]] = None, max_results_per_query: int = 10) -> list[dict]:
    """Search for videos matching topic keywords."""
    creds = get_credentials()
    if not creds:
        logger.warning("No YouTube credentials available. Skipping topic search.")
        return []

    query_map = queries if isinstance(queries, dict) else {"general": queries}
    if queries is None:
        query_map = TOPIC_QUERIES

    youtube = _build_youtube(creds)
    # Track which video ID belongs to which category group
    video_id_category: dict = {}

    for category_label, query_list in query_map.items():
        for query in query_list:
            try:
                response = youtube.search().list(
                    part="id",
                    q=query,
                    type="video",
                    maxResults=max_results_per_query,
                    order="date",
                    videoDuration="medium",  # 4–20 minutes
                    relevanceLanguage="en",
                ).execute()
                _track(100)

                for item in response.get("items", []):
                    vid_id = item.get("id", {}).get("videoId")
                    if vid_id and vid_id not in video_id_category:
                        video_id_category[vid_id] = category_label

            except HttpError as e:
                logger.warning(f"Error searching for '{query}': {e}")
                continue

    all_video_ids = list(video_id_category.keys())
    videos = _video_ids_to_details(youtube, all_video_ids)

    # Override category with the query group label (more precise than YouTube's categoryId)
    for v in videos:
        if v["youtube_id"] in video_id_category:
            v["category"] = video_id_category[v["youtube_id"]]

    # Filter out blocked channels
    unique_channel_ids = list({v["channel_id"] for v in videos if v.get("channel_id")})
    blocked = _blocked_channel_ids(youtube, unique_channel_ids)
    if blocked:
        before = len(videos)
        videos = [v for v in videos if v.get("channel_id") not in blocked]
        logger.info(f"Channel filter removed {before - len(videos)} search videos")

    # Trailer title override — must stay "trailer" for duration filter to work
    trailer_keywords = ["trailer", "official trailer", "teaser"]
    for v in videos:
        title_lower = v["title"].lower()
        if any(kw in title_lower for kw in trailer_keywords):
            v["category"] = "trailer"

    return videos
