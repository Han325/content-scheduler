import os
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

TOPIC_QUERIES = [
    "geopolitics explained 2025",
    "world news analysis",
    "morning brew news",
    "tldr news",
    "official movie trailer 2025",
    "official tv show trailer 2025",
    "philosophy explained",
    "social commentary",
    "tech explained",
    "science documentary short",
]

CHANNEL_WHITELIST: list[str] = [
    # Add channel IDs here:
    # "UCxxxxxx",  # About That (Andrew Chang)
    # "UCxxxxxx",  # Good Work
    # "UCxxxxxx",  # Morning Brew
]


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "client_secret": settings.YOUTUBE_CLIENT_SECRET,
            "redirect_uris": ["http://localhost:8000/auth/callback"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def get_credentials() -> Optional[Credentials]:
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
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow


def exchange_code_for_token(code: str, state: str) -> Credentials:
    flow = get_auth_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    return creds


def _build_youtube(creds: Credentials):
    return build("youtube", "v3", credentials=creds)


def _parse_duration(iso_duration: str) -> int:
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _video_ids_to_details(youtube, video_ids: list[str]) -> list[dict]:
    if not video_ids:
        return []
    results = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        try:
            response = youtube.videos().list(
                part="snippet,contentDetails,statistics",
                id=",".join(batch),
            ).execute()
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
        videos.append({
            "youtube_id": item["id"],
            "title": snippet.get("title", ""),
            "channel_name": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "duration_seconds": _parse_duration(content.get("duration", "PT0S")),
            "thumbnail_url": (snippet.get("thumbnails", {}).get("medium", {}) or {}).get("url", ""),
            "published_at": published_at,
            "category": "general",
        })
    return videos


def get_subscription_videos(max_results: int = 50) -> list[dict]:
    creds = get_credentials()
    if not creds:
        logger.warning("No YouTube credentials available. Skipping subscription fetch.")
        return []
    youtube = _build_youtube(creds)
    video_ids = []
    try:
        subs_response = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
        ).execute()
        channel_ids = [
            item["snippet"]["resourceId"]["channelId"]
            for item in subs_response.get("items", [])
        ]
        for cid in CHANNEL_WHITELIST:
            if cid not in channel_ids:
                channel_ids.append(cid)
        for channel_id in channel_ids[:20]:
            try:
                chan_response = youtube.channels().list(
                    part="contentDetails",
                    id=channel_id,
                ).execute()
                items = chan_response.get("items", [])
                if not items:
                    continue
                uploads_playlist = (
                    items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
                )
                if not uploads_playlist:
                    continue
                pl_response = youtube.playlistItems().list(
                    part="contentDetails",
                    playlistId=uploads_playlist,
                    maxResults=5,
                ).execute()
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
    video_ids = list(dict.fromkeys(video_ids))[:max_results]
    return _video_ids_to_details(youtube, video_ids)


def search_topic_videos(queries: Optional[list[str]] = None, max_results_per_query: int = 10) -> list[dict]:
    creds = get_credentials()
    if not creds:
        logger.warning("No YouTube credentials available. Skipping topic search.")
        return []
    if queries is None:
        queries = TOPIC_QUERIES
    youtube = _build_youtube(creds)
    all_video_ids = []
    for query in queries:
        try:
            response = youtube.search().list(
                part="id",
                q=query,
                type="video",
                maxResults=max_results_per_query,
                order="date",
                videoDuration="medium",
            ).execute()
            for item in response.get("items", []):
                vid_id = item.get("id", {}).get("videoId")
                if vid_id:
                    all_video_ids.append(vid_id)
        except HttpError as e:
            logger.warning(f"Error searching for '{query}': {e}")
            continue
    all_video_ids = list(dict.fromkeys(all_video_ids))
    videos = _video_ids_to_details(youtube, all_video_ids)
    trailer_keywords = ["trailer", "official trailer", "teaser"]
    for v in videos:
        if any(kw in v["title"].lower() for kw in trailer_keywords):
            v["category"] = "trailer"
    return videos
