import logging
import os
import shutil

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings

logger = logging.getLogger(__name__)

# GCS FUSE and SQLite are incompatible — SQLite's journal does random writes
# while GCS FUSE requires sequential writes, causing cascading 429s.
# Fix: run SQLite on local /tmp (fast), sync to GCS only after bulk writes.
_GCS_DB_PATH = os.getenv("GCS_DB_PATH", "")
_LOCAL_DB_PATH = "/tmp/lineup.db"

_db_url = f"sqlite:////{_LOCAL_DB_PATH}" if _GCS_DB_PATH else settings.DATABASE_URL

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if "sqlite" in _db_url else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def load_from_gcs():
    """Copy DB from GCS mount to /tmp at startup. No-op if GCS_DB_PATH is unset."""
    if not _GCS_DB_PATH:
        return
    if os.path.exists(_GCS_DB_PATH) and os.path.getsize(_GCS_DB_PATH) > 0:
        shutil.copy2(_GCS_DB_PATH, _LOCAL_DB_PATH)
        logger.info("Loaded DB from GCS (%d bytes)", os.path.getsize(_LOCAL_DB_PATH))
    else:
        logger.info("No existing GCS DB — starting fresh at %s", _LOCAL_DB_PATH)


def sync_to_gcs():
    """Copy DB from /tmp back to GCS mount after writes. No-op if GCS_DB_PATH is unset."""
    if not _GCS_DB_PATH or not os.path.exists(_LOCAL_DB_PATH):
        return
    shutil.copy2(_LOCAL_DB_PATH, _GCS_DB_PATH)
    logger.info("Synced DB to GCS (%d bytes)", os.path.getsize(_LOCAL_DB_PATH))


def init_db():
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import text
    _migrations = [
        "ALTER TABLE videos ADD COLUMN dismissed BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE lineup_slots ADD COLUMN user_id INTEGER",
        "ALTER TABLE backlog_videos ADD COLUMN user_id INTEGER",
        "ALTER TABLE external_watches ADD COLUMN user_id INTEGER",
        "ALTER TABLE rejected_videos ADD COLUMN user_id INTEGER",
        # ExternalWatch previously had a unique constraint on youtube_id alone;
        # with multi-user the same video ID can appear for different users.
        # SQLite doesn't support DROP CONSTRAINT, so we leave it — the unique
        # index was never created via SQLAlchemy (it was just a column kwarg).
    ]
    with engine.connect() as conn:
        for sql in _migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column / constraint already exists
