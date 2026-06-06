from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    # Import models to register them with Base before creating tables
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # Safe migration: add dismissed column if it doesn't exist yet
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE videos ADD COLUMN dismissed BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        except Exception:
            pass  # column already exists
