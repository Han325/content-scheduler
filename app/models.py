from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Date, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    google_sub = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=False)
    name = Column(String, default="")
    token_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    youtube_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=False)
    channel_name = Column(String, nullable=False)
    channel_id = Column(String, nullable=False)
    duration_seconds = Column(Integer, nullable=False)
    category = Column(String, default="general")
    thumbnail_url = Column(String, default="")
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    score = Column(Float, default=0.0)
    dismissed = Column(Boolean, default=False, server_default="0")

    lineup_slots = relationship("LineupSlot", back_populates="video")
    backlog_entries = relationship("BacklogVideo", back_populates="video")


class LineupSlot(Base):
    __tablename__ = "lineup_slots"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    position = Column(Integer, nullable=False)
    is_watched = Column(Boolean, default=False)
    watched_at = Column(DateTime, nullable=True)
    user_id = Column(Integer, nullable=True, index=True)

    video = relationship("Video", back_populates="lineup_slots")


class BacklogVideo(Base):
    __tablename__ = "backlog_videos"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)
    is_watched = Column(Boolean, default=False)
    watched_at = Column(DateTime, nullable=True)
    user_id = Column(Integer, nullable=True, index=True)

    video = relationship("Video", back_populates="backlog_entries")


class ExternalWatch(Base):
    """YouTube video IDs imported from Google Takeout watch history."""
    __tablename__ = "external_watches"

    id = Column(Integer, primary_key=True, index=True)
    youtube_id = Column(String, index=True, nullable=False)
    imported_at = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, nullable=True, index=True)


class RejectedVideo(Base):
    """Videos explicitly rejected from the on-demand list."""
    __tablename__ = "rejected_videos"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    rejected_at = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, nullable=True, index=True)

    youtube_id = Column(String, index=True, nullable=False)
    title = Column(String, nullable=False)
    channel_name = Column(String, nullable=False)
    channel_id = Column(String, nullable=False)
    category = Column(String, default="general")
    duration_seconds = Column(Integer, nullable=False)

    video = relationship("Video")
