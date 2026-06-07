"""
FastAPI application — routes, templates, OAuth flow, session auth.
"""

import base64
import hashlib
import hmac
import json as _json
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
from app.models import Video, LineupSlot, BacklogVideo, ExternalWatch, RejectedVideo, User
from app.scheduler import start_scheduler, stop_scheduler
from app.youtube import quota_status
from config import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="Lineup", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.cache = None


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


templates.env.globals["format_duration"] = _format_duration


# ---------------------------------------------------------------------------
# Session helpers (HMAC-signed cookie, no extra deps)
# ---------------------------------------------------------------------------

def _sign_session(user_id: int) -> str:
    payload = _json.dumps({"uid": user_id}).encode()
    sig = hmac.new(settings.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"|" + sig).decode()


def _verify_session(token: str) -> int:
    data = base64.urlsafe_b64decode(token.encode() + b"==")
    payload, sig = data.rsplit(b"|", 1)
    expected = hmac.new(settings.SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid session signature")
    return _json.loads(payload)["uid"]


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("lineup_session")
    if not token:
        raise NotAuthenticated()
    try:
        user_id = _verify_session(token)
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise NotAuthenticated()
        return user
    except NotAuthenticated:
        raise
    except Exception:
        raise NotAuthenticated()


# ---------------------------------------------------------------------------
# Basic Auth middleware (URL-level protection)
# ---------------------------------------------------------------------------

_UNPROTECTED = {"/auth/callback", "/refresh", "/login", "/auth", "/static"}


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not settings.APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path in _UNPROTECTED or path.startswith("/static"):
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
# HTTP exception → styled error page
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
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
        logger.warning("No YouTube credentials found. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET.")


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


def _get_todays_lineup(db: Session, user_id: int) -> list[LineupSlot]:
    today = date.today()
    return (
        db.query(LineupSlot)
        .filter(LineupSlot.date == today, LineupSlot.user_id == user_id)
        .order_by(LineupSlot.position)
        .all()
    )


def _get_user_creds(user: User):
    """Load and refresh credentials for a user. Updates user.token_json if refreshed."""
    if not user.token_json:
        return None
    from app.youtube import credentials_from_json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GRequest
    import json as _j
    try:
        creds = Credentials.from_authorized_user_info(_j.loads(user.token_json))
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            user.token_json = creds.to_json()
        except Exception as e:
            logger.warning("Could not refresh user %d credentials: %s", user.id, e)
            return None
    return creds if (creds and creds.valid) else None


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.get("/auth")
def auth_start(request: Request):
    """Start OAuth consent flow."""
    # If already authenticated, go home
    token = request.cookies.get("lineup_session")
    if token:
        try:
            _verify_session(token)
            return RedirectResponse("/", status_code=303)
        except Exception:
            pass

    if not settings.has_youtube_credentials:
        raise HTTPException(status_code=500, detail="YouTube credentials not configured on the server.")

    from app.youtube import get_auth_flow
    flow = get_auth_flow(redirect_uri=settings.OAUTH_REDIRECT_URI)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return RedirectResponse(url=auth_url)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str, state: Optional[str] = None, db: Session = Depends(get_db)):
    """OAuth callback — Google redirects here after consent."""
    from app.youtube import exchange_code_for_token
    try:
        token_json_str, userinfo = exchange_code_for_token(code=code, state=state or "")
    except Exception as e:
        logger.error("OAuth callback error: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"OAuth failed: {e}")

    google_sub = userinfo.get("id") or userinfo.get("sub", "")
    email = userinfo.get("email", "")
    name = userinfo.get("name", "")

    # Find or create user
    user = db.query(User).filter(User.google_sub == google_sub).first()
    is_new_user = user is None
    if is_new_user:
        user = User(google_sub=google_sub, email=email, name=name, token_json=token_json_str)
        db.add(user)
        db.flush()  # get user.id without full commit
    else:
        user.token_json = token_json_str
        user.email = email
        user.name = name

    # Attribute any existing orphaned data to the first user who logs in
    if is_new_user:
        for Model in [LineupSlot, BacklogVideo, ExternalWatch, RejectedVideo]:
            db.query(Model).filter(Model.user_id == None).update(  # noqa: E711
                {"user_id": user.id}, synchronize_session=False
            )

    db.commit()
    sync_to_gcs()

    is_secure = request.url.scheme == "https"
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key="lineup_session",
        value=_sign_session(user.id),
        max_age=86400 * 30,
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )
    return response


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def lineup_page(request: Request, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    slots = _get_todays_lineup(db, current_user.id)
    total_budget = settings.daily_budget_minutes * 60
    watched_seconds = sum(s.video.duration_seconds for s in slots if s.is_watched)
    total_seconds = sum(s.video.duration_seconds for s in slots)
    budget_used_pct = min(100, int(watched_seconds / total_budget * 100)) if total_budget else 0
    all_watched = bool(slots) and all(s.is_watched for s in slots)

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
            "current_user": current_user,
        },
    )


