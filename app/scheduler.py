"""
APScheduler background job — runs daily curation at 18:00 local time.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _curation_job():
    """Wrapper so the scheduler can import without circular issues."""
    from app.database import SessionLocal
    from app.curator import run_daily_curation

    db = SessionLocal()
    try:
        result = run_daily_curation(db)
        logger.info(f"Scheduled curation complete: {result}")
    except Exception as e:
        logger.error(f"Scheduled curation failed: {e}", exc_info=True)
    finally:
        db.close()


def start_scheduler():
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        return

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _curation_job,
        trigger=CronTrigger(hour=18, minute=0),
        id="daily_curation",
        name="Daily lineup curation",
        replace_existing=True,
        misfire_grace_time=3600,  # Run if missed by up to 1 hour
    )
    _scheduler.start()
    logger.info("Scheduler started. Daily curation at 18:00.")


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
