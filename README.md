# Lineup

A personal YouTube curator. Builds a daily 1.5-hour watchlist from your subscriptions and curated topics, available only during your set evening window. Inspired by TV schedules — content is waiting for you, not the other way around.

---

## How it works

- Runs a curation job daily at 6:00 PM, pulling from your YouTube subscriptions + topic searches
- Filters out shorts, reaction videos, clickbait titles, and anything outside a sane duration range
- Scores remaining videos by recency, duration sweet spot, and channel trust
- Builds a ~1.5hr lineup, locked until 7:00 PM
- Leftover quality videos go to an on-demand backlog
- Tracks watch history so nothing resurfaces

---

## Setup

### 1. Get YouTube OAuth credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. In the left sidebar: **APIs & Services → Library**
4. Search for **YouTube Data API v3** → Enable it
5. Go to **APIs & Services → OAuth consent screen**
   - Choose **External**, fill in app name and your email, save
6. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Add authorized redirect URI: `http://localhost:8000/auth/callback`
   - Click Create
7. Copy the **Client ID** and **Client Secret**

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
YOUTUBE_CLIENT_ID=your_client_id_here
YOUTUBE_CLIENT_SECRET=your_client_secret_here
```

The other defaults are fine to start:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./lineup.db` | Local SQLite database |
| `PRIMETIME_START` | `19:00` | When the lineup unlocks |
| `PRIMETIME_END` | `22:00` | End of viewing window |
| `DAILY_BUDGET_MINUTES` | `90` | Max content per day |

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python run.py
```

App starts at [http://localhost:8000](http://localhost:8000)

### 5. Authenticate with YouTube

Visit [http://localhost:8000/auth](http://localhost:8000/auth) — this opens Google's OAuth consent screen. Sign in with the Google account that has your YouTube subscriptions. A `token.json` file is saved locally. You only need to do this once.

### 6. Build your first lineup

Click **"Build today's lineup"** on the homepage, or send:

```bash
curl -X POST http://localhost:8000/refresh
```

---

## Customising content

### Add trusted channels (always included)

Open `app/youtube.py` and add channel IDs to `CHANNEL_WHITELIST`:

```python
CHANNEL_WHITELIST: list[str] = [
    "UCVq_s-I8L-yOLVpMEMqo7dw",  # About That (Andrew Chang)
    "UCeY0bbntWzzVIaj2z3QigXg",  # Good Work
]
```

To find a channel ID: go to the channel on YouTube, view page source, search for `"channelId"`.

### Edit topic searches

In `app/youtube.py`, edit `TOPIC_QUERIES`:

```python
TOPIC_QUERIES = [
    "geopolitics explained 2025",
    "official movie trailer 2025",
    "philosophy explained",
    # add or remove as needed
]
```

### Adjust filters

In `app/curator.py`:

```python
MIN_DURATION = 90        # skip anything under 1.5 min
MAX_DURATION = 40 * 60  # skip anything over 40 min
CAPS_THRESHOLD = 0.40   # filter titles with >40% uppercase letters
```

Add blacklist patterns to `TITLE_BLACKLIST_PATTERNS` to filter more aggressively.

---

## Pages

| Route | Description |
|---|---|
| `/` | Today's lineup |
| `/ondemand` | Quality backlog — use it deliberately |
| `/history` | Everything you've watched |
| `/api/status` | JSON status (credentials, lineup count, time remaining) |
| `/auth` | Start YouTube OAuth flow |
| `/refresh` | Manually trigger curation (POST) |

---

## Database

SQLite by default (`lineup.db` in the project root). Three tables:

- **videos** — every fetched video with metadata and score
- **lineup_slots** — today's ordered watchlist
- **backlog_videos** — overflow quality videos

To use Postgres (e.g. when deploying), set `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

---

## Project structure

```
content-scheduler/
├── app/
│   ├── main.py         # FastAPI routes
│   ├── curator.py      # Scoring, filtering, lineup builder
│   ├── youtube.py      # YouTube API client + OAuth
│   ├── scheduler.py    # Daily curation job (runs at 18:00)
│   ├── models.py       # SQLAlchemy models
│   ├── database.py     # DB engine + session
│   └── templates/      # Jinja2 HTML templates
├── static/
│   └── style.css       # Scandinavian aesthetic, dark mode
├── config.py           # Settings from environment
├── run.py              # Entry point
└── .env.example        # Environment variable template
```

---

## Deployment

Coming soon — Railway or Render, ~$5–10/mo, zero maintenance.
