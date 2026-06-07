"""
FastAPI application — routes, templates, OAuth flow.
"""

import base64
import logging
import secrets
from datetime import date, datetime, time
from typing import Optional

import json
import re as _re

from fastapi import FastAPI, Depends, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, init_db, load_from_gcs, sync_to_gcs
from app.models import Video, LineupSlot, BacklogVideo, ExternalWatch, RejectedVideo
from app.scheduler import start_scheduler, stop_scheduler
from app.youtube import quota_status
from config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Content Scheduler", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.cache = None  # newer Jinja2 includes env.globals in cache key, making it unhashable


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


templates.env.globals["format_duration"] = _format_duration


# ---------------------------------------------------------------------------
# Basic Auth middleware
# ---------------------------------------------------------------------------

_UNPROTECTED = {"/auth/callback", "/refresh"}

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not settings.APP_PASSWORD or request.url.path in _UNPROTECTED:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, password = decoded.split(":", 1)
            if secrets.compare_digest(password, settings.APP_PASSWORD):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Lineup"'},
    )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    load_from_gcs()
    init_db()
    if not settings.DISABLE_SCHEDULER:
        start_scheduler()
    else:
        logger.info("APScheduler disabled — curation triggered by Cloud Scheduler via POST /refresh")
    if not settings.has_youtube_credentials:
        logger.warning(
            "No YouTube credentials found in environment. "
            "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET, then visit /auth."
        )


@app.on_event("shutdown")
def on_shutdown():
    if not settings.DISABLE_SCHEDULER:
        stop_scheduler()
    sync_to_gcs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_primetime() -> bool:
    now = datetime.now().time()
    start = time(settings.primetime_start_hour, settings.primetime_start_minute)
    end = time(settings.primetime_end_hour, settings.primetime_end_minute)
    return start <= now <= end


def _get_todays_lineup(db: Session) -> list[LineupSlot]:
    today = date.today()
    return (
        db.query(LineupSlot)
        .filter(LineupSlot.date == today)
        .order_by(LineupSlot.position)
        .all()
    )


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def lineup_page(request: Request, db: Session = Depends(get_db)):
    slots = _get_todays_lineup(db)
    total_budget = settings.daily_budget_minutes * 60
    watched_seconds = sum(s.video.duration_seconds for s in slots if s.is_watched)
    total_seconds = sum(s.video.duration_seconds for s in slots)
    budget_used_pct = min(100, int(watched_seconds / total_budget * 100)) if total_budget else 0
    all_watched = bool(slots) and all(s.is_watched for s in slots)

    # Compute per-slot airtimes from primetime start + cumulative durations
    cursor = settings.primetime_start_hour * 60 + settings.primetime_start_minute
    schedule = []
    for slot in slots:
        dur_min = slot.video.duration_seconds // 60
        start_min, end_min = cursor, cursor + dur_min
        cursor = end_min
        schedule.append({
            "slot": slot,
            "start": f"{start_min // 60 % 24:02d}:{start_min % 60:02d}",
            "end":   f"{end_min   // 60 % 24:02d}:{end_min   % 60:02d}",
            "start_min": start_min,
            "end_min":   end_min,
        })

    end_min = settings.primetime_start_hour * 60 + settings.primetime_start_minute + total_seconds // 60
    schedule_end = f"{end_min // 60 % 24:02d}:{end_min % 60:02d}"

    return templates.TemplateResponse(
        request=request,
        name="lineup.html",
        context={
            "schedule": schedule,
            "slots": slots,
            "is_primetime": _is_primetime(),
            "primetime_start": settings.PRIMETIME_START,
            "primetime_end": settings.PRIMETIME_END,
            "total_seconds": total_seconds,
            "budget_used_pct": budget_used_pct,
            "all_watched": all_watched,
            "schedule_end": schedule_end,
            "today": date.today().strftime("%A, %B %d").replace(" 0", " "),
            "quota": quota_status(),
        },
    )