@app.get("/ondemand", response_class=HTMLResponse)
def ondemand_page(request: Request, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.is_watched == False, BacklogVideo.user_id == current_user.id)  # noqa: E712
        .order_by(BacklogVideo.added_at.desc())
        .all()
    )
    total_seconds = sum(e.video.duration_seconds for e in backlog)
    imported_count = db.query(ExternalWatch).filter(ExternalWatch.user_id == current_user.id).count()

    return templates.TemplateResponse(
        request=request,
        name="ondemand.html",
        context={
            "backlog": backlog,
            "total_seconds": total_seconds,
            "is_primetime": _is_primetime(),
            "quota": quota_status(),
            "imported_count": imported_count,
            "current_user": current_user,
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    watched_slots = (
        db.query(LineupSlot)
        .filter(LineupSlot.is_watched == True, LineupSlot.user_id == current_user.id)  # noqa: E712
        .order_by(LineupSlot.watched_at.desc())
        .all()
    )
    watched_backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.is_watched == True, BacklogVideo.user_id == current_user.id)  # noqa: E712
        .order_by(BacklogVideo.watched_at.desc())
        .all()
    )

    entries = []
    for slot in watched_slots:
        entries.append({"video": slot.video, "watched_at": slot.watched_at, "source": "lineup"})
    for be in watched_backlog:
        entries.append({"video": be.video, "watched_at": be.watched_at, "source": "backlog"})
    entries.sort(key=lambda e: e["watched_at"] or datetime.min, reverse=True)

    from itertools import groupby

    def date_key(e):
        return e["watched_at"].date() if e["watched_at"] else date.today()

    grouped = []
    for d, group in groupby(entries, key=date_key):
        grouped.append({
            "date": d.strftime("%A, %B %d").replace(" 0", " "),
            "entries": list(group),
        })

    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"grouped": grouped, "quota": quota_status(), "current_user": current_user},
    )


# ---------------------------------------------------------------------------
# API / actions
# ---------------------------------------------------------------------------

