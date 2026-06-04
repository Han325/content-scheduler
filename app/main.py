"""
FastAPI application — routes, templates, OAuth flow.
"""

import logging
from datetime import date, datetime, time
from typing import Optional

from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models import Video, LineupSlot, BacklogVideo
from app.scheduler import start_scheduler, stop_scheduler
from config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Content Scheduler", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()
    if not settings.has_youtube_credentials:
        logger.warning(
            "No YouTube credentials found in environment. "
            "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET, then visit /auth."
        )


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_primetime() -> bool:
    now = datetime.now().time()
    start = time(settings.primetime_start_hour, settings.primetime_start_minute)
    end = time(settings.primetime_end_hour, settings.primetime_end_minute)
    return start <= now <= end


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


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
    total_budget = settings.DAILY_BUDGET_MINUTES * 60
    watched_seconds = sum(
        s.video.duration_seconds for s in slots if s.is_watched
    )
    remaining_seconds = sum(
        s.video.duration_seconds for s in slots if not s.is_watched
    )
    total_seconds = sum(s.video.duration_seconds for s in slots)
    budget_used_pct = min(100, int(watched_seconds / total_budget * 100)) if total_budget else 0
    all_watched = slots and all(s.is_watched for s in slots)

    return templates.TemplateResponse(
        "lineup.html",
        {
            "request": request,
            "slots": slots,
            "is_primetime": _is_primetime(),
            "primetime_start": settings.PRIMETIME_START,
            "primetime_end": settings.PRIMETIME_END,
            "total_budget_minutes": settings.DAILY_BUDGET_MINUTES,
            "watched_seconds": watched_seconds,
            "remaining_seconds": remaining_seconds,
            "total_seconds": total_seconds,
            "budget_used_pct": budget_used_pct,
            "all_watched": all_watched,
            "format_duration": _format_duration,
            "today": date.today().strftime("%A, %B %d").replace(" 0", " "),
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

    return templates.TemplateResponse(
        "ondemand.html",
        {
            "request": request,
            "backlog": backlog,
            "total_seconds": total_seconds,
            "format_duration": _format_duration,
            "is_primetime": _is_primetime(),
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
        "history.html",
        {
            "request": request,
            "grouped": grouped,
            "format_duration": _format_duration,
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
    return {"ok": True, "marked": marked, "youtube_id": youtube_id}


@app.post("/refresh")
def manual_refresh(db: Session = Depends(get_db)):
    """Manually trigger curation pipeline. For dev/testing."""
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
        return result
    except Exception as e:
        logger.error(f"Manual refresh failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/status")
def api_status(db: Session = Depends(get_db)):
    """JSON status endpoint."""
    slots = _get_todays_lineup(db)
    total_budget = settings.DAILY_BUDGET_MINUTES * 60
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
        "has_token": __import__("os").path.exists("token.json"),
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
    flow = get_auth_flow()
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