@app.get("/ondemand", response_class=HTMLResponse)
def ondemand_page(request: Request, db: Session = Depends(get_db)):
    backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.is_watched == False)  # noqa: E712
        .order_by(BacklogVideo.added_at.desc())
        .all()
    )
    total_seconds = sum(e.video.duration_seconds for e in backlog)
    imported_count = db.query(ExternalWatch).count()

    return templates.TemplateResponse(
        request=request,
        name="ondemand.html",
        context={
            "backlog": backlog,
            "total_seconds": total_seconds,
            "is_primetime": _is_primetime(),
            "quota": quota_status(),
            "imported_count": imported_count,
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    # Watched lineup slots
    watched_slots = (
        db.query(LineupSlot)
        .filter(LineupSlot.is_watched == True)  # noqa: E712
        .order_by(LineupSlot.watched_at.desc())
        .all()
    )
    # Watched backlog
    watched_backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.is_watched == True)  # noqa: E712
        .order_by(BacklogVideo.watched_at.desc())
        .all()
    )

    # Merge and group by date
    entries = []
    for slot in watched_slots:
        entries.append({
            "video": slot.video,
            "watched_at": slot.watched_at,
            "source": "lineup",
        })
    for be in watched_backlog:
        entries.append({
            "video": be.video,
            "watched_at": be.watched_at,
            "source": "backlog",
        })

    entries.sort(key=lambda e: e["watched_at"] or datetime.min, reverse=True)

    # Group by date
    from itertools import groupby
    def date_key(e):
        if e["watched_at"]:
            return e["watched_at"].date()
        return date.today()

    grouped = []
    for d, group in groupby(entries, key=date_key):
        grouped.append({
            "date": d.strftime("%A, %B %d").replace(" 0", " "),
            "entries": list(group),
        })

    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "grouped": grouped,
            "quota": quota_status(),
        },
    )


# ---------------------------------------------------------------------------
# API / actions
# ---------------------------------------------------------------------------

@app.post("/watch/{youtube_id}")
def mark_watched(youtube_id: str, db: Session = Depends(get_db)):
    """Mark a video as watched. Called via fetch() when user clicks play."""
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    now = datetime.utcnow()
    marked = False

    # Mark in today's lineup if present
    today = date.today()
    slot = (
        db.query(LineupSlot)
        .filter(LineupSlot.date == today, LineupSlot.video_id == video.id)
        .first()
    )
    if slot and not slot.is_watched:
        slot.is_watched = True
        slot.watched_at = now
        marked = True

    # Mark in backlog if present
    backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.video_id == video.id, BacklogVideo.is_watched == False)  # noqa: E712
        .first()
    )
    if backlog:
        backlog.is_watched = True
        backlog.watched_at = now
        marked = True

    db.commit()
    sync_to_gcs()
    return {"ok": True, "marked": marked, "youtube_id": youtube_id}


@app.post("/skip/{youtube_id}")
def skip_video(youtube_id: str, db: Session = Depends(get_db)):
    """Remove an unwatched slot from today's lineup and pull in the top backlog pick."""
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    today = date.today()
    slot = (
        db.query(LineupSlot)
        .filter(
            LineupSlot.date == today,
            LineupSlot.video_id == video.id,
            LineupSlot.is_watched == False,  # noqa: E712
        )
        .first()
    )
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found or already watched")

    # Pull top backlog pick by score (desc), then by added_at (desc) as tiebreaker
    from sqlalchemy import desc as sa_desc
    replacement = (
        db.query(BacklogVideo)
        .join(Video, BacklogVideo.video_id == Video.id)
        .filter(BacklogVideo.is_watched == False)  # noqa: E712
        .order_by(sa_desc(Video.score), sa_desc(BacklogVideo.added_at))
        .first()
    )

    # Permanently dismiss the video so it never resurfaces
    video.dismissed = True
    db.delete(slot)

    # Also remove from backlog if it's sitting there
    existing_backlog = db.query(BacklogVideo).filter(
        BacklogVideo.video_id == video.id,
        BacklogVideo.is_watched == False,  # noqa: E712
    ).first()
    if existing_backlog:
        db.delete(existing_backlog)

    if replacement:
        repl_video = replacement.video
        max_pos = db.query(LineupSlot).filter(LineupSlot.date == today).count()
        new_slot = LineupSlot(
            date=today,
            video_id=repl_video.id,
            position=max_pos,
        )
        db.add(new_slot)
        db.delete(replacement)
        db.commit()
        sync_to_gcs()
        return {
            "ok": True,
            "replaced": True,
            "new_video": {
                "youtube_id": repl_video.youtube_id,
                "title": repl_video.title,
                "channel_name": repl_video.channel_name,
                "duration_seconds": repl_video.duration_seconds,
            },
        }

    db.commit()
    sync_to_gcs()
    return {"ok": True, "replaced": False}


@app.post("/reject/{youtube_id}")
def reject_video(youtube_id: str, db: Session = Depends(get_db)):
    """Reject a backlog video. Stores a record for pattern analysis and removes it from the backlog."""
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Idempotent — don't create a duplicate rejection record
    existing = db.query(RejectedVideo).filter(RejectedVideo.youtube_id == youtube_id).first()
    if not existing:
        db.add(RejectedVideo(
            video_id=video.id,
            youtube_id=video.youtube_id,
            title=video.title,
            channel_name=video.channel_name,
            channel_id=video.channel_id,
            category=video.category,
            duration_seconds=video.duration_seconds,
        ))

    # Remove from backlog
    backlog_entry = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.video_id == video.id, BacklogVideo.is_watched == False)  # noqa: E712
        .first()
    )
    if backlog_entry:
        db.delete(backlog_entry)

    db.commit()
    sync_to_gcs()
    return {"ok": True, "youtube_id": youtube_id}