@app.post("/watch/{youtube_id}")
def mark_watched(youtube_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    now = datetime.utcnow()
    marked = False
    today = date.today()
    slot = (
        db.query(LineupSlot)
        .filter(LineupSlot.date == today, LineupSlot.video_id == video.id,
                LineupSlot.user_id == current_user.id)
        .first()
    )
    if slot and not slot.is_watched:
        slot.is_watched = True
        slot.watched_at = now
        marked = True

    backlog = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.video_id == video.id, BacklogVideo.is_watched == False,  # noqa: E712
                BacklogVideo.user_id == current_user.id)
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
def skip_video(youtube_id: str, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
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
            LineupSlot.user_id == current_user.id,
        )
        .first()
    )
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found or already watched")

    from sqlalchemy import desc as sa_desc
    replacement = (
        db.query(BacklogVideo)
        .join(Video, BacklogVideo.video_id == Video.id)
        .filter(BacklogVideo.is_watched == False, BacklogVideo.user_id == current_user.id)  # noqa: E712
        .order_by(sa_desc(Video.score), sa_desc(BacklogVideo.added_at))
        .first()
    )

    video.dismissed = True
    db.delete(slot)

    existing_backlog = db.query(BacklogVideo).filter(
        BacklogVideo.video_id == video.id,
        BacklogVideo.is_watched == False,  # noqa: E712
        BacklogVideo.user_id == current_user.id,
    ).first()
    if existing_backlog:
        db.delete(existing_backlog)

    if replacement:
        repl_video = replacement.video
        max_pos = db.query(LineupSlot).filter(
            LineupSlot.date == today, LineupSlot.user_id == current_user.id
        ).count()
        new_slot = LineupSlot(date=today, video_id=repl_video.id, position=max_pos,
                              user_id=current_user.id)
        db.add(new_slot)
        db.delete(replacement)
        db.commit()
        sync_to_gcs()
        return {
            "ok": True, "replaced": True,
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
def reject_video(youtube_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    video = db.query(Video).filter(Video.youtube_id == youtube_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    existing = db.query(RejectedVideo).filter(
        RejectedVideo.youtube_id == youtube_id,
        RejectedVideo.user_id == current_user.id,
    ).first()
    if not existing:
        db.add(RejectedVideo(
            video_id=video.id,
            youtube_id=video.youtube_id,
            title=video.title,
            channel_name=video.channel_name,
            channel_id=video.channel_id,
            category=video.category,
            duration_seconds=video.duration_seconds,
            user_id=current_user.id,
        ))

    backlog_entry = (
        db.query(BacklogVideo)
        .filter(BacklogVideo.video_id == video.id, BacklogVideo.is_watched == False,  # noqa: E712
                BacklogVideo.user_id == current_user.id)
        .first()
    )
    if backlog_entry:
        db.delete(backlog_entry)

    db.commit()
    sync_to_gcs()
    return {"ok": True, "youtube_id": youtube_id}


@app.post("/refresh")
def manual_refresh(request: Request, db: Session = Depends(get_db)):
    """Trigger curation pipeline via Cloud Scheduler (X-Cron-Secret auth)."""
    if settings.CRON_SECRET:
        if request.headers.get("X-Cron-Secret", "") != settings.CRON_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")
    if not settings.has_youtube_credentials:
        return JSONResponse(status_code=400, content={"error": "No YouTube credentials configured."})

    from app.curator import run_daily_curation
    # Run curation for all users
    users = db.query(User).filter(User.token_json != None).all()  # noqa: E711
    if not users:
        return JSONResponse(status_code=400, content={"error": "No authenticated users yet."})

    results = []
    for user in users:
        creds = _get_user_creds(user)
        db.commit()  # save any refreshed token_json
        if not creds:
            results.append({"user": user.email, "error": "invalid credentials"})
            continue
        try:
            r = run_daily_curation(db, user_id=user.id, creds=creds)
            sync_to_gcs()
            results.append({"user": user.email, **r})
        except Exception as e:
            logger.error("Curation failed for %s: %s", user.email, e, exc_info=True)
            results.append({"user": user.email, "error": str(e)})

    if len(results) == 1:
        return results[0]
    return {"results": results}


@app.post("/api/build")
def ui_build(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Trigger curation from the browser UI. Protected by session auth."""
    if not settings.has_youtube_credentials:
        return JSONResponse(status_code=400, content={"error": "No YouTube credentials configured."})
    creds = _get_user_creds(current_user)
    db.commit()
    if not creds:
        return JSONResponse(status_code=401, content={"error": "YouTube not connected. Visit /auth."})
    from app.curator import run_daily_curation
    try:
        result = run_daily_curation(db, user_id=current_user.id, creds=creds)
        sync_to_gcs()
        return result
    except Exception as e:
        logger.error("UI build failed for %s: %s", current_user.email, e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/import-history")
async def import_watch_history(file: UploadFile = File(...), db: Session = Depends(get_db),
                               current_user: User = Depends(get_current_user)):
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
        exists = db.query(ExternalWatch).filter(
            ExternalWatch.youtube_id == vid_id,
            ExternalWatch.user_id == current_user.id,
        ).first()
        if not exists:
            db.add(ExternalWatch(youtube_id=vid_id, user_id=current_user.id))
            imported += 1
        else:
            skipped += 1

    db.commit()
    sync_to_gcs()
    return {"imported": imported, "skipped_duplicates": skipped, "total_entries": len(data)}


@app.get("/api/quota")
def api_quota():
    q = quota_status()
    pct = round(q["used"] / q["limit"] * 100, 1) if q["limit"] else 0
    return {"used": q["used"], "limit": q["limit"], "pct": pct, "remaining": q["limit"] - q["used"]}


@app.get("/api/status")
def api_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    slots = _get_todays_lineup(db, current_user.id)
    total_budget = settings.daily_budget_minutes * 60
    watched_seconds = sum(s.video.duration_seconds for s in slots if s.is_watched)
    remaining_seconds = sum(s.video.duration_seconds for s in slots if not s.is_watched)
    backlog_count = db.query(BacklogVideo).filter(
        BacklogVideo.is_watched == False, BacklogVideo.user_id == current_user.id  # noqa: E712
    ).count()

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
        "has_token": bool(current_user.token_json),
        "user_email": current_user.email,
    }
