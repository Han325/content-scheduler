import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    YOUTUBE_CLIENT_ID: str = os.getenv("YOUTUBE_CLIENT_ID", "")
    YOUTUBE_CLIENT_SECRET: str = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./lineup.db")
    PRIMETIME_START: str = os.getenv("PRIMETIME_START", "19:00")
    PRIMETIME_END: str = os.getenv("PRIMETIME_END", "22:00")
    DAILY_BUDGET_MINUTES: int = int(os.getenv("DAILY_BUDGET_MINUTES", "90"))

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
