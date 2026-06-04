import logging
from datetime import date, datetime, time
from itertools import groupby
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


@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()
    if not settings.has_youtube_credentials:
        logger.warning("No YouTube credentials. Visit /auth to authenticate.")


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


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
    return (
        db.query(LineupSlot)
        .filter(LineupSlot.date == date.today())
        .order_by(LineupSlot.position)
        .all()
    )


@app.get("/", response_class=HTMLResponse)
def lineup_page(request: Request, db: Session = Depends(get_db)):
    slots = _get_todays_lineup(db)
    total_budget = settings.DAILY_BUDGET_MINUTES * 60
    watched_seconds = sum(s.video.duration_seconds for s in slots if s.is_watched)
    total_seconds = sum(s.video.duration_seconds for s in slots)
    budget_used_pct = min(100, int(watched_seconds / total_budget * 100)) if total_budget else 0
    return templates.TemplateResponse("lineup.html", {
        "request": request,
        "slots": slots,
        "is_primetime": _is_primetime(),
        "primetime_start": settings.PRIMETIME_START,
        "primetime_end": settings.PRIMETIME_END,
        "total_budget_minutes": settings.DAILY_BUDGET_MINUTES,
        "watched_seconds": watched_seconds,
        "remaining_seconds": total_seconds - watched_seconds,
        "total_seconds": total_seconds,
        "budget_used_pct": budget_used_pct,
        "all_watched": bool(slots and all(s.is_watched for s in slots)),
        "format_duration": _format_duration,
        "today": date.today().strftime("%A, %B %-d"),
    })


@app.get("/ondemand", response_class=HTMLResponse)
def ondemand_page(request: Request, db: Session = Depends(get_db)):
    backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.is_watched == False)  # noqa: E712
        .order_by(BacklogVideo.added_at.desc())
        .all()
    )
    return templates.TemplateResponse("ondemand.html", {
        "request": request,
        "backlog": backlog,
        "total_seconds": sum(e.video.duration_seconds for e in backlog),
        "format_duration": _format_duration,
        "is_primetime": _is_primetime(),
    })


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db)):
    entries = []
    for slot in db.query(LineupSlot).filter(LineupSlot.is_watched == True).order_by(LineupSlot.watched_at.desc()).all():  # noqa: E712
        entries.append({"video": slot.video, "watched_at": slot.watched_at, "source": "lineup"})
    for be in db.query(BacklogVideo).filter(BacklogVideo.is_watched == True).order_by(BacklogVideo.watched_at.desc()).all():  # noqa: E712
        entries.append({"video": be.video, "watched_at": be.watched_at, "source": "backlog"})
    entries.sort(key=lambda e: e["watched_at"] or datetime.min, reverse=True)

    def date_key(e):
        return e["watched_at"].date() if e["watched_at"] else date.today()

    grouped = [{"date": d.strftime("%A, %B %-d"), "entries": list(g)} for d, g in groupby(entries, key=date_key)]
    return templates.TemplateResponse("history.html", {
        "request": request, "grouped": grouped, "format_duration": _format_duration,
    })


@app.post("/watch/{youtube_id}")
def mark_watched(youtube_id: str, db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    now = datetime.utcnow()
    slot = db.query(LineupSlot).filter(
        LineupSlot.date == date.today(), LineupSlot.video_id == video.id
    ).first()
    if slot and not slot.is_watched:
        slot.is_watched = True
        slot.watched_at = now
    backlog = db.query(BacklogVideo).filter(
        BacklogVideo.video_id == video.id, BacklogVideo.is_watched == False  # noqa: E712
    ).first()
    if backlog:
        backlog.is_watched = True
        backlog.watched_at = now
    db.commit()
    return {"ok": True, "youtube_id": youtube_id}


@app.post("/refresh")
def manual_refresh(db: Session = Depends(get_db)):
    if not settings.has_youtube_credentials:
        return JSONResponse(status_code=400, content={
            "error": "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env, then visit /auth."
        })
    from app.curator import run_daily_curation
    try:
        return run_daily_curation(db)
    except Exception as e:
        logger.error(f"Manual refresh failed: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/status")
def api_status(db: Session = Depends(get_db)):
    slots = _get_todays_lineup(db)
    total_budget = settings.DAILY_BUDGET_MINUTES * 60
    return {
        "today": str(date.today()),
        "is_primetime": _is_primetime(),
        "primetime_window": f"{settings.PRIMETIME_START}-{settings.PRIMETIME_END}",
        "lineup_count": len(slots),
        "watched_count": sum(1 for s in slots if s.is_watched),
        "watched_seconds": sum(s.video.duration_seconds for s in slots if s.is_watched),
        "remaining_seconds": sum(s.video.duration_seconds for s in slots if not s.is_watched),
        "budget_seconds": total_budget,
        "backlog_count": db.query(BacklogVideo).filter(BacklogVideo.is_watched == False).count(),  # noqa: E712
        "has_credentials": settings.has_youtube_credentials,
        "has_token": __import__("os").path.exists("token.json"),
    }


@app.get("/auth")
def auth_start():
    if not settings.has_youtube_credentials:
        return JSONResponse(status_code=400, content={"error": "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first."})
    from app.youtube import get_auth_flow
    flow = get_auth_flow()
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return RedirectResponse(url=auth_url)


@app.get("/auth/callback")
def auth_callback(code: str, state: Optional[str] = None):
    from app.youtube import exchange_code_for_token
    try:
        exchange_code_for_token(code=code, state=state or "")
        return HTMLResponse(
            "<h2>Authenticated!</h2><p><a href='/'>Return to lineup</a>, then hit 'Build today's lineup'.</p>"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth failed: {e}")
