# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Lineup** is a personal YouTube curator that builds a daily 1.5-hour watchlist from subscriptions and curated topics, available only during a configurable evening primetime window (19:00–22:00 by default). It's inspired by TV schedules — content waits for the user.

## Running the App

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp .env.example .env

# Run the app (http://localhost:8000)
python run.py
```

First-time OAuth setup: visit `http://localhost:8000/auth` and sign in with Google. This creates `token.json` locally.

To trigger curation manually without waiting for the scheduler:
```bash
curl -X POST http://localhost:8000/refresh
```

There are no test, lint, or build commands — the project has no test suite or CI configuration.

## Environment Variables

Required in `.env`:
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` — from Google Cloud Console (YouTube Data API v3, OAuth 2.0 Web Client)
- `DATABASE_URL` — defaults to `sqlite:///./lineup.db`; supports PostgreSQL for deployment
- `PRIMETIME_START` / `PRIMETIME_END` — viewing window (default `19:00`/`22:00`)
- `DAILY_BUDGET_MINUTES` — max content per day (default `90`)

OAuth redirect URI to register in Google Cloud: `http://localhost:8000/auth/callback`

## Architecture

### Curation Pipeline (the core)

The pipeline runs daily at 18:00 via APScheduler, or on-demand via `POST /refresh`:

1. **Fetch** (`app/youtube.py`) — pulls recent videos from YouTube subscriptions and topic search queries
2. **Filter** (`app/curator.py: filter_videos()`) — removes shorts, reaction content, excessive-caps titles, out-of-range durations, and already-watched videos
3. **Score** (`app/curator.py: score_video()`) — rates 0.0–1.0+ on recency, channel whitelist, duration sweet spot (10–25 min), and category
4. **Build** (`app/curator.py: build_lineup()`) — greedy selection of highest-scored videos until the 90-minute budget is met
5. **Persist** — lineup saved to `lineup_slots` table; overflow quality videos go to `backlog_videos` for on-demand viewing

### Database (SQLAlchemy + SQLite/PostgreSQL)

Three tables defined in `app/models.py`:
- `videos` — all fetched videos with metadata and computed score
- `lineup_slots` — today's ordered watchlist
- `backlog_videos` — overflow videos available on-demand

Session management in `app/database.py`; engine configured from `DATABASE_URL`.

### Web Layer (FastAPI + Jinja2)

All routes in `app/main.py`:
- `GET /` — today's lineup (gated by primetime window)
- `GET /ondemand` — backlog videos
- `GET /history` — watch history
- `POST /watch/{id}` — mark video watched
- `POST /refresh` — manual curation trigger
- `GET /api/status` — JSON status
- `GET /auth` + `GET /auth/callback` — Google OAuth flow

Templates in `app/templates/`, static assets in `static/style.css`.

### Key Customization Points

- `app/youtube.py` — `CHANNEL_WHITELIST` (always-included channels) and `TOPIC_QUERIES` (search terms)
- `app/curator.py` — `MIN_DURATION`, `MAX_DURATION`, `CAPS_THRESHOLD`, `TITLE_BLACKLIST_PATTERNS` filter thresholds
- `config.py` — all settings loaded from environment via dotenv
