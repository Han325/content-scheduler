import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    YOUTUBE_CLIENT_ID: str = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET: str = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./lineup.db")
    TOKEN_FILE: str = os.getenv("TOKEN_FILE", "token.json")
    PRIMETIME_START: str = os.getenv("PRIMETIME_START", "19:00")
    PRIMETIME_END: str = os.getenv("PRIMETIME_END", "22:00")
    # Set to the Cloud Run service URL/auth/callback when deployed
    OAUTH_REDIRECT_URI: str = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")
    # Set to true on Cloud Run — Cloud Scheduler replaces APScheduler
    DISABLE_SCHEDULER: bool = os.getenv("DISABLE_SCHEDULER", "false").lower() == "true"
    # Secret sent by Cloud Scheduler in X-Cron-Secret header to protect /refresh
    CRON_SECRET: str = os.getenv("CRON_SECRET", "")
    # Password for HTTP Basic Auth — set this to lock the app to just you
    APP_PASSWORD: str = os.getenv("APP_PASSWORD", "")
    # Secret for signing session cookies — must be set in production
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "dev-secret-change-in-production")
    @property
    def daily_budget_minutes(self) -> int:
        env_val = os.getenv("DAILY_BUDGET_MINUTES")
        if env_val:
            return int(env_val)
        # Default: fill the full primetime window
        end = self.primetime_end_hour * 60 + self.primetime_end_minute
        start = self.primetime_start_hour * 60 + self.primetime_start_minute
        return end - start

    @property
    def has_youtube_credentials(self) -> bool:
        return bool(self.YOUTUBE_CLIENT_ID and self.YOUTUBE_CLIENT_SECRET)

    @property
    def primetime_start_hour(self) -> int:
        return int(self.PRIMETIME_START.split(":")[0])

    @property
    def primetime_start_minute(self) -> int:
        return int(self.PRIMETIME_START.split(":")[1])

    @property
    def primetime_end_hour(self) -> int:
        return int(self.PRIMETIME_END.split(":")[0])

    @property
    def primetime_end_minute(self) -> int:
        return int(self.PRIMETIME_END.split(":")[1])


settings = Settings()