@app.post("/refresh")
def manual_refresh(request: Request, db: Session = Depends(get_db)):
    """Trigger curation pipeline. Called by Cloud Scheduler in production."""
    if settings.CRON_SECRET:
        if request.headers.get("X-Cron-Secret", "") != settings.CRON_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")
    if not settings.has_youtube_credentials:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No YouTube credentials configured. "
                "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env, "
                "then visit /auth to authenticate."
            },
        )

    from app.curator import run_daily_curation
    try:
        result = run_daily_curation(db)
        sync_to_gcs()
        return result
    except Exception as e:
        logger.error(f"Manual refresh failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/build")
def ui_build(db: Session = Depends(get_db)):
    """Trigger curation from the browser UI. Protected by Basic Auth middleware."""
    if not settings.has_youtube_credentials:
        return JSONResponse(status_code=400, content={"error": "No YouTube credentials configured."})
    from app.curator import run_daily_curation
    try:
        result = run_daily_curation(db)
        sync_to_gcs()
        return result
    except Exception as e:
        logger.error(f"UI build failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/import-history")
async def import_watch_history(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Import YouTube watch history from a Google Takeout watch-history.json file.
    Videos whose IDs are imported will be permanently excluded from future lineups.
    """
    try:
        raw = await file.read()
        data = json.loads(raw)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Could not parse file: {e}"})

    _vid_re = _re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
    imported = 0
    skipped = 0
    for entry in data:
        url = entry.get("titleUrl", "")
        match = _vid_re.search(url)
        if not match:
            continue
        vid_id = match.group(1)
        exists = db.query(ExternalWatch).filter(ExternalWatch.youtube_id == vid_id).first()
        if not exists:
            db.add(ExternalWatch(youtube_id=vid_id))
            imported += 1
        else:
            skipped += 1

    db.commit()
    sync_to_gcs()
    return {"imported": imported, "skipped_duplicates": skipped, "total_entries": len(data)}


@app.get("/api/quota")
def api_quota():
    """Live YouTube API quota usage."""
    q = quota_status()
    pct = round(q["used"] / q["limit"] * 100, 1) if q["limit"] else 0
    return {"used": q["used"], "limit": q["limit"], "pct": pct, "remaining": q["limit"] - q["used"]}


@app.get("/api/status")
def api_status(db: Session = Depends(get_db)):
    """JSON status endpoint."""
    slots = _get_todays_lineup(db)
    total_budget = settings.daily_budget_minutes * 60
    watched_seconds = sum(s.video.duration_seconds for s in slots if s.is_watched)
    remaining_seconds = sum(s.video.duration_seconds for s in slots if not s.is_watched)

    backlog_count = db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).count()  # noqa: E712

    return {
        "today": str(date.today()),
        "is_primetime": _is_primetime(),
        "primetime_window": f"{settings.PRIMETIME_START}–{settings.PRIMETIME_END}",
        "lineup_count": len(slots),
        "watched_count": sum(1 for s in slots if s.is_watched),
        "remaining_count": sum(1 for s in slots if not s.is_watched),
        "watched_seconds": watched_seconds,
        "remaining_seconds": remaining_seconds,
        "budget_seconds": total_budget,
        "backlog_count": backlog_count,
        "has_credentials": settings.has_youtube_credentials,
        "has_token": __import__("os").path.exists(settings.TOKEN_FILE),
    }


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

@app.get("/auth")
def auth_start():
    """Start OAuth consent flow. Visit this in browser to authenticate."""
    if not settings.has_youtube_credentials:
        return JSONResponse(
            status_code=400,
            content={
                "error": "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first."
            },
        )

    from app.youtube import get_auth_flow
    flow = get_auth_flow(redirect_uri=settings.OAUTH_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return RedirectResponse(url=auth_url)


@app.get("/auth/callback")
def auth_callback(code: str, state: Optional[str] = None):
    """OAuth callback. Google redirects here after consent."""
    from app.youtube import exchange_code_for_token
    try:
        exchange_code_for_token(code=code, state=state or "")
        return HTMLResponse(
            "<h2>Authentication successful!</h2>"
            "<p>token.json saved. You can now <a href='/'>return to the lineup</a> "
            "and run <code>POST /refresh</code> to fetch videos.</p>"
        )
    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"OAuth failed: {e}")
